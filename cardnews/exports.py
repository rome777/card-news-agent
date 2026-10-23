"""최종 승인한 결과 버전을 내보내고, 내보낸 파일을 다시 열어 검사한다.

exports/v<결과 버전>/ 아래에 카드 PNG, 원고, 출처 기록, 이미지 사용 기록, 전체 ZIP을 만든다.
검사는 '통과 / 실패 / 확인 못 함'으로 나누고, 사람이 직접 봐야 하는 항목은 통과로 적지 않는다.
"""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from .render import CARD_H, CARD_W
from .store import now_iso


def build(project_dir: Path, project: dict[str, Any]) -> dict[str, Any]:
    version = project["result_version"]
    out = project_dir / "exports" / f"v{version}"
    if out.exists():
        shutil.rmtree(out)
    (out / "cards").mkdir(parents=True)
    cards = sorted(project["cards"], key=lambda c: c["order"])
    r = project["research"]

    files = []
    for i, card in enumerate(cards, start=1):
        name = f"cards/{i:02d}_{card['id']}.png"
        shutil.copyfile(project_dir / card["render"]["file"], out / name)
        files.append(name)

    manuscript = [f"# {project['topic']} 카드뉴스 원고", "", f"- 결과 버전: {version}", f"- 조사 기준 시각: {r['searched_at']} ({r['timezone']})", f"- 조사 기간: {r['period_start']} ~ {r['period_end']}", ""]
    for i, card in enumerate(cards, start=1):
        manuscript += [f"## {i}. {card['title']} ({card['role']})", "", card["body"], "", f"_{card['source_line']}_", ""]
    (out / "manuscript.md").write_text("\n".join(manuscript), encoding="utf-8")

    sources = [f"# 출처 기록", "", f"- 조사 기준 시각: {r['searched_at']} ({r['timezone']})", f"- 조사 기간: {r['period_start']} ~ {r['period_end']} (최근 {r['period_days']}일)", ""]
    for i, card in enumerate(cards, start=1):
        sources.append(f"## 카드 {i}. {card['title']}")
        sources += [f"- [{s['label']}]({s['url']})" for s in card["sources"]] or ["- (출처 없음)"]
        sources.append("")
    editorial = project.get("editorial") or {}
    for title, key in (("확인한 사실", "confirmed"), ("발표 주체의 주장", "claims"), ("아직 확인하지 못한 것", "unverified"), ("자료끼리 어긋나는 점", "conflicts")):
        notes = (editorial.get("notes") or {}).get(key) or []
        if notes:
            sources += [f"## {title}", ""] + [f"- {_note(n)}" for n in notes] + [""]
    (out / "sources.md").write_text("\n".join(sources), encoding="utf-8")

    image_log = {
        "exported_at": now_iso(),
        "note": "그림은 모두 생성한 이미지입니다. 실제 인물·현장 사진이 아닙니다. 사진·스톡 이미지는 쓰지 않았습니다.",
        "cards": [
            {
                "order": i,
                "card_id": c["id"],
                "image_file": c["image"].get("file"),
                "tool": c["image"].get("tool"),
                "tool_description": _tool_label(c["image"].get("tool")),
                "created_at": c["image"].get("created_at"),
                "prompt": c["image"].get("prompt"),
                "agy_conversation_id": c["image"].get("conversation_id"),
                "size": [c["image"].get("width"), c["image"].get("height")],
            }
            for i, c in enumerate(cards, start=1)
        ],
    }
    (out / "image_log.json").write_text(json.dumps(image_log, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_name = f"cardnews_v{version}.zip"
    with zipfile.ZipFile(out / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files + ["manuscript.md", "sources.md", "image_log.json"]:
            z.write(out / name, name)
    base = f"exports/v{version}"
    return {
        "version": version,
        "dir": base,
        "zip": f"{base}/{zip_name}",
        "cards": [f"{base}/{f}" for f in files],
        "documents": [f"{base}/manuscript.md", f"{base}/sources.md", f"{base}/image_log.json"],
        "built_at": now_iso(),
    }


def verify(project_dir: Path, project: dict[str, Any]) -> dict[str, Any]:
    """내보낸 ZIP을 다시 열어 검사한다."""
    final = project.get("final") or {}
    exp = final.get("export")
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    if not exp:
        add("내보낸 파일", "fail", "최종 승인 후 내보낸 파일이 없습니다.")
        return {"checks": checks, "checked_at": now_iso()}

    cards = sorted(project["cards"], key=lambda c: c["order"])
    zip_path = project_dir / exp["zip"]
    try:
        z = zipfile.ZipFile(zip_path)
    except Exception as e:
        add("ZIP 열기", "fail", str(e))
        return {"checks": checks, "checked_at": now_iso()}
    with z:
        names = z.namelist()
        add("ZIP 열기", "pass", f"{zip_path.name} · 파일 {len(names)}개")
        pngs = sorted(n for n in names if n.startswith("cards/") and n.endswith(".png"))
        add("카드 수", "pass" if len(pngs) == len(cards) else "fail", f"PNG {len(pngs)}장 / 승인한 카드 {len(cards)}장")
        expected = [f"cards/{i:02d}_{c['id']}.png" for i, c in enumerate(cards, start=1)]
        add("카드 순서", "pass" if pngs == expected else "fail", ", ".join(pngs))
        for name in pngs:
            try:
                with Image.open(io.BytesIO(z.read(name))) as im:
                    ok = im.format == "PNG" and im.size == (CARD_W, CARD_H)
                    add(f"{name} 이미지", "pass" if ok else "fail", f"{im.format} {im.size[0]}×{im.size[1]}")
            except Exception as e:
                add(f"{name} 이미지", "fail", f"열리지 않음: {e}")
        for i, c in enumerate(cards, start=1):
            warns = c["render"].get("warnings") or []
            add(f"카드 {i} 글자 잘림·대비", "pass" if not warns else "fail", "; ".join(w["message"] for w in warns) or "자동 검사 문제 없음")
        for doc in ("manuscript.md", "sources.md", "image_log.json"):
            add(f"{doc} 포함", "pass" if doc in names else "fail")
        if "sources.md" in names:
            text = z.read("sources.md").decode("utf-8")
            r = project["research"]
            add("조사 기준일 기록", "pass" if r["searched_at"] in text and r["period_start"] in text else "fail")
            missing = [str(i) for i, c in enumerate(cards, start=1) if not c["sources"]]
            add("카드별 출처", "pass" if not missing else "fail", f"출처 없는 카드: {', '.join(missing)}" if missing else "모든 카드에 출처 있음")
        if "image_log.json" in names:
            log = json.loads(z.read("image_log.json"))
            bad = [str(e["order"]) for e in log["cards"] if not (e.get("tool") and e.get("prompt") and e.get("created_at"))]
            add("이미지 생성 기록", "pass" if not bad else "fail", f"기록이 빠진 카드: {', '.join(bad)}" if bad else "모든 카드에 도구·날짜·프롬프트 있음")

    # 자동으로 확인할 수 없는 것은 통과로 적지 않는다
    add("원문 링크가 지금 열리는지", "unknown", "sources.md의 링크를 직접 열어 보세요.")
    add("카드 문장이 원문과 맞는지", "unknown", "핵심 문장 하나 이상을 원문과 대조하세요.")
    add("한글 글꼴·줄바꿈이 자연스러운지", "unknown", "PNG를 휴대폰 크기로 넘겨 보며 확인하세요.")
    add("그림이 내용과 어울리고 오해를 부르지 않는지", "unknown", "생성 그림이 실제 사진처럼 보이지 않는지 확인하세요.")
    return {"checks": checks, "checked_at": now_iso()}


def _note(n: Any) -> str:
    if isinstance(n, str):
        return n
    return f"{n['text']}" + (f" ({n['source_url']})" if n.get("source_url") else "")


def _tool_label(tool: str | None) -> str:
    return {
        "agy": "Antigravity CLI 내장 이미지 도구로 생성한 일러스트",
        "openai": "OpenAI 이미지 API로 생성한 일러스트",
        "svg": "언어 모델이 SVG 코드로 그린 도형 그림(AI 일러스트 아님)",
        "claude-svg": "Claude CLI가 SVG 코드로 그린 도형 그림(AI 일러스트 아님)",
        "mock": "모의 그림(실제 생성 아님)",
    }.get(tool or "", "알 수 없음")
