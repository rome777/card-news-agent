"""화면 흐름을 확인하기 위한 모의 엔진. 실제 검색을 하지 않는다.

모든 후보 제목에 [모의]를 붙이고 링크는 example.com으로 둔다. 과거 예시를
실제 최신 검색 결과처럼 보이게 하지 않기 위해서다(PRD 1절).
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import Cancelled, Emit, EngineError, Outcome

# 최근 7일 안에서 찾은 것으로 꾸민 후보. 일부러 7개보다 적게 두어 '기간 넓히기' 안내를 확인한다.
RECENT = [
    {
        "title": "당국 공식 발표 요약",
        "source_type": "공식 발표",
        "source_name": "예시 정부 기관",
        "days_ago": 1,
        "event_days_ago": 1,
        "grouped_count": 1,
        "summary": "당국이 발표한 사건 규모와 대응 방침을 정리한 자료입니다.",
        "value": "사건 규모와 정부 입장을 1차 자료로 확인할 수 있습니다.",
        "uncertainty": "발표 주체의 입장이 담겨 있어 다른 쪽 자료와 대조가 필요합니다.",
    },
    {
        "title": "통신사 종합 기사",
        "source_type": "통신사",
        "source_name": "예시 통신사",
        "days_ago": 2,
        "event_days_ago": 2,
        "grouped_count": 2,
        "summary": "여러 도시의 상황과 양측 입장을 함께 전한 종합 기사입니다.",
        "value": "사건 전개를 날짜별로 정리하기 좋습니다.",
        "uncertainty": "같은 사건을 다룬 기사 2건을 묶었습니다. 수치는 기사마다 다를 수 있습니다.",
    },
    {
        "title": "원인과 배경을 다룬 해설 기사",
        "source_type": "언론",
        "source_name": "예시 일간지",
        "days_ago": 3,
        "event_days_ago": None,
        "grouped_count": 1,
        "summary": "이번 일이 왜 커졌는지 배경과 쟁점을 설명한 해설입니다.",
        "value": "원인과 쟁점을 설명하는 카드의 근거가 됩니다.",
        "uncertainty": "해설 기사라 사건 발생일이 따로 적혀 있지 않습니다(미확인).",
    },
    {
        "title": "참여 단체의 공식 성명",
        "source_type": "단체 성명",
        "source_name": "예시 시민단체",
        "days_ago": 4,
        "event_days_ago": 4,
        "grouped_count": 1,
        "summary": "참여 단체가 요구 사항과 다음 일정을 밝힌 성명입니다.",
        "value": "한쪽 당사자의 요구를 원문으로 확인할 수 있습니다.",
        "uncertainty": "단체의 주장이므로 사실로 단정하지 않고 '주장'으로 표시해야 합니다.",
    },
    {
        "title": "경제·생활 영향 분석",
        "source_type": "언론",
        "source_name": "예시 경제지",
        "days_ago": 5,
        "event_days_ago": 5,
        "grouped_count": 1,
        "summary": "교통·상업 등 생활에 미친 영향을 정리한 기사입니다.",
        "value": "독자에게 '나와 무슨 상관인가'를 보여 줄 수 있습니다.",
        "uncertainty": "추정치가 섞여 있어 출처별 수치를 확인해야 합니다.",
    },
    {
        "title": "출처가 불분명한 블로그 정리 글",
        "source_type": "기타",
        "source_name": "알 수 없음",
        "days_ago": None,
        "event_days_ago": None,
        "grouped_count": 1,
        "summary": "여러 소식을 모아 놓은 글이지만 작성자와 원문 출처가 없습니다.",
        "value": "다른 후보를 찾는 실마리 정도로만 쓸 수 있습니다.",
        "uncertainty": "게시일과 작성자를 확인하지 못했습니다. 카드의 핵심 사실 근거로 쓰지 않습니다.",
    },
]

# 30일로 넓혔을 때 더해지는 후보
OLDER = [
    {
        "title": "첫 대규모 집회 보도",
        "source_type": "통신사",
        "source_name": "예시 통신사",
        "days_ago": 10,
        "event_days_ago": 11,
        "grouped_count": 3,
        "summary": "이번 흐름의 시작으로 꼽히는 첫 대규모 집회를 다룬 기사입니다.",
        "value": "‘어떻게 시작됐나’를 설명하는 카드에 쓸 수 있습니다.",
        "uncertainty": "같은 사건 기사 3건을 묶었습니다.",
    },
    {
        "title": "관련 법안 발표",
        "source_type": "공식 발표",
        "source_name": "예시 정부 기관",
        "days_ago": 16,
        "event_days_ago": 16,
        "grouped_count": 1,
        "summary": "갈등의 계기가 된 정책 발표 원문입니다.",
        "value": "원인을 1차 자료로 확인할 수 있습니다.",
        "uncertainty": "발표 뒤 수정된 내용이 있는지 확인이 필요합니다.",
    },
    {
        "title": "여론조사 결과 보도",
        "source_type": "언론",
        "source_name": "예시 방송사",
        "days_ago": 25,
        "event_days_ago": 26,
        "grouped_count": 1,
        "summary": "이번 사안에 대한 찬반 여론조사 결과를 전한 보도입니다.",
        "value": "여론의 흐름을 보여 줄 수 있습니다.",
        "uncertainty": "조사 기관·표본·오차 범위를 원문에서 확인해야 합니다.",
    },
]

AUDIENCE_QUESTION = {
    "id": "audience",
    "question": "어떤 독자에게 전달할까요?",
    "why": "독자에 따라 카드 순서와 설명의 깊이가 크게 달라집니다.",
    "options": [
        {"value": "general", "label": "일반 독자", "description": "뉴스를 가끔 보는 사람. 배경과 용어 설명이 먼저 필요합니다."},
        {"value": "student", "label": "이 주제를 공부하는 학생", "description": "원인과 쟁점을 구조적으로 정리하는 데 초점을 둡니다."},
        {"value": "practitioner", "label": "관련 업무 종사자", "description": "일정·영향·대응처럼 바로 쓸 정보가 먼저 옵니다."},
    ],
    "recommended": {"value": "general", "reason": "선택한 자료가 배경 설명 위주라 처음 접하는 독자에게 가장 쓸모가 큽니다. (모의 판단)"},
    "multi_select": False,
    "allow_free_text": True,
}

FORMAT_QUESTION = {
    "id": "format",
    "question": "어떤 형식으로 만들까요?",
    "why": "두 형식은 카드 순서가 완전히 달라서 직접 고르는 것이 좋습니다.",
    "options": [
        {"value": "explainer", "label": "해설(이야기)형", "description": "무슨 일 → 왜 → 쟁점 → 대응 → 앞으로. 배경을 이해시키기 좋습니다."},
        {"value": "timeline", "label": "타임라인형", "description": "날짜별 전개. 흐름 정리에는 좋지만 원인 설명은 약해집니다."},
    ],
    "recommended": {"value": "explainer", "reason": "원인과 쟁점을 다룬 자료가 많고 날짜가 미확인인 자료도 있어 해설형이 맞습니다. (모의 판단)"},
    "multi_select": False,
    "allow_free_text": True,
}


class MockEngine:
    name = "mock"
    is_mock = True

    def __init__(self, fail_once: str | None = None, step_seconds: float = 0.7):
        # fail_once: 이 단계(research / deep_research)를 처음 한 번만 실패시켜 재시도 흐름을 확인한다
        self._fail_once = fail_once
        self._step = step_seconds
        self._lock = threading.Lock()

    def execute(self, task: dict[str, Any], session_id: str | None, emit: Emit, cancel: threading.Event) -> Outcome:
        session_id = session_id or f"mock-{uuid.uuid4().hex[:12]}"
        stage = task["stage"]
        with self._lock:
            fail = self._fail_once == stage
            if fail:
                self._fail_once = None
        if fail:
            emit("모의 오류를 내는 중입니다(--mock-fail).")
            self._wait(cancel)
            raise EngineError(
                "search_failed",
                "모의 오류: 검색 호출이 실패했습니다.",
                "‘다시 시도’를 누르세요. 모의 오류는 한 번만 납니다.",
            )
        if stage == "research":
            return Outcome("result", self._research(task, emit, cancel), session_id)
        if stage == "deep_research":
            return self._deep_research(task, emit, cancel, session_id)
        if stage == "storyboard":
            return Outcome("result", self._storyboard(task, emit, cancel), session_id)
        if stage == "revise":
            return Outcome("result", self._revise(task, emit, cancel), session_id)
        if stage == "svg_image":
            emit("도형 그림 SVG 작성 (모의)")
            self._wait(cancel)
            palette = task["visual"]["palette"]
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
                f'<rect width="1024" height="1024" fill="{palette[0]}"/>'
                f'<rect x="120" y="560" width="784" height="320" rx="40" fill="{palette[-1]}"/>'
                f'<circle cx="512" cy="560" r="150" fill="{palette[1 % len(palette)]}"/></svg>'
            )
            return Outcome("result", {"svg": svg}, None)
        raise EngineError("invalid_task", f"모의 엔진이 모르는 단계입니다: {stage}", "개발자에게 알려 주세요.")

    def _storyboard(self, task: dict[str, Any], emit: Emit, cancel: threading.Event) -> dict[str, Any]:
        emit("카드별 기획 작성 (모의)")
        self._wait(cancel)
        topic = task["topic"]
        sources = [{"label": "예시 통신사 10/21", "url": "https://example.com/mock/2"}]
        plan = [
            ("표지", f"[모의] {topic}, 왜 커졌나", "이번 일을 한눈에 소개합니다.", "광장에 모인 군중 실루엣과 깃발"),
            ("무슨 일", "[모의] 지난주 무슨 일이", "당국 발표 기준 참가 규모와 주요 장면을 정리합니다. (당국 발표, 10/22)", "여러 도시를 잇는 지도 위의 점들"),
            ("왜", "[모의] 시작은 생활의 불만", "참여 단체는 요구 사항을 이렇게 밝혔습니다. 단체의 주장입니다.", "저울 위의 지갑과 집 모양"),
            ("쟁점", "[모의] 숫자가 둘인 이유", "같은 시위인데 당국과 단체의 집계가 다릅니다. 둘 다 출처와 함께 봅니다.", "두 개의 다른 높이 막대"),
            ("정부 대응", "[모의] 정부는 무엇을 했나", "당국은 일부 집회를 제한했습니다. 노조는 반발하고 있습니다.", "닫힌 문과 열린 문"),
            ("앞으로", "[모의] 다음 일정은 예정", "다음 집회와 파업이 예정돼 있습니다. 실제 진행 여부는 지켜봐야 합니다.", "달력 위의 동그라미 표시"),
        ]
        cards = [
            {
                "id": f"k{i}",
                "role": role,
                "title": title,
                "key_sentence": body.split(".")[0],
                "body": body,
                "image_plan": {"scene": scene, "purpose": f"{role} 카드의 분위기를 보여 줌"},
                "sources": sources,
            }
            for i, (role, title, body, scene) in enumerate(plan, start=1)
        ]
        note = "모의 스토리보드입니다."
        if task["feedback"]:
            last = task["feedback"][-1]["instruction"]
            cards[1]["body"] = "쉬운 말로 다시 썼습니다. 참가 규모는 당국 발표 기준입니다. (모의)"
            note = f"요청 반영: {last} (모의)"
        return {
            "cards": cards,
            "visual": {"style": "단순한 평면 일러스트, 굵은 면", "palette": ["#1d2b53", "#f4f1ea", "#e4572e", "#ffcc4d"]},
            "note": note,
        }

    def _revise(self, task: dict[str, Any], emit: Emit, cancel: threading.Event) -> dict[str, Any]:
        emit(f"카드 {len(task['cards'])}장 수정 (모의)")
        self._wait(cancel)
        scope = task["scope"]
        wants_image = scope == "image" or (scope == "auto" and "그림" in task["instruction"])
        out = []
        for c in task["cards"]:
            body = c["body"] if scope == "image" else c["body"].split(". ")[0].rstrip(".") + ". (모의 수정)"
            out.append(
                {
                    "id": c["id"],
                    "title": c["title"],
                    "body": body,
                    "needs_new_image": wants_image,
                    "image_scene": f"{c['scene']}, 더 단순하게" if wants_image else None,
                    "change_note": "본문을 한 문장으로 줄임 (모의)" if scope != "image" else "그림만 다시 그림 (모의)",
                }
            )
        return {"cards": out}

    def _wait(self, cancel: threading.Event) -> None:
        if cancel.wait(self._step):
            raise Cancelled()

    def _research(self, task: dict[str, Any], emit: Emit, cancel: threading.Event) -> dict[str, Any]:
        topic, days = task["topic"], task["period_days"]
        now = datetime.fromisoformat(task["now"])
        rows = RECENT + (OLDER if days > 7 else [])

        emit(f"검색어: ‘{topic}’ 최근 {days}일 (모의)")
        self._wait(cancel)
        emit(f"검색 결과 {len(rows) * 2 + 2}건 수신 (모의)")
        self._wait(cancel)
        emit("같은 사건을 다룬 기사 묶기 (모의)")
        self._wait(cancel)
        emit("원문 읽기: https://example.com/mock/2 (모의)")
        self._wait(cancel)

        def day(n: int | None) -> str | None:
            return None if n is None else (now - timedelta(days=n)).date().isoformat()

        candidates = [
            {
                "id": f"c{i}",
                "title": f"[모의] {topic}: {row['title']}",
                "summary": row["summary"],
                "source_type": row["source_type"],
                "source_name": row["source_name"],
                "url": f"https://example.com/mock/{i}",
                "published_at": day(row["days_ago"]),
                "event_date": day(row["event_days_ago"]),
                "grouped_count": row["grouped_count"],
                "value": row["value"],
                "uncertainty": row["uncertainty"],
            }
            for i, row in enumerate(rows, start=1)
        ]
        emit(f"후보 {len(candidates)}개 정리 (모의)")

        notice = None
        if len(candidates) < 7:
            notice = {
                "found": len(candidates),
                "message": f"최근 {days}일 안에서 확인한 후보가 {len(candidates)}개뿐입니다.",
                "can_expand": days < 30,
            }
        return {
            "searched_at": task["now"],
            "timezone": now.strftime("UTC%z")[:-2] + ":" + now.strftime("%z")[-2:],
            "period_days": days,
            "period_start": (now - timedelta(days=days)).date().isoformat(),
            "period_end": now.date().isoformat(),
            "candidates": candidates,
            "notice": notice,
            "mock": True,
        }

    def _deep_research(
        self, task: dict[str, Any], emit: Emit, cancel: threading.Event, session_id: str
    ) -> Outcome:
        answers = {a["question_id"]: a for a in task["answers"]}
        if not answers:
            for c in task["selected"]:
                emit(f"원문 확인: {c['url']} (모의)")
                self._wait(cancel)
            emit("확인한 사실 · 발표 주체의 주장 · 미확인 내용 나누기 (모의)")
            self._wait(cancel)
        else:
            emit("받은 답변을 반영해 이어서 판단 (모의)")
            self._wait(cancel)

        # 이미 답한 질문은 다시 묻지 않는다
        if "audience" not in answers:
            return Outcome("need_input", AUDIENCE_QUESTION, session_id)
        if "format" not in answers:
            return Outcome("need_input", FORMAT_QUESTION, session_id)

        audience = answers["audience"]["answer_label"]
        fmt = answers["format"]["answer_label"]
        emit("독자·형식·표지 문장 정리 (모의)")
        self._wait(cancel)
        selected = task["selected"]
        return Outcome(
            "result",
            {
                "audience": audience,
                "why_useful": f"‘{task['topic']}’ 사안이 왜 커졌는지와 앞으로 무엇을 지켜봐야 하는지 {audience}에게 설명합니다. (모의)",
                "format": fmt,
                "format_comparison": [
                    {"format": "해설(이야기)형", "fits": "원인·쟁점 자료가 많을 때", "weak": "날짜별 흐름은 덜 드러남"},
                    {"format": "타임라인형", "fits": "날짜가 확인된 사건이 여러 개일 때", "weak": "왜 커졌는지 설명이 약함"},
                ],
                "hooks": [
                    f"[모의] {task['topic']}, 왜 이렇게까지 커졌을까?",
                    f"[모의] 뉴스로만 보던 {task['topic']}, 3분 만에 정리",
                ],
                "notes": {
                    "confirmed": [f"{c['source_name']}: {c['summary']}" for c in selected if c["source_type"] in ("공식 발표", "통신사")],
                    "claims": [f"{c['source_name']}: {c['summary']}" for c in selected if c["source_type"] in ("단체 성명", "언론")],
                    "unverified": [c["uncertainty"] for c in selected if c["published_at"] is None or c["event_date"] is None],
                },
                "sources": [{"title": c["title"], "url": c["url"], "published_at": c["published_at"]} for c in selected],
                "mock": True,
            },
            session_id,
        )
