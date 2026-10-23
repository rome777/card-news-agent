"""웹 화면과 API. 엔진 호출은 Worker에 넘기고 요청은 바로 돌려준다.

변경을 일으키는 요청은 requestId로 중복을 식별한다. 같은 ID·같은 입력은 저장한
응답을 돌려주고, 같은 ID·다른 입력은 409로 거절한다(PRD '웹 API 계약 초안').
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import exports
from .render import default_style
from .store import NotFound, Store, new_id, now_iso
from .worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
FREE_TEXT = "__free__"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, next_action: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.next_action = next_action


class Limits:
    """공개 서버의 하루 사용량 상한. 0은 제한 없음. 숫자는 저장소(SQLite)에 남겨 서버를 다시 켜도 그날 것은 유지한다.
    (디스크가 남지 않는 무료 호스팅에서는 디스크와 함께 사라진다.) 날짜는 서버 시간대 기준이다."""

    def __init__(
        self, projects: int = 0, ip_projects: int = 0, images: int = 0, store: Store | None = None,
        unlimited_ips: set[str] | None = None,
    ):
        self.projects, self.ip_projects, self.images = projects, ip_projects, images
        self.store = store
        # 접속 주소당 상한을 적용하지 않는 주소(운영자 등). 저장소에 올리지 않도록 환경 변수로만 받는다.
        self.unlimited_ips = unlimited_ips or set()
        self._lock = threading.Lock()
        self._memory: dict[tuple[str, str], int] = {}

    def _count(self, day: str, key: str) -> int:
        return self.store.usage_count(day, key) if self.store else self._memory.get((day, key), 0)

    def _add(self, day: str, key: str) -> None:
        if self.store:
            self.store.add_usage(day, key)
        else:
            self._memory[(day, key)] = self._memory.get((day, key), 0) + 1

    def take_project(self, ip: str) -> str | None:
        """새 프로젝트를 하나 쓸 수 있으면 세고 None, 상한이면 이유를 돌려준다."""
        day = date.today().isoformat()
        with self._lock:
            if self.projects and self._count(day, "projects") >= self.projects:
                return f"오늘 이 서버에서 만들 수 있는 카드뉴스 수({self.projects}개)를 모두 썼습니다."
            if self.ip_projects and ip not in self.unlimited_ips and self._count(day, f"ip:{ip}") >= self.ip_projects:
                return f"카드뉴스는 같은 접속 주소에서 하루 {self.ip_projects}번까지 만들 수 있습니다. 오늘은 이미 만들었습니다."
            self._add(day, "projects")
            self._add(day, f"ip:{ip}")
            return None

    def take_image(self) -> bool:
        day = date.today().isoformat()
        with self._lock:
            if self.images and self._count(day, "images") >= self.images:
                return False
            self._add(day, "images")
            return True


class CreateProjectBody(BaseModel):
    topic: str = Field(min_length=1, max_length=100)
    periodDays: int = Field(default=7, ge=1, le=30)
    requestId: str = Field(min_length=1, max_length=100)


class ResearchBody(BaseModel):
    requestId: str = Field(min_length=1, max_length=100)
    periodDays: int | None = Field(default=None, ge=1, le=30)


class SelectionBody(BaseModel):
    candidateIds: list[str]
    version: int
    requestId: str = Field(min_length=1, max_length=100)


class Answer(BaseModel):
    choice: str | None = None
    text: str | None = Field(default=None, max_length=500)


class AnswerBody(BaseModel):
    questionId: str
    version: int
    answer: Answer
    requestId: str = Field(min_length=1, max_length=100)


class RetryBody(BaseModel):
    requestId: str = Field(min_length=1, max_length=100)


class StoryboardReviseBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=500)
    version: int
    requestId: str = Field(min_length=1, max_length=100)


class ApproveBody(BaseModel):
    stage: Literal["storyboard", "final"]
    version: int
    requestId: str = Field(min_length=1, max_length=100)


class CardImageBody(BaseModel):
    # default: 서버를 켤 때 고른 그림 작업자(agy·openai·mock). "agy"는 예전 화면과의 호환용으로 같은 뜻
    tool: Literal["default", "agy", "claude"]
    requestId: str = Field(min_length=1, max_length=100)


class CardStyle(BaseModel):
    position: Literal["top", "bottom"] = "top"
    font_scale: float = Field(default=1.0, ge=0.7, le=1.4)
    text_color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    box_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    box_opacity: float = Field(default=0.6, ge=0, le=1)


class CardEditBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(default="", max_length=400)
    source_line: str = Field(default="", max_length=200)
    # 그림 장면. 바꿔도 그림은 그대로이고, '그림 다시 만들기'를 누를 때 이 장면으로 그린다
    scene: str | None = Field(default=None, max_length=400)
    style: CardStyle
    version: int
    requestId: str = Field(min_length=1, max_length=100)


class RevisionBody(BaseModel):
    cardIds: list[str]
    instruction: str = Field(min_length=1, max_length=500)
    scope: Literal["auto", "text", "image"] = "auto"
    version: int
    requestId: str = Field(min_length=1, max_length=100)


def client_ip(request: Request, trust_proxy: bool) -> str:
    """접속 주소. 호스팅의 프록시 뒤에서는 모든 요청이 프록시 주소로 보이므로, trust_proxy일 때만
    프록시가 붙여 주는 헤더에서 실제 주소를 읽는다(프록시 없이 이 헤더를 믿으면 누구나 꾸밀 수 있다)."""
    if trust_proxy:
        for name in ("cf-connecting-ip", "true-client-ip"):
            if request.headers.get(name):
                return request.headers[name].strip()
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def create_app(
    store: Store, worker: Worker, public: bool = False, limits: Limits | None = None, trust_proxy: bool = False
) -> FastAPI:
    """public이면 프로젝트 목록을 내주지 않는다(각 브라우저는 자기가 만든 프로젝트 ID만 기억한다)."""
    app = FastAPI(title="카드뉴스 에이전트")
    limits = limits or Limits()
    engine = worker.engine
    # 상태 검사와 변경 사이에 다른 요청이 끼어들지 않게 변경 요청을 직렬화한다
    mutate = threading.Lock()

    @app.middleware("http")
    async def _no_stale_ui(request: Request, call_next):
        # 앱을 고친 뒤 서버를 다시 켜면 브라우저가 예전 화면 파일을 쓰지 않게 한다
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        body = {"code": e.code, "message": e.message, "nextAction": e.next_action}
        return JSONResponse(status_code=e.status, content={"error": body})

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in err['loc'][1:])}: {err['msg']}" for err in e.errors())
        body = {"code": "invalid_request", "message": f"요청 형식이 잘못됐습니다. {detail}", "nextAction": None}
        return JSONResponse(status_code=400, content={"error": body})

    def project_or_404(project_id: str) -> dict[str, Any]:
        try:
            return store.get_project(project_id)
        except NotFound:
            raise ApiError(404, "not_found", "프로젝트를 찾을 수 없습니다.", "목록에서 다시 고르세요.")

    def once(scope: str, request_id: str, action: str, payload: Any, make: Callable[[], Any]) -> Any:
        fingerprint = hashlib.sha256(json.dumps([action, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with mutate:
            saved = store.get_request(scope, request_id)
            if saved is not None:
                if saved[0] != fingerprint:
                    raise ApiError(409, "request_conflict", "같은 요청 ID로 다른 내용을 보냈습니다.", "새로고침한 뒤 다시 시도하세요.")
                return saved[1]
            response = make()
            store.save_request(scope, request_id, fingerprint, response)
            return response

    def view(project: dict[str, Any]) -> dict[str, Any]:
        return {"project": project, "runs": store.list_runs(project["id"], limit=5)}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {"engine": engine.name, "mock": engine.is_mock, "images": worker.images.name, "public": public}

    @app.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        if public:
            # 공개 서버에서는 남의 프로젝트를 보여 주지 않는다
            return {"projects": [], "public": True}
        keys = ("id", "topic", "stage", "status", "updated_at", "mock", "engine")
        return {"projects": [{k: p.get(k) for k in keys} for p in store.list_projects()]}

    @app.post("/api/projects")
    def create_project(body: CreateProjectBody, request: Request) -> dict[str, Any]:
        topic = body.topic.strip()
        if not topic:
            raise ApiError(400, "invalid_request", "주제를 입력해 주세요.")
        saved = store.get_request("_global", body.requestId)
        if saved is None:
            over = limits.take_project(client_ip(request, trust_proxy))
            if over:
                raise ApiError(429, "daily_limit", over, "내일 다시 해 주세요.")

        def make() -> dict[str, Any]:
            project = store.create_project(
                {
                    "id": new_id("prj"),
                    "topic": topic,
                    "period_days": body.periodDays,
                    "engine": engine.name,
                    "mock": engine.is_mock,
                    "stage": "created",
                    "status": "idle",
                    "engine_session_id": None,
                    "active_run_id": None,
                    "research": None,
                    "selection": None,
                    "question": None,
                    "answers": [],
                    "editorial": None,
                    "storyboard": None,
                    "cards": [],
                    "result_version": 0,
                    "final": None,
                    "error": None,
                }
            )
            return {"projectId": project["id"]}

        return once("_global", body.requestId, "create", body.model_dump(exclude={"requestId"}), make)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return view(project_or_404(project_id))

    @app.post("/api/projects/{project_id}/research")
    def research(project_id: str, body: ResearchBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            if project["stage"] not in ("created", "research", "select"):
                raise ApiError(409, "invalid_stage", "이미 후보를 고른 프로젝트라 다시 조사할 수 없습니다.", "새 프로젝트를 만드세요.")
            if project["status"] == "running":
                # 진행 중에 같은 요청을 반복해도 새로 조사하지 않는다
                return {"runId": project["active_run_id"], "status": "running", "alreadyRunning": True}
            if body.periodDays:
                store.update_project(project_id, lambda p: p.update(period_days=body.periodDays))
            run = worker.start(project_id, "research")
            return {"runId": run["id"], "status": run["status"], "alreadyRunning": False}

        return once(project_id, body.requestId, "research", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/selection")
    def selection(project_id: str, body: SelectionBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            if project["stage"] != "select" or project["status"] == "running":
                raise ApiError(409, "invalid_stage", "지금은 후보를 고르는 단계가 아닙니다.", "새로고침해서 현재 단계를 확인하세요.")
            if body.version != project["version"]:
                raise ApiError(409, "stale_version", "화면이 오래됐습니다. 그사이 프로젝트가 바뀌었습니다.", "새로고침한 뒤 다시 고르세요.")
            ids = list(dict.fromkeys(body.candidateIds))
            known = {c["id"] for c in project["research"]["candidates"]}
            if not 1 <= len(ids) <= 3:
                raise ApiError(400, "invalid_selection", "후보는 1~3개를 골라 주세요.")
            if unknown := [i for i in ids if i not in known]:
                raise ApiError(400, "invalid_selection", f"없는 후보입니다: {', '.join(unknown)}", "새로고침한 뒤 다시 고르세요.")
            selected = {"candidate_ids": ids, "saved_at": now_iso()}
            store.update_project(project_id, lambda p: p.update(selection=selected))
            run = worker.start(project_id, "deep_research")
            return {"selection": selected, "runId": run["id"], "status": run["status"]}

        return once(project_id, body.requestId, "selection", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/answers")
    def answers(project_id: str, body: AnswerBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            question = project.get("question")
            if question is None:
                raise ApiError(409, "no_pending_question", "기다리는 질문이 없습니다. 이미 답했거나 질문이 바뀌었습니다.", "새로고침해서 현재 상태를 확인하세요.")
            if question["id"] != body.questionId or question["version"] != body.version:
                raise ApiError(409, "stale_question", "지난 질문에 대한 답변이라 반영하지 않았습니다.", "새로고침해서 지금 질문에 답하세요.")
            label = _answer_label(question, body.answer)
            record = {
                "question_id": question["id"],
                "version": question["version"],
                "question": question["question"],
                "answer": body.answer.model_dump(),
                "answer_label": label,
                "answered_at": now_iso(),
            }

            def accept(p: dict[str, Any]) -> None:
                p["answers"].append(record)
                p.update(question=None, status="running")

            store.update_project(project_id, accept)
            worker.resume(question["run_id"])
            return {"accepted": True, "status": "running", "runId": question["run_id"]}

        return once(project_id, body.requestId, "answer", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/retry")
    def retry(project_id: str, body: RetryBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            if project["status"] not in ("failed", "interrupted", "cancelled"):
                raise ApiError(409, "invalid_stage", "다시 시도할 실패 작업이 없습니다.", "새로고침해서 현재 상태를 확인하세요.")
            last = store.get_run(project["active_run_id"])
            run = worker.start(project_id, last["kind"], last.get("params"))
            return {"runId": run["id"], "status": run["status"], "retryOf": last["id"]}

        return once(project_id, body.requestId, "retry", body.model_dump(exclude={"requestId"}), make)

    def editable(project: dict[str, Any], version: int | None = None) -> None:
        """제작이 끝난 카드를 고칠 수 있는 상태인지 확인한다."""
        if project["stage"] not in ("review", "final") or project["status"] == "running":
            raise ApiError(409, "invalid_stage", "지금은 카드를 고칠 수 없습니다. 진행 중인 작업이 끝난 뒤 다시 하세요.", "새로고침해서 현재 단계를 확인하세요.")
        if version is not None and version != project["version"]:
            raise ApiError(409, "stale_version", "화면이 오래됐습니다. 그사이 프로젝트가 바뀌었습니다.", "새로고침한 뒤 다시 하세요.")

    @app.post("/api/projects/{project_id}/storyboard/revise")
    def revise_storyboard(project_id: str, body: StoryboardReviseBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            if project["stage"] != "storyboard" or project["status"] != "waiting_for_user":
                raise ApiError(409, "invalid_stage", "지금은 스토리보드를 고칠 수 없습니다.", "새로고침해서 현재 단계를 확인하세요.")
            if body.version != project["version"]:
                raise ApiError(409, "stale_version", "화면이 오래됐습니다.", "새로고침한 뒤 다시 요청하세요.")
            note = {"instruction": body.instruction.strip(), "at": now_iso()}
            store.update_project(project_id, lambda p: p.setdefault("storyboard_feedback", []).append(note))
            run = worker.start(project_id, "storyboard")
            return {"runId": run["id"], "status": run["status"]}

        return once(project_id, body.requestId, "storyboard_revise", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/approve")
    def approve(project_id: str, body: ApproveBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            if body.version != project["version"]:
                raise ApiError(409, "stale_version", "화면이 오래됐습니다. 승인하려던 내용과 지금 내용이 다릅니다.", "새로고침해서 다시 확인한 뒤 승인하세요.")
            if body.stage == "storyboard":
                if project["stage"] != "storyboard" or project["status"] != "waiting_for_user":
                    raise ApiError(409, "invalid_stage", "지금은 스토리보드를 승인할 수 없습니다.", "새로고침해서 현재 단계를 확인하세요.")
                cards = _cards_from_storyboard(project)

                def approve_board(p: dict[str, Any]) -> None:
                    p["storyboard"]["approved_at"] = now_iso()
                    p.update(cards=cards, result_version=1, final=None)

                store.update_project(project_id, approve_board)
                run = worker.start(project_id, "images")
                return {"stage": "production", "runId": run["id"]}

            if project["stage"] != "review" or project["status"] != "waiting_for_user":
                raise ApiError(409, "invalid_stage", "지금은 최종 승인할 수 없습니다.", "새로고침해서 현재 단계를 확인하세요.")
            unready = [str(c["order"]) for c in project["cards"] if c["render"].get("status") != "done" or c["render"].get("rev") != c["rev"]]
            if unready:
                raise ApiError(409, "not_ready", f"아직 완성되지 않은 카드가 있습니다: {', '.join(unready)}번", "그림과 글자 배치가 끝난 뒤 승인하세요.")
            export = exports.build(store.project_dir(project_id), project)

            def approve_final(p: dict[str, Any]) -> None:
                p.update(stage="final", status="idle", error=None)
                p["final"] = {"approved_at": now_iso(), "result_version": project["result_version"], "export": export, "verification": None}

            store.update_project(project_id, approve_final)
            return {"stage": "final", "resultVersion": project["result_version"], "export": export}

        return once(project_id, body.requestId, "approve", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/images")
    def remake_failed_images(project_id: str, body: RetryBody) -> dict[str, Any]:
        """그림이 없는 카드만 다시 만든다. 이미 완성된 카드는 건드리지 않는다."""
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            editable(project)
            missing = [c["order"] for c in project["cards"] if c["image"].get("status") != "done"]
            if not missing:
                raise ApiError(409, "invalid_stage", "그림이 없는 카드가 없습니다.")
            run = worker.start(project_id, "images")
            return {"runId": run["id"], "status": run["status"], "cards": missing}

        return once(project_id, body.requestId, "images", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/cards/{card_id}/image")
    def card_image(project_id: str, card_id: str, body: CardImageBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            editable(project)
            if card_id not in {c["id"] for c in project["cards"]}:
                raise ApiError(404, "not_found", "카드를 찾을 수 없습니다.")
            reason = "코드 도형 그림으로 바꿈" if body.tool == "claude" else "그림 다시 만들기"
            tool = "claude" if body.tool == "claude" else worker.images.name
            run = worker.start(project_id, "card_image", {"card_id": card_id, "tool": tool, "reason": reason})
            return {"runId": run["id"], "status": run["status"]}

        return once(project_id, body.requestId, f"card_image:{card_id}", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/cards/{card_id}/edit")
    def card_edit(project_id: str, card_id: str, body: CardEditBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            editable(project, body.version)
            if card_id not in {c["id"] for c in project["cards"]}:
                raise ApiError(404, "not_found", "카드를 찾을 수 없습니다.")

            def edit(p: dict[str, Any]) -> None:
                c = next(c for c in p["cards"] if c["id"] == card_id)
                new = {"title": body.title.strip(), "body": body.body.strip(), "source_line": body.source_line.strip(), "style": body.style.model_dump()}
                if body.scene is not None and body.scene.strip():
                    new["scene"] = body.scene.strip()
                if all(c.get(k) == v for k, v in new.items()):
                    return
                c.setdefault("history", []).append({
                    "rev": c["rev"], "title": c["title"], "body": c["body"], "style": dict(c["style"]),
                    "image_file": c["image"].get("file"), "png_file": c["render"].get("file"),
                    "replaced_at": now_iso(), "reason": "화면에서 직접 고침(그림 유지)",
                })
                c["rev"] += 1
                c.update(new)
                p["result_version"] = p.get("result_version", 0) + 1
                p["final"] = None

            store.update_project(project_id, edit)
            run = worker.start(project_id, "render", {"card_ids": [card_id]})
            return {"runId": run["id"], "status": run["status"]}

        return once(project_id, body.requestId, f"card_edit:{card_id}", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/rerender")
    def rerender(project_id: str, body: RetryBody) -> dict[str, Any]:
        """그림은 그대로 두고 모든 카드의 글자 배치를 지금 레이아웃으로 다시 한다(앱의 배치 방식이 바뀌었을 때)."""
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            editable(project)
            ready = [c["id"] for c in project["cards"] if c["image"].get("status") == "done" and c["image"].get("file")]
            if not ready:
                raise ApiError(409, "invalid_stage", "다시 배치할 카드가 없습니다.")

            def bump(p: dict[str, Any]) -> None:
                for c in p["cards"]:
                    if c["id"] not in ready:
                        continue
                    c.setdefault("history", []).append({
                        "rev": c["rev"], "title": c["title"], "body": c["body"], "style": dict(c["style"]),
                        "image_file": c["image"].get("file"), "png_file": c["render"].get("file"),
                        "replaced_at": now_iso(), "reason": "카드 다시 배치(그림 유지)",
                    })
                    c["rev"] += 1
                p["result_version"] = p.get("result_version", 0) + 1
                p["final"] = None

            store.update_project(project_id, bump)
            run = worker.start(project_id, "render", {"card_ids": ready})
            return {"runId": run["id"], "status": run["status"], "cards": len(ready)}

        return once(project_id, body.requestId, "rerender", body.model_dump(exclude={"requestId"}), make)

    @app.post("/api/projects/{project_id}/revisions")
    def revisions(project_id: str, body: RevisionBody) -> dict[str, Any]:
        project_or_404(project_id)

        def make() -> dict[str, Any]:
            project = store.get_project(project_id)
            editable(project, body.version)
            ids = list(dict.fromkeys(body.cardIds))
            known = {c["id"] for c in project["cards"]}
            if not ids or any(i not in known for i in ids):
                raise ApiError(400, "invalid_selection", "고칠 카드를 1장 이상 골라 주세요.", "새로고침한 뒤 다시 고르세요.")
            params = {"card_ids": ids, "instruction": body.instruction.strip(), "scope": body.scope}
            run = worker.start(project_id, "revise", params)
            return {"runId": run["id"], "status": run["status"]}

        return once(project_id, body.requestId, "revision", body.model_dump(exclude={"requestId"}), make)

    @app.get("/api/projects/{project_id}/exports")
    def get_exports(project_id: str) -> dict[str, Any]:
        project = project_or_404(project_id)
        final = project.get("final")
        if not final or final["result_version"] != project.get("result_version"):
            raise ApiError(409, "not_approved", "최종 승인한 결과가 없습니다.", "카드를 검토하고 최종 승인하세요.")
        return {"resultVersion": final["result_version"], "approvedAt": final["approved_at"], **final["export"]}

    @app.post("/api/projects/{project_id}/exports/verify")
    def verify_exports(project_id: str) -> dict[str, Any]:
        project = project_or_404(project_id)
        if not project.get("final"):
            raise ApiError(409, "not_approved", "최종 승인한 결과가 없습니다.", "카드를 검토하고 최종 승인하세요.")
        result = exports.verify(store.project_dir(project_id), project)

        def save(p: dict[str, Any]) -> None:
            if p.get("final"):
                p["final"]["verification"] = result

        store.update_project(project_id, save)
        return result

    @app.get("/api/projects/{project_id}/files/{path:path}")
    def project_file(project_id: str, path: str) -> FileResponse:
        project_or_404(project_id)
        base = store.project_dir(project_id).resolve()
        target = (base / path).resolve()
        if base not in target.parents or not target.is_file() or target.name == "proc.pid":
            raise ApiError(404, "not_found", "파일을 찾을 수 없습니다.")
        download = path.startswith("exports/")
        return FileResponse(target, filename=target.name if download else None)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        try:
            with mutate:
                run = worker.cancel(run_id)
        except NotFound:
            raise ApiError(404, "not_found", "작업을 찾을 수 없습니다.")
        return {"runId": run["id"], "status": run["status"]}

    return app


def _cards_from_storyboard(project: dict[str, Any]) -> list[dict[str, Any]]:
    board = project["storyboard"]
    searched = project["research"]["searched_at"][:10]
    style = default_style(board["visual"]["palette"])
    cards = []
    for i, s in enumerate(board["cards"], start=1):
        labels = [x["label"] for x in s["sources"]]
        shown = ", ".join(labels[:2]) + (" 외" if len(labels) > 2 else "")
        cards.append({
            "id": s["id"],
            "order": i,
            "role": s["role"],
            "title": s["title"],
            "key_sentence": s["key_sentence"],
            "body": s["body"],
            "source_line": f"출처: {shown or '없음'} · 조사 기준 {searched}",
            "sources": s["sources"],
            "scene": s["image_plan"]["scene"],
            "style": dict(style),
            "rev": 1,
            "image_count": 0,
            "image": {"status": "pending"},
            "render": {"status": "pending"},
            "history": [],
        })
    return cards


def _answer_label(question: dict[str, Any], answer: Answer) -> str:
    if answer.choice == FREE_TEXT:
        text = (answer.text or "").strip()
        if not question.get("allow_free_text"):
            raise ApiError(400, "invalid_answer", "이 질문은 직접 입력을 받지 않습니다.")
        if not text:
            raise ApiError(400, "invalid_answer", "직접 입력 칸이 비어 있습니다.")
        return text
    for option in question["options"]:
        if option["value"] == answer.choice:
            return option["label"]
    raise ApiError(400, "invalid_answer", "선택지에 없는 답입니다.", "새로고침한 뒤 다시 고르세요.")
