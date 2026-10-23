"""엔진·그림·렌더링 작업을 웹 요청과 분리된 작업 스레드에서 수행한다.

웹 요청은 실행(run)을 만들고 바로 돌아간다. 작업 스레드는 엔진을 부른 뒤 결과를
프로젝트에 반영한다. 엔진이 질문(need_input)을 돌려주면 실행은 waiting_for_user로
멈추고, 답변이 오면 같은 실행을 running으로 되돌려 저장된 세션으로 이어 간다.

제작 단계에서 카드 하나를 바꾸면 그 카드의 rev만 올리고 바뀌기 전 내용을 history에
남긴다. 고치지 않은 카드의 그림·PNG 파일은 건드리지 않는다. 카드가 바뀌면
result_version이 오르고 최종 승인은 풀린다.
"""
from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .engines import Cancelled, Engine, EngineError, Outcome
from .images import build_prompt
from .render import Renderer
from .render import _luminance_hex as _luminance
from .store import Store, new_id, now_iso

log = logging.getLogger(__name__)

# 실행 종류마다 프로젝트가 놓이는 단계
STAGE_OF_KIND = {
    "research": "research",
    "deep_research": "deep_research",
    "storyboard": "storyboard",
    "images": "production",
    "card_image": "review",
    "render": "review",
    "revise": "review",
}
ENGINE_KINDS = ("research", "deep_research", "storyboard")
ACTIVE_STATUSES = ("queued", "running")
# 이 오류가 나면 남은 카드도 같은 이유로 실패할 것이므로 그림 만들기를 멈춘다
STOP_IMAGE_CODES = ("rate_limited", "auth_required", "engine_unavailable", "permission_denied")


class Worker:
    def __init__(self, store: Store, engine: Engine, images: Any, renderer: Renderer, image_quota: Callable[[], bool] | None = None):
        self.store = store
        self.engine = engine
        self.images = images
        self.renderer = renderer
        # 공개 서버의 하루 그림 상한. 그림 한 장을 만들기 전에 부르고, False면 만들지 않는다
        self.image_quota = image_quota or (lambda: True)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="run")
        self._cancels: dict[str, threading.Event] = {}

    # 웹 요청에서 부르는 함수

    def start(self, project_id: str, kind: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.store.create_run(
            {"id": new_id("run"), "project_id": project_id, "kind": kind, "params": params or {}, "status": "queued", "engine": self.engine.name}
        )

        def mark(p: dict[str, Any]) -> None:
            p.update(status="running", stage=STAGE_OF_KIND[kind], active_run_id=run["id"], error=None, question=None)

        self.store.update_project(project_id, mark)
        self._submit(run["id"])
        return run

    def resume(self, run_id: str) -> None:
        """답변을 받은 실행을 같은 run ID로 이어 간다."""
        self.store.update_run(run_id, lambda r: r.update(status="queued"))
        self._submit(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["status"] in ACTIVE_STATUSES:
            self._cancels.setdefault(run_id, threading.Event()).set()
            self._emit(run_id, "취소 요청을 받았습니다. 하위 작업을 정리하는 중입니다.")
        elif run["status"] == "waiting_for_user":
            # 기다리는 동안에는 살아 있는 엔진 호출이 없으므로 바로 취소로 기록한다
            self._finish(run, "cancelled", _cancel_error())
        return self.store.get_run(run_id)

    def recover_interrupted(self) -> int:
        """서버가 꺼질 때 돌던 실행을 '중단됨'으로 바꾼다. 서버 시작 시 한 번 부른다."""
        runs = self.store.runs_with_status(*ACTIVE_STATUSES)
        for run in runs:
            error = {
                "code": "interrupted",
                "message": "서버가 다시 시작되어 진행 중이던 작업이 중단됐습니다.",
                "next_action": "‘다시 시도’를 누르면 저장된 선택·답변·기획과 이미 만든 카드는 두고 남은 작업만 다시 실행합니다.",
            }
            self._finish(run, "interrupted", error, emit="서버 재시작으로 중단됨")
        return len(runs)

    # 작업 스레드

    def _submit(self, run_id: str) -> None:
        self._cancels[run_id] = threading.Event()
        self._pool.submit(self._execute, run_id)

    def _emit(self, run_id: str, message: str) -> None:
        self.store.update_run(run_id, lambda r: r["events"].append({"at": now_iso(), "message": message}))

    def _execute(self, run_id: str) -> None:
        cancel = self._cancels[run_id]
        run = self.store.get_run(run_id)
        if run["status"] != "queued":
            return
        if cancel.is_set():
            self._finish(run, "cancelled", _cancel_error())
            return
        self.store.update_run(run_id, lambda r: r.update(status="running"))
        emit = lambda m: self._emit(run_id, m)  # noqa: E731
        handlers: dict[str, Callable[[dict[str, Any], Callable[[str], None], threading.Event], None]] = {
            "images": self._run_images,
            "card_image": self._run_card_image,
            "render": self._run_render,
            "revise": self._run_revise,
        }
        try:
            if run["kind"] in ENGINE_KINDS:
                project = self.store.get_project(run["project_id"])
                work_dir = self.store.project_dir(project["id"]) / "engine"
                outcome = self.engine.execute(
                    build_task(project, run["kind"], str(work_dir)), project.get("engine_session_id"), emit, cancel
                )
                self._apply(run, outcome)
            else:
                handlers[run["kind"]](run, emit, cancel)
        except Cancelled:
            self._finish(run, "cancelled", _cancel_error())
        except EngineError as e:
            self._finish(run, "failed", e.as_dict(), emit=f"실패: {e.message}")
        except Exception as e:  # 엔진 밖의 버그도 완료로 보이지 않게 실패로 남긴다
            log.error("run %s crashed:\n%s", run_id, traceback.format_exc())
            error = {
                "code": "internal",
                "message": f"앱 내부 오류가 났습니다: {e}",
                "next_action": "‘다시 시도’를 누르고, 반복되면 서버 창에 나온 오류를 알려 주세요.",
            }
            self._finish(run, "failed", error, emit="앱 내부 오류")
        finally:
            self._cancels.pop(run_id, None)

    def _apply(self, run: dict[str, Any], outcome: Outcome) -> None:
        run_id, kind = run["id"], run["kind"]

        if outcome.kind == "need_input":
            question = outcome.data

            def ask(p: dict[str, Any]) -> None:
                asked = p.setdefault("question_counts", {})
                asked[question["id"]] = asked.get(question["id"], 0) + 1
                p["question"] = {
                    **question,
                    "version": asked[question["id"]],
                    "run_id": run_id,
                    "asked_at": now_iso(),
                }
                p["status"] = "waiting_for_user"
                p["engine_session_id"] = outcome.session_id or p.get("engine_session_id")

            self.store.update_project(run["project_id"], ask)
            self.store.update_run(run_id, lambda r: r.update(status="waiting_for_user", session_id=outcome.session_id))
            self._emit(run_id, f"질문 대기: {question['question']}")
            return

        if outcome.kind != "result":
            error = {
                "code": "invalid_output",
                "message": f"엔진이 알 수 없는 결과({outcome.kind})를 돌려줬습니다.",
                "next_action": "‘다시 시도’를 누르세요.",
            }
            self._finish(run, "failed", error)
            return

        def done(p: dict[str, Any]) -> None:
            p["engine_session_id"] = outcome.session_id or p.get("engine_session_id")
            p["question"] = None
            if kind == "research":
                p.update(research=outcome.data, selection=None, stage="select", status="waiting_for_user")
            elif kind == "deep_research":
                p.update(editorial=outcome.data, stage="editorial", status="idle")
            elif kind == "storyboard":
                p.update(storyboard={**outcome.data, "generated_at": now_iso()}, stage="storyboard", status="waiting_for_user")

        self.store.update_project(run["project_id"], done)
        self.store.update_run(run_id, lambda r: r.update(status="succeeded", session_id=outcome.session_id))
        self._emit(run_id, "완료")
        if kind == "deep_research":
            # 편집 방향이 정해지면 바로 카드별 기획을 만든다
            self.start(run["project_id"], "storyboard")

    def _finish(self, run: dict[str, Any], status: str, error: dict[str, str], emit: str | None = None) -> None:
        def stop(r: dict[str, Any]) -> None:
            r.update(status=status, error=error)
            r["events"].append({"at": now_iso(), "message": emit or error["message"]})

        self.store.update_run(run["id"], stop)

        def mark(p: dict[str, Any]) -> None:
            for c in p.get("cards") or []:
                if c["image"].get("status") == "running":
                    _image_error(c, error)
            if p.get("active_run_id") != run["id"]:
                return
            if run["kind"] in ("card_image", "render", "revise"):
                # 카드 단위 작업이 실패해도 검토 화면은 그대로 쓸 수 있다. 오류는 실행 기록과 카드에 남는다.
                p.update(status="waiting_for_user", stage="review", error={**error, "scope": "card"}, question=None)
            else:
                p.update(status=status, error=error, question=None)

        self.store.update_project(run["project_id"], mark)

    # 제작 단계

    def _run_images(self, run: dict[str, Any], emit: Callable[[str], None], cancel: threading.Event) -> None:
        pid = run["project_id"]
        stop: dict[str, str] | None = None
        for card in sorted(self.store.get_project(pid)["cards"], key=lambda c: c["order"]):
            if cancel.is_set():
                raise Cancelled()
            cid = card["id"]
            current = _card(self.store.get_project(pid), cid)
            if current["image"].get("status") != "done":
                if stop:
                    blocked = {**stop, "message": f"앞 카드에서 멈춤: {stop['message']}"}
                    self._change_card(pid, cid, lambda c: _image_error(c, blocked), bump=False)
                    continue
                try:
                    self._make_image(pid, cid, self.images.name, emit, cancel, reason="그림 생성")
                except EngineError as e:
                    self._change_card(pid, cid, lambda c: _image_error(c, e.as_dict()), bump=False)
                    emit(f"카드 {current['order']} 그림 실패: {e.message}")
                    if e.code in STOP_IMAGE_CODES:
                        stop = e.as_dict()
                        emit("같은 이유로 남은 카드도 실패할 것이므로 그림 만들기를 멈춥니다.")
                    continue
            self._render_if_needed(pid, [cid], emit)
        self._to_review(run, emit)

    def _run_card_image(self, run: dict[str, Any], emit: Callable[[str], None], cancel: threading.Event) -> None:
        pid, params = run["project_id"], run["params"]
        cid = params["card_id"]
        try:
            self._make_image(pid, cid, params["tool"], emit, cancel, reason=params.get("reason") or "그림 다시 만들기")
        except EngineError as e:
            self._change_card(pid, cid, lambda c: _image_error(c, e.as_dict()), bump=False)
            raise
        self._render_if_needed(pid, [cid], emit)
        self._to_review(run, emit)

    def _run_render(self, run: dict[str, Any], emit: Callable[[str], None], cancel: threading.Event) -> None:
        self._render_if_needed(run["project_id"], run["params"]["card_ids"], emit)
        self._to_review(run, emit)

    def _run_revise(self, run: dict[str, Any], emit: Callable[[str], None], cancel: threading.Event) -> None:
        pid, params = run["project_id"], run["params"]
        project = self.store.get_project(pid)
        chosen = [_card(project, cid) for cid in params["card_ids"]]
        task = {
            "stage": "revise",
            "topic": project["topic"],
            "instruction": params["instruction"],
            "scope": params["scope"],
            "cards": [
                {"id": c["id"], "role": c["role"], "title": c["title"], "body": c["body"], "sources": [s["label"] for s in c["sources"]], "scene": c["scene"]}
                for c in chosen
            ],
            "work_dir": str(self.store.project_dir(pid) / "engine"),
        }
        outcome = self.engine.execute(task, project.get("engine_session_id"), emit, cancel)
        if outcome.session_id:
            self.store.update_project(pid, lambda p: p.update(engine_session_id=outcome.session_id))
        reason = f"수정 요청: {params['instruction']}"
        image_errors = []
        for rc in outcome.data["cards"]:
            old = _card(self.store.get_project(pid), rc["id"])
            if params["scope"] != "image" and (rc["title"] != old["title"] or rc["body"] != old["body"]):
                def edit(c: dict[str, Any], rc: dict[str, Any] = rc) -> None:
                    _snapshot(c, reason)
                    c.update(title=rc["title"], body=rc["body"], last_change=rc["change_note"])

                self._change_card(pid, rc["id"], edit)
                emit(f"카드 {old['order']} 글 수정: {rc['change_note']}")
            if params["scope"] == "image" or (params["scope"] == "auto" and rc["needs_new_image"]):
                try:
                    self._make_image(pid, rc["id"], self.images.name, emit, cancel, scene=rc.get("image_scene") or old["scene"], reason=reason)
                except EngineError as e:
                    self._change_card(pid, rc["id"], lambda c: _image_error(c, e.as_dict()), bump=False)
                    image_errors.append(f"카드 {old['order']}: {e.message}")
        self._render_if_needed(pid, params["card_ids"], emit)
        if image_errors:
            emit("그림을 다시 만들지 못한 카드: " + "; ".join(image_errors))
        self._to_review(run, emit)

    def _make_image(
        self, pid: str, cid: str, tool: str, emit: Callable[[str], None], cancel: threading.Event,
        scene: str | None = None, reason: str = "",
    ) -> None:
        project = self.store.get_project(pid)
        card = _card(project, cid)
        visual = project["storyboard"]["visual"]
        scene = scene or card["scene"]
        pdir = self.store.project_dir(pid)
        n = card.get("image_count", 0) + 1
        out_base = pdir / "images" / f"{cid}_i{n}"
        self._change_card(pid, cid, lambda c: c["image"].update(status="running"), bump=False)

        if tool == "claude":
            emit(f"카드 {card['order']} 그림을 {self.engine.name} 엔진이 SVG 도형으로 그리는 중")
            prompt = f"SVG 도형 그림. 장면: {scene}. 그림체: {visual['style']}. 색: {', '.join(visual['palette'])}"
            outcome = self.engine.execute(
                {"stage": "svg_image", "scene": scene, "visual": visual, "work_dir": str(pdir / "engine")}, None, emit, cancel
            )
            out = out_base.with_suffix(".png")
            info = self.renderer.svg_to_png(outcome.data["svg"], out)
            made = {"file": out, "conversation_id": None, **info}
            used = "mock" if self.engine.is_mock else "svg"
        else:
            if not self.image_quota():
                self._change_card(pid, cid, lambda c: c["image"].update(status="done" if c["image"].get("file") else "pending"), bump=False)
                raise EngineError("rate_limited", "오늘 이 서버에서 만들 수 있는 그림 수를 모두 썼습니다.", "내일 다시 ‘그림 다시 만들기’를 누르세요.")
            emit(f"카드 {card['order']} 그림 만드는 중 ({self.images.name})")
            prompt = build_prompt(scene, visual, card["style"]["position"], self.images.name, project["topic"])
            made = self.images.generate(prompt, out_base, pdir / "agy", emit, cancel)
            used = self.images.name

        def apply(c: dict[str, Any]) -> None:
            if c["image"].get("file"):
                _snapshot(c, reason or "그림 다시 만들기")
            c["image_count"] = n
            c["scene"] = scene
            c["image"] = {
                "status": "done",
                "file": _rel(made["file"], pdir),
                "tool": used,
                "prompt": prompt,
                "created_at": now_iso(),
                "width": made.get("width"),
                "height": made.get("height"),
                "format": made.get("format"),
                "conversation_id": made.get("conversation_id"),
                "error": None,
            }

        self._change_card(pid, cid, apply)

    def _render_if_needed(self, pid: str, card_ids: list[str], emit: Callable[[str], None]) -> None:
        project = self.store.get_project(pid)
        pdir = self.store.project_dir(pid)
        total = len(project["cards"])
        # 모든 카드에 같은 바탕색(공통 색 중 가장 밝은 색)을 써서 카드뉴스 한 편이 한 벌로 보이게 한다
        background = max(project["storyboard"]["visual"]["palette"], key=_luminance)
        jobs = []
        for cid in card_ids:
            c = _card(project, cid)
            if c["image"].get("status") != "done" or not c["image"].get("file"):
                continue
            if c["render"].get("status") == "done" and c["render"].get("rev") == c["rev"]:
                continue
            jobs.append({
                "card": c,
                "image_path": pdir / c["image"]["file"],
                "out_png": pdir / "renders" / f"{cid}_r{c['rev']}.png",
                "index": c["order"],
                "total": total,
                "background": background,
            })
        if not jobs:
            return
        emit("글자 배치·PNG 만들기: 카드 " + ", ".join(str(j["index"]) for j in jobs))
        for job, res in zip(jobs, self.renderer.render(jobs)):
            def apply(c: dict[str, Any], job: dict[str, Any] = job, res: dict[str, Any] = res) -> None:
                c["render"] = {
                    "status": "done",
                    "file": _rel(res["file"], pdir),
                    "rev": job["card"]["rev"],
                    "size": res["size"],
                    "checks": res["checks"],
                    "warnings": res["warnings"],
                    "rendered_at": now_iso(),
                }

            self._change_card(pid, job["card"]["id"], apply, bump=False)
            if res["warnings"]:
                emit(f"카드 {job['index']} 검사 경고: " + "; ".join(w["message"] for w in res["warnings"]))

    def _to_review(self, run: dict[str, Any], emit: Callable[[str], None]) -> None:
        def ready(p: dict[str, Any]) -> None:
            if p.get("active_run_id") == run["id"]:
                p.update(stage="review", status="waiting_for_user", error=None)

        self.store.update_project(run["project_id"], ready)
        self.store.update_run(run["id"], lambda r: r.update(status="succeeded"))
        self._emit(run["id"], "완료")

    def _change_card(self, pid: str, cid: str, change: Callable[[dict[str, Any]], None], bump: bool = True) -> dict[str, Any]:
        def apply(p: dict[str, Any]) -> None:
            change(_card(p, cid))
            if bump:
                p["result_version"] = p.get("result_version", 0) + 1
                p["final"] = None

        return self.store.update_project(pid, apply)


def build_task(project: dict[str, Any], kind: str, work_dir: str) -> dict[str, Any]:
    """엔진에 넘길 작업 설명. work_dir은 프로젝트마다 따로 둔 엔진 작업 폴더다."""
    if kind == "research":
        return {
            "stage": "research",
            "topic": project["topic"],
            "period_days": project["period_days"],
            "now": now_iso(),
            "work_dir": work_dir,
        }
    if kind == "deep_research":
        chosen = set(project["selection"]["candidate_ids"])
        return {
            "stage": "deep_research",
            "topic": project["topic"],
            "research": {k: v for k, v in project["research"].items() if k != "candidates"},
            "selected": [c for c in project["research"]["candidates"] if c["id"] in chosen],
            "read_urls": [c["url"] for c in project["research"]["candidates"] if c.get("read_original")],
            "answers": project.get("answers", []),
            "work_dir": work_dir,
        }
    if kind == "storyboard":
        previous = project.get("storyboard")
        return {
            "stage": "storyboard",
            "topic": project["topic"],
            "research": {k: v for k, v in project["research"].items() if k != "candidates"},
            "editorial": project["editorial"],
            "feedback": project.get("storyboard_feedback", []),
            "previous": {k: previous[k] for k in ("cards", "visual")} if previous else None,
            "work_dir": work_dir,
        }
    raise ValueError(f"unknown run kind: {kind}")


def _card(project: dict[str, Any], cid: str) -> dict[str, Any]:
    for c in project.get("cards") or []:
        if c["id"] == cid:
            return c
    raise KeyError(cid)


def _snapshot(c: dict[str, Any], reason: str) -> None:
    """바뀌기 전 카드를 history에 남기고 rev를 올린다."""
    c.setdefault("history", []).append(
        {
            "rev": c["rev"],
            "title": c["title"],
            "body": c["body"],
            "style": dict(c["style"]),
            "image_file": c["image"].get("file"),
            "png_file": c["render"].get("file"),
            "replaced_at": now_iso(),
            "reason": reason,
        }
    )
    c["rev"] += 1


def _image_error(c: dict[str, Any], error: dict[str, str]) -> None:
    # 예전 그림이 있으면 그대로 두고 오류만 적는다
    c["image"]["status"] = "done" if c["image"].get("file") else "failed"
    c["image"]["error"] = error


def _rel(path: Path, base: Path) -> str:
    return Path(path).resolve().relative_to(base.resolve()).as_posix()


def _cancel_error() -> dict[str, str]:
    return {"code": "cancelled", "message": "작업을 취소했습니다.", "next_action": "필요하면 ‘다시 시도’를 누르세요."}
