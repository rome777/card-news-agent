"""Google Gemini Interactions API 실행 엔진 (기본 엔진).

프롬프트·결과 형식(JSON 스키마)·결과 검사는 Claude 엔진(claude_cli.ClaudeCliEngine)과 같고,
호출만 Gemini Interactions API(`POST /v1beta/interactions`)로 한다.
- 웹 조사: 내장 `google_search`(검색)와 `url_context`(페이지 읽기) 도구.
- 결과 모양: `response_format`의 JSON 스키마.
- 질문 뒤 이어가기: interaction ID를 세션 ID로 저장하고 `previous_interaction_id`로 이어 간다.
- 긴 조사는 background로 시작하고 상태를 주기적으로 조회해, 진행 내역(검색어·연 페이지)을 남기고 취소를 받는다.
- 키는 환경 변수 GEMINI_API_KEY(또는 GOOGLE_API_KEY)에서만 읽고, 화면·로그·파일에 남기지 않는다.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import Cancelled, Emit, EngineError
from .claude_cli import TIMEOUT_SECONDS, ClaudeCliEngine, _norm_url

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class GeminiEngine(ClaudeCliEngine):
    name = "gemini"
    is_mock = False

    def __init__(self, model: str = "gemini-3.8-flash", effort: str = "medium"):
        super().__init__(model=model, effort=effort)

    def check(self) -> str | None:
        return None if gemini_key() else "Gemini API 키(환경 변수 GEMINI_API_KEY)가 없습니다."

    def _call(
        self, prompt: str, schema: dict[str, Any], session_id: str | None, work_dir: str, emit: Emit,
        cancel: threading.Event, tools: str,
    ) -> dict[str, Any]:
        try:
            return self._request(prompt, schema, session_id, emit, cancel, tools)
        except EngineError as e:
            if e.code != "session_missing" or not session_id:
                raise
            emit("이전 대화를 찾지 못해, 저장된 선택과 답변으로 새로 시작합니다.")
            return self._request(prompt, schema, None, emit, cancel, tools)

    def _request(
        self, prompt: str, schema: dict[str, Any], session_id: str | None, emit: Emit,
        cancel: threading.Event, tools: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": prompt,
            "store": True,
            "background": True,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": gemini_schema(schema)},
            "generation_config": {"thinking_level": self.effort},
        }
        if tools:
            body["tools"] = [{"type": "google_search"}, {"type": "url_context"}]
        if session_id:
            body["previous_interaction_id"] = session_id
        emit(f"Gemini {self.model} 호출 시작{' (앞 대화 이어가기)' if session_id else ''}")

        interaction = call_api("POST", BASE_URL, body, cancel)
        state: dict[str, Any] = {"seen": 0, "searches": 0, "fetched": set()}
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            _report_steps(interaction, state, emit)
            status = interaction.get("status")
            if status not in ("in_progress", None) or not interaction.get("id"):
                break
            if time.monotonic() > deadline:
                raise EngineError("timeout", f"{TIMEOUT_SECONDS // 60}분 안에 끝나지 않아 멈췄습니다.", "‘다시 시도’를 누르세요.")
            if cancel.wait(2.0):
                raise Cancelled()
            interaction = call_api("GET", f"{BASE_URL}/{interaction['id']}", None, cancel)

        if interaction.get("status") not in ("completed", None):
            errors = interaction.get("errors") or [{}]
            detail = (errors[0] or {}).get("message") or interaction.get("status")
            raise _billing(str(detail)) or EngineError("engine_failed", f"Gemini 호출이 끝나지 못했습니다: {detail}", "‘다시 시도’를 누르세요.")

        text = _output_text(interaction)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise EngineError("invalid_output", "Gemini 결과가 정해진 JSON 형식이 아니어서 받아들이지 않았습니다.", "‘다시 시도’를 누르세요.")
        usage = interaction.get("usage") or {}
        emit(
            f"Gemini 호출 끝 · 입력 {usage.get('total_input_tokens', '?')} / 출력 {usage.get('total_output_tokens', '?')} 토큰"
            f" · 검색 {usage.get('grounding_tool_count', state['searches'])}회"
        )
        return {"data": data, "session_id": interaction.get("id"), "searches": state["searches"], "fetched": state["fetched"]}


def call_api(method: str, url: str, body: dict[str, Any] | None, cancel: threading.Event, timeout: float = 300) -> dict[str, Any]:
    """Gemini API를 부른다. 응답을 기다리는 동안에도 취소를 받는다."""
    key = gemini_key()
    if not key:
        raise EngineError("auth_required", "Gemini API 키(환경 변수 GEMINI_API_KEY)가 없습니다.", "키를 환경 변수에 넣고 서버를 다시 켜세요.")
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    box: dict[str, Any] = {}

    def send() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                box["data"] = json.loads(r.read())
        except urllib.error.HTTPError as e:
            box["http"] = (e.code, e.read().decode("utf-8", "replace"))
        except Exception as e:  # 연결 끊김·시간 초과 등
            box["exc"] = e

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    while worker.is_alive():
        if cancel.wait(0.3):
            raise Cancelled()
    if "http" in box:
        raise classify(*box["http"])
    if "exc" in box:
        raise EngineError("engine_failed", f"Gemini에 연결하지 못했습니다: {box['exc']}", "인터넷 연결을 확인하고 ‘다시 시도’를 누르세요.")
    return box["data"]


def classify(status: int, text: str) -> EngineError:
    try:
        err = json.loads(text).get("error") or {}
    except json.JSONDecodeError:
        err = {}
    message, code = str(err.get("message") or text[:200]), str(err.get("status") or "")
    if status in (401, 403) and ("api key" in message.lower() or code in ("UNAUTHENTICATED", "PERMISSION_DENIED")):
        return EngineError("auth_required", "Gemini API 키가 틀렸거나 권한이 없습니다.", "키를 확인해 다시 넣고 서버를 다시 켜세요.")
    billing = _billing(message)
    if billing:
        return billing
    if status == 429 or code == "RESOURCE_EXHAUSTED":
        return EngineError("rate_limited", "Gemini 요청 한도에 걸렸습니다(429).", "잠시 뒤 ‘다시 시도’를 누르세요.")
    if status == 404 and "interaction" in message.lower():
        return EngineError("session_missing", "저장된 이전 대화를 찾지 못했습니다.", "‘다시 시도’를 누르세요.")
    return EngineError("engine_failed", f"Gemini 호출이 실패했습니다({status}): {message}", "‘다시 시도’를 누르세요.")


def _billing(message: str) -> EngineError | None:
    """결제·한도 문제는 원문(계정 정보가 섞일 수 있음)을 보여 주지 않고 운영자 할 일을 안내한다."""
    low = message.lower()
    if any(k in low for k in ("billing", "spend", "free tier", "quota exceeded", "exceeded your current quota", "prepayment")):
        return EngineError(
            "billing_limit",
            "Gemini 계정의 결제 설정이나 사용 한도 때문에 더 호출할 수 없습니다.",
            "서버 운영자가 Google AI Studio에서 결제·한도 설정을 확인해야 합니다. 그전에는 다시 시도해도 같습니다.",
        )
    return None


def _report_steps(interaction: dict[str, Any], state: dict[str, Any], emit: Emit) -> None:
    steps = interaction.get("steps") or []
    for step in steps[state["seen"]:]:
        kind = step.get("type")
        args = step.get("arguments") or {}
        if kind == "google_search_call":
            queries = args.get("queries") or []
            state["searches"] += max(1, len(queries))
            emit("검색: " + ", ".join(queries) if queries else "검색")
        elif kind == "url_context_call":
            for url in args.get("urls") or []:
                emit(f"원문 읽기: {url}")
        elif kind == "url_context_result":
            for r in step.get("result") or []:
                if r.get("status") == "success" and r.get("url"):
                    state["fetched"].add(_norm_url(r["url"]))
                elif r.get("url"):
                    emit(f"원문을 읽지 못함({r.get('status')}): {r['url']}")
    state["seen"] = len(steps)


def _output_text(interaction: dict[str, Any]) -> str:
    """마지막 model_output의 글. 앞쪽 model_output은 도구를 쓰기 전 중간 글일 수 있다."""
    outputs = [s for s in interaction.get("steps") or [] if s.get("type") == "model_output"]
    for step in reversed(outputs):
        text = "".join(b.get("text") or "" for b in step.get("content") or [] if b.get("type") == "text")
        if text.strip():
            return text
    return ""


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini가 받는 JSON 스키마 부분집합으로 바꾼다: null 허용 타입 배열은 anyOf로, additionalProperties는 뺀다."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items() if k != "additionalProperties"}
            t = node.get("type")
            if isinstance(t, list) and "null" in t:
                rest = [x for x in t if x != "null"]
                return {"anyOf": [{**node, "type": rest[0] if len(rest) == 1 else rest}, {"type": "null"}]}
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(copy.deepcopy(schema))
