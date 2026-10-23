"""엔진이 낸 기사 주소가 실제로 있는지 확인한다.

검색 엔진 모델이 검색 결과의 주소가 아니라 기억으로 주소를 지어내는 일이 있다(2026-10-23 Gemini 시험에서
후보 8개 중 5개의 주소가 404·410). 확실히 없는 주소(404·410·없는 도메인)만 죽은 주소로 본다.
403·429·시간 초과는 로봇을 막는 사이트일 수 있어 살아 있는 것으로 둔다.
"""
from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

DEAD_STATUS = {404, 410}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def is_dead(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return True
    target = url
    host = urllib.parse.urlsplit(url).hostname or ""
    # 유튜브는 없는 영상도 200을 주므로 oEmbed로 확인한다
    if host.endswith("youtube.com") or host == "youtu.be":
        target = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
    request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=12):
            return False
    except urllib.error.HTTPError as e:
        return e.code in DEAD_STATUS or (target != url and e.code in (400, 401))
    except urllib.error.URLError as e:
        return isinstance(e.reason, socket.gaierror)
    except Exception:
        return False


def dead_links(urls: Iterable[str], cancel: threading.Event) -> set[str]:
    todo = sorted({u for u in urls if u})
    if not todo:
        return set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        flags = list(pool.map(lambda u: False if cancel.is_set() else is_dead(u), todo))
    return {u for u, dead in zip(todo, flags) if dead}
