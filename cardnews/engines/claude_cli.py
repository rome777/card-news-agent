"""Claude CLI(`claude -p`) 실행 엔진.

- 프롬프트는 표준 입력으로 넘기고, 나머지 인자는 배열로 넘긴다. 주제를 명령 문자열에 이어 붙이지 않는다.
- `--output-format stream-json`으로 진행 이벤트(검색어·원문 읽기)를 받아 진행 내역에 남긴다.
- `--json-schema`로 결과 모양을 고정하고, 결과의 structured_output만 받아들인다.
- 질문이 필요하면 엔진이 status=need_input을 내고 호출이 끝난다. 답변이 오면 저장한
  세션 ID로 `--resume`해서 같은 대화를 이어 간다(같은 OS 프로세스가 살아 있는 것은 아님).
- 검색을 한 번도 하지 않은 결과, 형식이 맞지 않는 결과는 완료로 받아들이지 않는다.
- 조사 단계만 웹 도구(WebSearch·WebFetch)를 쓰고, 기획·수정·도형 그림 단계는 도구 없이 실행한다.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import Emit, EngineError, Outcome

WEB_TOOLS = "WebSearch,WebFetch"
TIMEOUT_SECONDS = 15 * 60
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SOURCE_TYPES = ["통신사", "언론", "공식 발표", "단체 성명", "기타"]

_str = {"type": "string"}
_nullable_str = {"type": ["string", "null"]}
_strings = {"type": "array", "items": _str}


def _obj(props: dict[str, Any], nullable: bool = False) -> dict[str, Any]:
    return {
        "type": ["object", "null"] if nullable else "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


RESEARCH_SCHEMA = _obj(
    {
        "status": {"type": "string", "enum": ["ok", "search_failed"]},
        "failure_reason": _nullable_str,
        "candidates": {
            "type": "array",
            "items": _obj(
                {
                    "title": _str,
                    "original_title": _str,
                    "summary": _str,
                    "source_type": {"type": "string", "enum": SOURCE_TYPES},
                    "source_name": _str,
                    "url": _str,
                    "published_at": _nullable_str,
                    "event_date": _nullable_str,
                    "grouped_count": {"type": "integer"},
                    "related_urls": _strings,
                    "value": _str,
                    "uncertainty": _str,
                }
            ),
        },
    }
)

_note = {"type": "array", "items": _obj({"text": _str, "source_url": _nullable_str})}

DEEP_SCHEMA = _obj(
    {
        "status": {"type": "string", "enum": ["need_input", "result"]},
        "question": _obj(
            {
                "id": {"type": "string", "enum": ["audience", "format"]},
                "question": _str,
                "why": _str,
                "options": {"type": "array", "items": _obj({"value": _str, "label": _str, "description": _str})},
                "recommended": _obj({"value": _str, "reason": _str}),
            },
            nullable=True,
        ),
        "result": _obj(
            {
                "audience": _str,
                "why_useful": _str,
                "format": _str,
                "format_reason": _str,
                "format_comparison": {"type": "array", "items": _obj({"format": _str, "fits": _str, "weak": _str})},
                "hooks": _strings,
                "notes": _obj({"confirmed": _note, "claims": _note, "unverified": _note, "conflicts": _note}),
                "sources": {
                    "type": "array",
                    "items": _obj({"title": _str, "url": _str, "published_at": _nullable_str, "read_original": {"type": "boolean"}}),
                },
            },
            nullable=True,
        ),
    }
)

_card_sources = {"type": "array", "items": _obj({"label": _str, "url": _str})}

STORYBOARD_SCHEMA = _obj(
    {
        "cards": {
            "type": "array",
            "items": _obj(
                {
                    "role": _str,
                    "title": _str,
                    "key_sentence": _str,
                    "body": _str,
                    "image_plan": _obj({"scene": _str, "purpose": _str}),
                    "sources": _card_sources,
                }
            ),
        },
        "visual": _obj({"style": _str, "palette": _strings}),
        "note": _str,
    }
)

REVISE_SCHEMA = _obj(
    {
        "cards": {
            "type": "array",
            "items": _obj(
                {
                    "id": _str,
                    "title": _str,
                    "body": _str,
                    "needs_new_image": {"type": "boolean"},
                    "image_scene": _nullable_str,
                    "change_note": _str,
                }
            ),
        }
    }
)

SVG_SCHEMA = _obj({"svg": _str})

# 사용자 피드백(2026-10-23): 출처는 카드 아래 출처 줄에 따로 나오므로 본문에 매체 이름을 되풀이하지 않는다
NO_OUTLET_RULE = (
    "title과 body에 '○○는 보도했습니다', '○○ 보도입니다', '(AP)'처럼 어느 매체가 전했는지 밝히는 문구를 쓰지 않는다. "
    "출처는 카드 아래 출처 줄에 따로 표시된다. 다만 '당국 집계', '정부 주장'처럼 누가 주장한 내용인지는 밝힌다."
)

COMMON_RULES = """공통 규칙
- 모든 설명은 한국어로 쓴다. 원문 제목은 원어 그대로 둔다.
- 출처는 공신력 있는 곳을 우선한다: 통신사(AFP·Reuters·AP 등), 주요 언론, 정부·경찰·노조·단체의 공식 발표. 출처가 불분명한 블로그·콘텐츠 농장·집계 사이트는 핵심 사실의 근거로 쓰지 않는다.
- 확인하지 못한 것은 추측해서 채우지 않는다. 날짜는 확인한 것만 YYYY-MM-DD로 쓰고, 모르면 null로 둔다.
- 기사 제목만으로 사실을 확정하지 않는다. 발표 주체(정부·단체·기업)의 주장을 독립적으로 확인된 사실처럼 쓰지 않는다. 예정된 일을 이미 일어난 일로 쓰지 않는다.
- 수치(참가자·체포자·부상자 수 등)는 누가 발표한 숫자인지와 날짜를 함께 적는다.
- 조사 기준일보다 앞선 날짜의 일을 '예정'으로 쓰지 않는다. 그 일이 실제로 있었는지 확인하지 못했으면 '~할 예정이라고 발표됐다(결과 미확인)'처럼 쓴다.
- 결과는 지정된 JSON 형식으로만 낸다."""


class ClaudeCliEngine:
    name = "claude"
    is_mock = False

    def __init__(self, model: str = "sonnet", effort: str = "medium", executable: str | None = None):
        self.model = model
        self.effort = effort
        self.executable = executable

    # 엔진 규약

    def execute(self, task: dict[str, Any], session_id: str | None, emit: Emit, cancel: threading.Event) -> Outcome:
        if task["stage"] == "research":
            return self._research(task, emit, cancel)
        if task["stage"] == "deep_research":
            return self._deep_research(task, session_id, emit, cancel)
        if task["stage"] == "storyboard":
            return self._storyboard(task, session_id, emit, cancel)
        if task["stage"] == "revise":
            return self._revise(task, session_id, emit, cancel)
        if task["stage"] == "svg_image":
            return self._svg_image(task, emit, cancel)
        raise EngineError("invalid_task", f"모르는 단계입니다: {task['stage']}", "개발자에게 알려 주세요.")

    def check(self) -> str | None:
        """실행 파일을 찾지 못하면 이유를 돌려준다."""
        try:
            self._exe()
        except EngineError as e:
            return e.message
        return None

    # 단계별 작업

    def _research(self, task: dict[str, Any], emit: Emit, cancel: threading.Event) -> Outcome:
        now = datetime.fromisoformat(task["now"])
        days = task["period_days"]
        start = (now - timedelta(days=days)).date()
        end = now.date()
        prompt = f"""너는 카드뉴스 제작을 돕는 조사 담당이다. 아래 주제의 최근 소식을 웹에서 조사해 카드뉴스로 다룰 후보를 만든다.

주제: {task['topic']}
조사 기준 시각: {task['now']}
조사 기간: {start} ~ {end} (최근 {days}일). 이 기간에 게시됐거나 일어난 소식만 후보로 삼는다.

할 일
1. 웹 검색을 여러 번 한다. 한국어와 영어, 그리고 주제와 관련된 나라의 언어로 된 검색어를 섞는다.
2. 서로 다른 소식을 가능하면 7~12개 고른다. 같은 사건을 다룬 기사는 하나로 묶고, 묶은 기사 수를 grouped_count에, 다른 기사 주소를 related_urls에 적는다. 주제의 여러 측면(원인, 당사자 요구, 정부 대응, 영향, 반응 등)이 고루 들어가게 한다.
3. 중요한 후보 2~3개는 원문 페이지를 직접 열어 제목과 날짜를 확인한다.
4. 기간 안에서 확인한 소식이 7개보다 적으면 찾은 만큼만 낸다. 개수를 채우려고 지어내거나 기간 밖 소식을 넣지 않는다.
5. 검색 도구가 실패하거나 결과가 전혀 없으면 status를 search_failed로 하고 failure_reason에 이유를 쓴다.

각 후보
- title: 한국어로 옮긴 제목 / original_title: 원문 제목
- summary: 한두 문장 요약(확인한 사실만)
- source_type: 통신사·언론·공식 발표·단체 성명·기타 중 하나
- published_at: 게시일, event_date: 사건이 일어난 날(둘 다 확인한 경우만, 아니면 null)
- value: 카드뉴스로 다룰 가치 / uncertainty: 확인하지 못한 점, 주장과 사실이 섞인 점, 출처마다 다른 수치 등

{COMMON_RULES}"""
        out = self._call(prompt, RESEARCH_SCHEMA, None, task["work_dir"], emit, cancel, tools=WEB_TOOLS)
        data = out["data"]
        if data["status"] == "search_failed":
            raise EngineError(
                "search_failed",
                f"검색에 실패했습니다: {data.get('failure_reason') or '이유 미상'}",
                "잠시 뒤 ‘다시 시도’를 누르세요. 계속 실패하면 조사 기간을 넓히거나 주제를 바꿔 보세요.",
            )
        if out["searches"] == 0:
            raise EngineError(
                "search_failed",
                "엔진이 실제 웹 검색을 한 번도 하지 않아 결과를 받아들이지 않았습니다.",
                "‘다시 시도’를 누르세요.",
            )
        return Outcome("result", _normalize_research(data, task, start, end, now, out["fetched"]), out["session_id"])

    def _deep_research(self, task: dict[str, Any], session_id: str | None, emit: Emit, cancel: threading.Event) -> Outcome:
        answers = task["answers"]
        answered = [a["question_id"] for a in answers]
        selected = [
            {k: c.get(k) for k in ("title", "original_title", "source_type", "source_name", "url", "related_urls", "published_at", "event_date", "summary", "uncertainty")}
            for c in task["selected"]
        ]
        answer_lines = "\n".join(f"- {a['question']} → {a['answer_label']}" for a in answers) or "- (아직 없음)"
        intro = (
            "사용자가 웹에서 질문에 답했다. 앞서 하던 작업을 이 답을 반영해 이어서 진행한다. 이 세션에서 이미 읽은 원문은 다시 읽지 않아도 된다."
            if answers
            else "너는 카드뉴스 편집 담당이다. 사용자가 고른 소식을 심층 조사하고 편집 방향을 정한다."
        )
        r = task["research"]
        prompt = f"""{intro}

주제: {task['topic']}
조사 기간: {r['period_start']} ~ {r['period_end']} (기준 시각 {r['searched_at']})
사용자가 고른 소식:
{json.dumps(selected, ensure_ascii=False, indent=1)}

사용자가 이미 답한 질문:
{answer_lines}

할 일
1. 고른 소식의 원문 페이지를 직접 열어 읽는다. 필요하면 공식 발표나 다른 통신사 기사를 웹 검색으로 더 찾아 대조한다.
2. 확인한 사실(confirmed), 발표 주체의 주장(claims), 아직 확인하지 못한 것(unverified), 자료끼리 어긋나는 점(conflicts)을 나눈다. 항목마다 근거 URL을 source_url에 적는다.
3. 독자, 독자에게 유용한 이유, 카드 형식, 표지 문장(후킹) 2~3개를 정한다.
   - 형식: 사용자의 가설은 해설(이야기)형(무슨 일 → 왜 → 핵심 쟁점 → 정부의 대응 → 앞으로)이다. 자료에 맞는 대안 형식 하나(예: 타임라인형, 비교형)를 골라 같은 기준으로 비교하고(format_comparison) 하나를 추천한다(format, format_reason).
   - 후킹은 확인한 사실의 범위 안에서만 쓴다. 근거 없는 수치·순위·과장은 쓰지 않는다. '최신', '요즘', '이번 주' 같은 말은 근거 날짜가 조사 기간 안에 있을 때만 쓴다.
   - sources에는 근거로 쓴 자료를 적고, 원문을 직접 읽었으면 read_original을 true로 한다.
4. 묻기 규칙
   - 지금까지의 정보로 판단할 수 있으면 묻지 말고 status=result로 결과를 낸다(question은 null).
   - 독자나 형식에 따라 카드 구성이 크게 달라지는데 판단하기 어려울 때만 status=need_input으로 질문 하나를 낸다(result는 null).
   - 사용자의 형식 가설(해설형)과 다른 형식을 추천하려면, 사용자가 정한 것을 바꾸는 일이므로 format 질문으로 확인한다(format에 아직 답하지 않았을 때). 선택지에 해설형과 추천 형식을 넣고 recommended에 추천 이유를 쓴다.
   - 질문 id는 audience(독자) 또는 format(형식)이다. 이미 답한 질문은 다시 묻지 않는다. 이미 답한 id: {answered or '없음'}
   - 선택지는 2~4개, description에 그 선택을 하면 무엇이 달라지는지 한 줄로 쓴다. recommended에 추천 선택지의 value와 이유를 쓴다.

{COMMON_RULES}"""
        out = self._call(prompt, DEEP_SCHEMA, session_id, task["work_dir"], emit, cancel, tools=WEB_TOOLS)
        data = out["data"]
        if data["status"] == "need_input":
            return Outcome("need_input", _normalize_question(data.get("question"), answered), out["session_id"])
        result = data.get("result")
        if not result:
            raise EngineError("invalid_output", "엔진 결과에 편집 방향이 비어 있습니다.", "‘다시 시도’를 누르세요.")
        # 앞선 호출(넓은 조사)에서 직접 읽은 원문도 원문 확인으로 친다
        read = out["fetched"] | {_norm_url(u) for u in task.get("read_urls", [])}
        for source in result["sources"]:
            source["read_original"] = _was_read(source["url"], read)
        return Outcome("result", result, out["session_id"])

    def _storyboard(self, task: dict[str, Any], session_id: str | None, emit: Emit, cancel: threading.Event) -> Outcome:
        r = task["research"]
        feedback = ""
        if task["feedback"]:
            lines = "\n".join(f"- {f['instruction']}" for f in task["feedback"])
            feedback = f"""
사용자가 이전 스토리보드를 보고 아래 변경을 요청했다. 요청을 반영해 스토리보드 전체를 다시 낸다. 요청과 상관없는 카드는 되도록 그대로 둔다.
{lines}

이전 스토리보드:
{json.dumps(task['previous'], ensure_ascii=False, indent=1)}
"""
        prompt = f"""너는 카드뉴스 기획 담당이다. 앞에서 정한 편집 방향으로 카드별 설계도(스토리보드)를 만든다.

주제: {task['topic']}
조사 기간: {r['period_start']} ~ {r['period_end']} (조사 기준일 {r['searched_at'][:10]})
편집 방향:
{json.dumps(task['editorial'], ensure_ascii=False, indent=1)}
{feedback}
규칙
- 보통 5~8장으로 기획하되 내용에 맞게 정한다. 한 장에는 메시지 하나만 담는다.
- 첫 장은 표지다. 편집 방향의 후킹 문구처럼 확인한 사실 범위에서 관심을 끈다. 마지막 장은 요약과 다음에 지켜볼 일을 담는다.
- 편집 방향의 형식({task['editorial'].get('format', '')})을 따라 카드 순서를 정한다. role에는 '표지', '무슨 일', '왜', '쟁점', '정부 대응', '앞으로', '요약'처럼 그 카드의 역할을 쓴다.
- title은 휴대폰에서 두 줄 안에 들어가게 20자 안팎, body는 두 문장 이하 90자 안팎으로 쓴다. key_sentence는 그 카드가 전할 메시지 한 문장이다.
- 수치는 누가 언제 발표한 숫자인지 body에 함께 쓴다. 예정된 일은 '예정'이라고 쓴다. 단체·정부의 주장은 주장이라고 쓴다. 편집 방향에서 확인하지 못한 것(unverified)은 사실처럼 쓰지 않는다.
- {NO_OUTLET_RULE}
- sources에는 그 카드의 근거 자료를 1~3개 적는다. label은 '매체명 날짜'(예: 'France 24 10/19'), url은 편집 방향의 근거 URL 중에서 고른다.
- image_plan.scene에는 그 카드에 그릴 장면을 구체적으로 쓴다. 실제 인물이나 실제 현장 사진처럼 보이지 않는 상징적인 평면 일러스트로 한다. 글자·숫자·로고·구호는 넣지 않는다. purpose에는 그림이 카드에서 하는 역할을 쓴다.
- 장면 설명에 '희미한', '흐릿한', '번지는', '뿌연' 같은 표현을 쓰지 않는다. 그림은 선명한 평면 일러스트로 그린다.
- 글자가 들어가기 쉬운 물건(날짜를 표시한 달력, 제목 있는 책·법전, 글씨 쓴 표지판·피켓·현수막, 신문, 차량의 'POLICE' 표시)은 장면에 넣지 않는다. 꼭 필요하면 '아무것도 쓰여 있지 않은 빈 책', '빈 피켓'처럼 글자가 없다고 적는다.
- 건물을 그릴 때 다른 나라 건물로 오해받을 모양(예: 돔이 있는 의사당은 미국 국회의사당처럼 보인다)은 쓰지 않는다. 그 나라 건물의 실제 특징을 쓴다(예: 프랑스 국민의회는 돔 없는 기둥 정면 건물).
- 장면에 장소가 나오면 어느 나라·도시인지 분명히 쓰고 그곳의 알아볼 수 있는 상징(예: 프랑스라면 에펠탑·센강)으로 나타낸다. 이름 없는 '도시 지도', 달·별처럼 다른 나라 국기나 종교 상징으로 오해될 수 있는 기호는 쓰지 않는다.
- visual에는 모든 카드가 함께 쓸 그림체(style)와 색 3~4개(palette, '#RRGGBB')를 정한다.

{COMMON_RULES}"""
        # 프롬프트에 편집 방향과 근거가 다 들어 있으므로 긴 조사 세션을 이어 가지 않는다(이어 가면 비용이 몇 배로 든다)
        out = self._call(prompt, STORYBOARD_SCHEMA, None, task["work_dir"], emit, cancel, tools="")
        data = out["data"]
        if not 3 <= len(data["cards"]) <= 10:
            raise EngineError("invalid_output", f"스토리보드 카드 수가 {len(data['cards'])}장이라 받아들이지 않았습니다.", "‘다시 시도’를 누르세요.")
        palette = [c for c in data["visual"]["palette"] if HEX_RE.match(c)]
        data["visual"]["palette"] = palette or ["#1d2b53", "#f4f1ea", "#e4572e"]
        for i, card in enumerate(data["cards"], start=1):
            card["id"] = f"k{i}"
        # 프로젝트의 엔진 세션(조사·답변 맥락)은 그대로 둔다
        return Outcome("result", data, None)

    def _revise(self, task: dict[str, Any], session_id: str | None, emit: Emit, cancel: threading.Event) -> Outcome:
        scope_rule = {
            "text": "글만 고친다. 그림은 그대로 두므로 needs_new_image는 false, image_scene은 null로 한다.",
            "image": "그림만 다시 그린다. title과 body는 지금 그대로 두고, needs_new_image는 true, image_scene에 새 장면을 쓴다.",
            "auto": "요청에 따라 글을 고칠지, 그림을 다시 그릴지 판단한다. 그림을 다시 그려야 할 때만 needs_new_image를 true로 하고 image_scene에 새 장면을 쓴다.",
        }[task["scope"]]
        prompt = f"""너는 카드뉴스 편집 담당이다. 사용자가 완성된 카드 중 일부를 골라 수정을 요청했다. 고른 카드만 고친다.

주제: {task['topic']}
수정 요청: {task['instruction']}
수정 범위: {scope_rule}
고른 카드(지금 내용):
{json.dumps(task['cards'], ensure_ascii=False, indent=1)}

규칙
- 요청한 부분만 고치고 나머지 내용과 사실관계는 유지한다. 근거 자료에 없는 사실을 새로 넣지 않는다.
- {NO_OUTLET_RULE}
- title은 20자 안팎, body는 두 문장 이하 90자 안팎으로 쓴다.
- 고른 카드마다 id를 그대로 돌려주고, change_note에 무엇을 바꿨는지 한 줄로 쓴다.
- image_scene은 실제 인물·현장 사진처럼 보이지 않는 상징적 평면 일러스트 장면으로, 글자 없이 쓴다.

{COMMON_RULES}"""
        out = self._call(prompt, REVISE_SCHEMA, None, task["work_dir"], emit, cancel, tools="")
        wanted = {c["id"] for c in task["cards"]}
        cards = [c for c in out["data"]["cards"] if c["id"] in wanted]
        if {c["id"] for c in cards} != wanted:
            raise EngineError("invalid_output", "엔진이 고른 카드 일부를 돌려주지 않았습니다.", "‘다시 시도’를 누르세요.")
        return Outcome("result", {"cards": cards}, None)

    def _svg_image(self, task: dict[str, Any], emit: Emit, cancel: threading.Event) -> Outcome:
        visual = task["visual"]
        prompt = f"""카드뉴스 한 장에 들어갈 설명용 그림을 SVG 코드로 그려라.

장면: {task['scene']}
그림체: {visual['style']}
색: {', '.join(visual['palette'])}

규칙
- <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">로 시작하는 완전한 SVG 한 개.
- 단순한 도형과 면으로 상징적으로 그린다. 사람은 얼굴 없는 실루엣으로만 그린다.
- 위쪽 1/3은 제목을 올릴 수 있게 배경색만 두고 비운다.
- <text>, 글자, 숫자, 외부 이미지, <script>, 이벤트 속성은 쓰지 않는다.
- 결과는 지정된 JSON 형식으로만 낸다."""
        out = self._call(prompt, SVG_SCHEMA, None, task["work_dir"], emit, cancel, tools="")
        return Outcome("result", {"svg": out["data"]["svg"]}, None)

    # CLI 호출

    def _exe(self) -> str:
        found = self.executable or shutil.which("claude")
        if not found:
            raise EngineError(
                "engine_unavailable",
                "Claude CLI(claude)를 찾을 수 없습니다.",
                "Claude Code를 설치한 뒤 PowerShell 창을 새로 열고 서버를 다시 켜세요.",
            )
        path = Path(found)
        # npm으로 설치하면 claude.cmd/.ps1 껍데기가 잡힌다. 인자 따옴표가 깨지지 않게 실제 exe를 직접 부른다.
        if path.suffix.lower() in (".cmd", ".ps1", ".bat", ""):
            real = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if real.exists():
                return str(real)
        return str(path)

    def _call(
        self, prompt: str, schema: dict[str, Any], session_id: str | None, work_dir: str, emit: Emit,
        cancel: threading.Event, tools: str,
    ) -> dict[str, Any]:
        try:
            return self._call_once(prompt, schema, session_id, work_dir, emit, cancel, tools)
        except EngineError as e:
            if e.code != "session_missing" or not session_id:
                raise
            # 저장한 세션을 찾지 못하면 프롬프트에 담긴 프로젝트 내용으로 새 세션을 연다
            emit("저장된 엔진 세션을 찾지 못해, 저장된 선택과 답변으로 새 세션을 엽니다.")
            return self._call_once(prompt, schema, None, work_dir, emit, cancel, tools)

    def _call_once(
        self, prompt: str, schema: dict[str, Any], session_id: str | None, work_dir: str, emit: Emit,
        cancel: threading.Event, tools: str,
    ) -> dict[str, Any]:
        from ..procs import run_streaming

        args = [
            self._exe(), "-p",
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(schema),
            "--tools", tools,
            "--model", self.model, "--effort", self.effort,
            "--strict-mcp-config",
        ]
        if tools:
            args += ["--allowedTools", tools]
        if session_id:
            args += ["--resume", session_id]

        emit(f"Claude CLI {'세션 이어가기' if session_id else '새 세션'} 시작 ({self.model}, effort {self.effort})")
        state = {"session_id": session_id, "searches": 0, "fetched": set(), "tools": {}, "result": None}
        code, stderr = run_streaming(
            args, Path(work_dir), cancel, TIMEOUT_SECONDS, lambda line: _handle_event(line, state, emit), input_text=prompt
        )

        result = state["result"]
        if result is None:
            raise _classify(stderr or f"종료 코드 {code}", None)
        if result.get("is_error") or result.get("subtype") != "success":
            raise _classify(f"{result.get('result') or ''}\n{stderr}", result.get("api_error_status"))

        data = result.get("structured_output")
        if data is None:
            try:
                data = json.loads(result.get("result") or "")
            except json.JSONDecodeError:
                raise EngineError("invalid_output", "엔진 결과가 정해진 JSON 형식이 아니어서 받아들이지 않았습니다.", "‘다시 시도’를 누르세요.")
        cost = result.get("total_cost_usd")
        if cost is not None:
            emit(f"엔진 호출 끝 · 사용량 약 ${cost:.2f} (API 요금 환산)")
        return {
            "data": data,
            "session_id": result.get("session_id") or state["session_id"],
            "searches": state["searches"],
            "fetched": state["fetched"],
        }


def _handle_event(line: str, state: dict[str, Any], emit: Emit) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        state["session_id"] = ev.get("session_id") or state["session_id"]
    elif kind == "assistant":
        for block in _blocks(ev):
            if block.get("type") != "tool_use":
                continue
            name, data = block.get("name"), block.get("input") or {}
            state["tools"][block.get("id")] = (name, data)
            if name == "WebSearch":
                state["searches"] += 1
                emit(f"검색: {data.get('query', '')}")
            elif name == "WebFetch":
                emit(f"원문 읽기: {data.get('url', '')}")
            elif name == "StructuredOutput":
                emit("결과 정리")
    elif kind == "user":
        for block in _blocks(ev):
            if block.get("type") != "tool_result":
                continue
            name, data = state["tools"].get(block.get("tool_use_id"), (None, {}))
            failed = bool(block.get("is_error"))
            if name == "WebSearch":
                emit("검색 실패: " + _short(block) if failed else "검색 결과 받음")
            elif name == "WebFetch":
                url = data.get("url", "")
                if failed:
                    emit(f"원문을 읽지 못함: {url}")
                else:
                    state["fetched"].add(_norm_url(url))
                    emit(f"원문 받음: {url}")
    elif kind == "result":
        state["result"] = ev


def _blocks(ev: dict[str, Any]) -> list[dict[str, Any]]:
    content = (ev.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _short(block: dict[str, Any]) -> str:
    content = block.get("content")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return text[:120]


def _classify(text: str, api_status: Any) -> EngineError:
    low = text.lower()
    if any(k in low for k in ("not logged in", "/login", "invalid api key", "authentication", "unauthorized", "oauth")) or api_status == 401:
        return EngineError("auth_required", "Claude CLI 로그인이 필요합니다.", "PowerShell에서 claude를 실행해 로그인한 뒤 ‘다시 시도’를 누르세요.")
    if "no conversation found" in low:
        return EngineError("session_missing", "저장된 엔진 세션을 찾지 못했습니다.", "‘다시 시도’를 누르세요.")
    if any(k in low for k in ("rate limit", "usage limit", "429", "overloaded", "quota")) or api_status in (429, 529):
        return EngineError("rate_limited", "Claude 사용 한도에 걸렸거나 서버가 바쁩니다.", "잠시 뒤 ‘다시 시도’를 누르세요.")
    return EngineError("engine_failed", f"Claude CLI가 실패했습니다: {text.strip()[:300]}", "‘다시 시도’를 누르고, 반복되면 서버 창의 오류를 알려 주세요.")


def _normalize_research(
    data: dict[str, Any], task: dict[str, Any], start: date, end: date, now: datetime, fetched: set[str]
) -> dict[str, Any]:
    kept, excluded, seen = [], [], set()
    for c in data["candidates"]:
        url = c["url"].strip()
        if not url.startswith(("http://", "https://")) or _norm_url(url) in seen:
            continue
        seen.add(_norm_url(url))
        for key in ("published_at", "event_date"):
            if c.get(key) and not DATE_RE.match(c[key]):
                c[key] = None
        # 게시일과 사건일이 모두 기간 밖이면 최근 소식이 아니므로 뺀다. 날짜를 모르면 미확인으로 남긴다.
        dates = [date.fromisoformat(c[k]) for k in ("published_at", "event_date") if c.get(k)]
        if dates and all(d < start or d > end for d in dates):
            excluded.append(c["title"])
            continue
        c["url"] = url
        c["read_original"] = _was_read(url, fetched)
        kept.append(c)
    for i, c in enumerate(kept, start=1):
        c["id"] = f"c{i}"

    days = task["period_days"]
    notice = None
    if len(kept) < 7:
        notice = {
            "found": len(kept),
            "message": f"최근 {days}일 안에서 확인한 후보가 {len(kept)}개뿐입니다.",
            "can_expand": days < 30,
        }
    offset = now.strftime("%z")
    return {
        "searched_at": task["now"],
        "timezone": f"UTC{offset[:3]}:{offset[3:]}",
        "period_days": days,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "candidates": kept,
        "excluded": excluded,
        "notice": notice,
        "mock": False,
    }


def _normalize_question(q: dict[str, Any] | None, answered: list[str]) -> dict[str, Any]:
    if not q or len(q.get("options") or []) < 2:
        raise EngineError("invalid_output", "엔진이 낸 질문에 선택지가 부족합니다.", "‘다시 시도’를 누르세요.")
    if q["id"] in answered:
        raise EngineError("invalid_output", "엔진이 이미 답한 질문을 다시 물었습니다.", "‘다시 시도’를 누르세요.")
    values = [o["value"] for o in q["options"]]
    if len(set(values)) != len(values):
        raise EngineError("invalid_output", "엔진이 낸 질문의 선택지가 겹칩니다.", "‘다시 시도’를 누르세요.")
    recommended = q.get("recommended")
    if recommended and recommended.get("value") not in values:
        recommended = None
    return {**q, "recommended": recommended, "multi_select": False, "allow_free_text": True}


def _was_read(url: str, fetched: set[str]) -> bool:
    """직접 연 페이지인지. 같은 기사도 주소 모양이 다를 수 있어(예: AP의 /article/<id>와 /article/<제목>-<id>)
    쿼리를 뗀 주소가 같거나, 연 주소의 마지막 경로(8자 이상)가 후보 주소에 들어 있으면 같은 기사로 본다."""
    target = _norm_url(url).split("?")[0].split("#")[0]
    for f in fetched:
        f = f.split("?")[0].split("#")[0]
        tail = f.rstrip("/").rsplit("/", 1)[-1]
        if f == target or (len(tail) >= 8 and tail in target):
            return True
    return False


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower()
