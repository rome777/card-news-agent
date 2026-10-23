"""그림 작업자.

- AgyImageMaker: Antigravity CLI(agy)의 내장 이미지 도구에 자연어로 요청한다. `agy image generate` 같은
  확인하지 않은 명령은 쓰지 않는다. agy는 "SUCCESS"여도 그림을 작업 폴더가 아니라
  ~/.gemini/antigravity-cli/brain/<conversation_id>/ 에 JPG로 저장하므로 그 폴더에서 새 파일을 찾는다.
  응답 JSON(형식)과 그림 파일(PNG/JPG)을 혼동하지 않도록, 파일이 실제로 열리는 이미지인지 검사한다.
- OpenAIImageMaker: OpenAI 이미지 API(`/v1/images/generations`)를 직접 부른다. 키는 환경 변수
  OPENAI_API_KEY에서만 읽고, 화면·로그·파일에 남기지 않는다.
- MockImageMaker: 화면 흐름 확인용. Pillow로 단순한 도형 그림을 만든다.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .engines import Cancelled, Emit, EngineError
from .procs import run_streaming

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MIN_SIDE = 256


def build_prompt(scene: str, visual: dict[str, Any], position: str, tool: str = "agy", topic: str = "") -> str:
    # 카드는 글 상자와 그림을 나눠 배치하고 그림의 빈 가장자리를 잘라 쓰므로(render.prepare_image),
    # 바탕은 한 가지 색으로 두고 그림 요소를 가운데에 모아 달라고 한다. position은 쓰지 않는다.
    # 2026-10-23 '프랑스 시위' 카드에 서울처럼 보이는 지도와 초승달 국기, 미국 국회의사당 같은 건물이
    # 그려진 일이 있어, 주제를 알려 주고 다른 나라로 보이는 요소를 금지한다.
    describe = (
        f"카드뉴스용 설명 일러스트 한 장. 카드뉴스 주제: {topic or '(주제 미상)'}. 장면: {scene}. "
        f"그림체: {visual.get('style', '단순한 평면 일러스트')}. 색: {', '.join(visual.get('palette', []))}. "
        "실제 인물·실제 현장 사진처럼 보이지 않는 상징적인 평면 일러스트로 그린다. "
        "사람은 얼굴 없는 실루엣으로만 그리고 눈·코·입을 그리지 않는다. "
        "주제와 다른 나라나 도시로 보일 수 있는 지도·국기·문장·건물(예: 미국 국회의사당)과 종교 상징(초승달·별·십자가 등)은 넣지 않는다. "
        "장소를 나타내야 하면 주제 속 장소의 알아볼 수 있는 상징만 쓴다(예: 프랑스라면 에펠탑·센강·프랑스 삼색기). "
        "흐림·빛 번짐(glow)·그라데이션·비네팅·부드러운 그림자 효과 없이, 선명한 단색 면과 또렷한 윤곽선으로만 그린다. "
        "바탕은 무늬 없는 한 가지 색으로 하고, 그림 요소는 가운데에 모아 가장자리에 바탕색 여백을 둔다. "
        "이미지 안에 글자·숫자·로고·구호는 넣지 않는다. 정사각형."
    )
    if tool == "agy":
        return f"내장 generate_image 도구로 다음 그림을 만들어 줘. {describe} 만든 이미지 파일의 실제 저장 경로를 알려 줘."
    return describe


class AgyImageMaker:
    name = "agy"

    def __init__(self, executable: str | None = None, brain_dir: Path | None = None, timeout: float = 7 * 60):
        self.executable = executable
        self.brain_dir = brain_dir or Path.home() / ".gemini" / "antigravity-cli" / "brain"
        self.timeout = timeout

    def check(self) -> str | None:
        try:
            self._exe()
        except EngineError as e:
            return e.message
        return None

    def _exe(self) -> str:
        found = self.executable or shutil.which("agy")
        if not found:
            local = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
            found = str(local) if local.exists() else None
        if not found:
            raise EngineError("engine_unavailable", "Antigravity CLI(agy)를 찾을 수 없습니다.", "agy를 설치하고 PowerShell 창을 새로 연 뒤 서버를 다시 켜세요.")
        return found

    def generate(self, prompt: str, out_base: Path, work_dir: Path, emit: Emit, cancel: threading.Event) -> dict[str, Any]:
        started = time.time()
        lines: list[str] = []
        emit("agy에 그림 요청")
        code, stderr = run_streaming(
            [self._exe(), "-p", prompt, "--output-format", "json", "--print-timeout", f"{int(self.timeout) - 30}s"],
            work_dir, cancel, self.timeout, lines.append,
        )
        raw = "".join(lines)
        # 원인을 나중에 확인할 수 있게 마지막 응답을 남긴다
        (work_dir / "agy_last_output.json").write_text(raw or stderr, encoding="utf-8")
        data = _parse_json(raw)
        if data is None or data.get("status") != "SUCCESS":
            raise _classify(f"{raw}\n{stderr}", code)

        conversation = data.get("conversation_id")
        found = self._find_new_image(conversation, started, data.get("response") or "")
        if found is None:
            # agy는 하위 작업자가 429로 실패해도 겉 상태를 SUCCESS로 낸다. 이번 호출의 기록에서 원인을 찾는다.
            hint = f"{data.get('response') or ''}\n{self._transcripts_since(started)}"
            error = _classify(hint, code)
            if error.code != "image_failed":
                raise error
            raise EngineError(
                "image_missing",
                "agy가 성공이라고 응답했지만 새로 생긴 그림 파일을 찾지 못했습니다.",
                "‘다시 만들기’를 누르세요.",
            )
        info = _verify_image(found)
        out = out_base.with_suffix(found.suffix.lower())
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(found, out)
        emit(f"그림 파일 확인: {found.name} ({info['width']}×{info['height']} {info['format']})")
        return {"file": out, "conversation_id": conversation, "original_path": str(found), **info}

    def _find_new_image(self, conversation: str | None, since: float, response: str) -> Path | None:
        candidates: list[Path] = []
        if conversation:
            folder = self.brain_dir / conversation
            if folder.is_dir():
                candidates += [p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES and ".system_generated" not in p.parts]
        # 응답에 적힌 경로도 확인한다 (file:///C:/... 또는 C:\...)
        for m in re.finditer(r"(?:file:///)?([A-Za-z]:[\\/][^\s`<>\"')\]]+?\.(?:jpe?g|png|webp))", response, re.IGNORECASE):
            candidates.append(Path(m.group(1).replace("/", os.sep)))
        fresh = [p for p in candidates if p.is_file() and p.stat().st_mtime >= since - 2]
        return max(fresh, key=lambda p: p.stat().st_mtime) if fresh else None

    def _transcripts_since(self, since: float) -> str:
        """이번 호출 동안 agy가 남긴 대화 기록(하위 작업자 포함)의 오류 줄만 모은다."""
        if not self.brain_dir.is_dir():
            return ""
        out = []
        for path in self.brain_dir.glob("*/.system_generated/logs/transcript.jsonl"):
            if path.stat().st_mtime < since - 2:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out += [line[:400] for line in text.splitlines() if '"ERROR"' in line or "error" in line.lower()]
        return "\n".join(out[-20:])


class OpenAIImageMaker:
    """OpenAI 이미지 API. gpt-image 계열은 결과를 base64로 돌려준다."""

    name = "openai"
    URL = "https://api.openai.com/v1/images/generations"

    def __init__(self, model: str = "gpt-image-1.5", quality: str = "medium", timeout: float = 240):
        self.model = model
        self.quality = quality
        self.timeout = timeout

    def check(self) -> str | None:
        if not os.environ.get("OPENAI_API_KEY"):
            return "OpenAI API 키(환경 변수 OPENAI_API_KEY)가 없습니다."
        return None

    def generate(self, prompt: str, out_base: Path, work_dir: Path, emit: Emit, cancel: threading.Event) -> dict[str, Any]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise EngineError("auth_required", "OpenAI API 키(환경 변수 OPENAI_API_KEY)가 없습니다.", "키를 사용자 환경 변수에 넣고 PowerShell 창을 새로 연 뒤 서버를 다시 켜세요.")
        body = json.dumps({
            "model": self.model, "prompt": prompt, "n": 1, "size": "1024x1024",
            "quality": self.quality, "output_format": "png",
        }).encode()
        request = urllib.request.Request(
            self.URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        emit(f"OpenAI에 그림 요청 ({self.model}, {self.quality})")

        # 응답을 기다리는 동안에도 취소를 받을 수 있게 요청은 별도 스레드에서 보낸다
        box: dict[str, Any] = {}

        def send() -> None:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as r:
                    box["data"] = json.loads(r.read())
            except urllib.error.HTTPError as e:
                box["http"] = (e.code, e.read().decode("utf-8", "replace"))
            except Exception as e:  # 네트워크 오류 등
                box["exc"] = e

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        while worker.is_alive():
            if cancel.wait(0.3):
                raise Cancelled()
        if "http" in box:
            raise _classify_openai(*box["http"])
        if "exc" in box:
            raise EngineError("image_failed", f"OpenAI에 연결하지 못했습니다: {box['exc']}", "인터넷 연결을 확인하고 ‘다시 만들기’를 누르세요.")

        item = (box["data"].get("data") or [{}])[0]
        if not item.get("b64_json"):
            raise EngineError("image_missing", "OpenAI 응답에 그림이 없습니다.", "‘다시 만들기’를 누르세요.")
        out = out_base.with_suffix(".png")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(item["b64_json"]))
        info = _verify_image(out)
        usage = box["data"].get("usage") or {}
        emit(f"그림 파일 확인: {out.name} ({info['width']}×{info['height']} {info['format']}) · 출력 토큰 {usage.get('output_tokens', '?')}")
        return {"file": out, "conversation_id": None, "original_path": None, **info}


def _classify_openai(status: int, text: str) -> EngineError:
    try:
        err = json.loads(text).get("error") or {}
    except json.JSONDecodeError:
        err = {}
    code, message = str(err.get("code") or ""), str(err.get("message") or text[:200])
    if status == 401:
        return EngineError("auth_required", "OpenAI API 키가 틀렸거나 만료됐습니다.", "키를 확인해 환경 변수에 다시 넣고 서버를 다시 켜세요.")
    if code == "insufficient_quota" or "billing" in message.lower() or "spend limit" in message.lower():
        return EngineError(
            "billing_limit",
            "OpenAI 계정의 크레딧이나 지출 한도에 도달해 그림을 만들 수 없습니다.",
            "서버 운영자가 OpenAI 대시보드에서 한도나 결제 설정을 바꿔야 합니다.",
        )
    if status == 429:
        return EngineError("rate_limited", "OpenAI 요청 한도에 걸렸습니다(429).", "잠시 뒤 ‘다시 만들기’를 누르세요.")
    if status == 403 or "verif" in message.lower():
        return EngineError("permission_denied", f"이 키로는 이 이미지 모델을 쓸 수 없습니다: {message}", "OpenAI 조직 인증이나 모델 권한을 확인하세요.")
    if code in ("moderation_blocked", "content_policy_violation") or "safety" in message.lower():
        return EngineError("image_blocked", "OpenAI 안전 기준에 걸려 그림을 만들지 않았습니다.", "‘골라서 고치기’에서 그림 장면을 더 상징적으로 바꿔 다시 만드세요.")
    return EngineError("image_failed", f"OpenAI 그림 생성이 실패했습니다({status}): {message}", "‘다시 만들기’를 누르세요.")


class MockImageMaker:
    """실제 생성 없이 Pillow로 그림을 만든다. fail_once='images'면 첫 요청을 할당량 초과로 실패시킨다."""

    name = "mock"

    def __init__(self, fail_once: bool = False, step_seconds: float = 0.6):
        self._fail_once = fail_once
        self._step = step_seconds
        self._lock = threading.Lock()

    def check(self) -> str | None:
        return None

    def generate(self, prompt: str, out_base: Path, work_dir: Path, emit: Emit, cancel: threading.Event) -> dict[str, Any]:
        emit("모의 그림 요청")
        if cancel.wait(self._step):
            raise Cancelled()
        with self._lock:
            fail, self._fail_once = self._fail_once, False
        if fail:
            raise EngineError("rate_limited", "모의 오류: 이미지 할당량을 넘었습니다(429 RESOURCE_EXHAUSTED).", "‘다시 만들기’ 또는 ‘코드 도형 그림으로’를 누르세요.")
        palette = re.findall(r"#[0-9a-fA-F]{6}", prompt) or ["#1d2b53", "#f4f1ea", "#e4572e"]
        rnd = random.Random(prompt)
        im = Image.new("RGB", (1024, 1024), palette[0])
        draw = ImageDraw.Draw(im)
        for _ in range(9):
            x, y, r = rnd.randint(0, 1024), rnd.randint(380, 1024), rnd.randint(60, 220)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=rnd.choice(palette[1:] or palette))
        out = out_base.with_suffix(".png")
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out)
        return {"file": out, "conversation_id": None, "original_path": None, **_verify_image(out)}


def _parse_json(raw: str) -> dict[str, Any] | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _classify(text: str, code: int) -> EngineError:
    low = text.lower()
    if any(k in low for k in ("resource_exhausted", "429 too many", '"code": 429', "quota", "rate limit", "exhausted your capacity", "할당량")):
        reset = re.search(r"reset after ([0-9hms]+)", text)
        when = f" 한도는 {reset.group(1)} 뒤에 풀린다고 합니다." if reset else ""
        return EngineError(
            "rate_limited",
            f"agy 이미지 한도(할당량)를 넘었습니다(429 RESOURCE_EXHAUSTED).{when}",
            "한도가 풀린 뒤 ‘다시 만들기’를 누르거나, ‘코드 도형 그림으로’를 누르세요.",
        )
    if any(k in low for k in ("login", "unauthenticated", "authenticat", "sign in", "credentials")):
        return EngineError("auth_required", "agy 로그인이 필요합니다.", "PowerShell에서 agy를 실행해 로그인한 뒤 ‘다시 만들기’를 누르세요.")
    if any(k in low for k in ("permission", "denied", "not allowed")):
        return EngineError("permission_denied", "agy가 이미지 도구 권한을 받지 못했습니다.", "대화형 agy에서 이미지 도구를 허용한 뒤 ‘다시 만들기’를 누르세요.")
    tail = text.strip()[-300:] or f"종료 코드 {code}"
    return EngineError("image_failed", f"agy 그림 생성이 실패했습니다: {tail}", "‘다시 만들기’를 누르세요.")


def _verify_image(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            info = {"width": im.width, "height": im.height, "format": im.format}
    except Exception as e:
        raise EngineError("image_invalid", f"그림 파일이 이미지로 열리지 않습니다: {e}", "‘다시 만들기’를 누르세요.")
    if min(info["width"], info["height"]) < MIN_SIDE:
        raise EngineError("image_invalid", f"그림이 너무 작습니다({info['width']}×{info['height']}).", "‘다시 만들기’를 누르세요.")
    return info
