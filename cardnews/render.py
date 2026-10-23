"""카드 렌더러: 글자(HTML/CSS)와 그림을 한 장에 배치하고 Playwright(Chromium)로 PNG를 만든다.

그림에는 글자를 넣지 않고, 제목·본문·출처는 여기서 배치한다. 그래서 글자만 고칠 때는
그림을 다시 만들지 않는다. 글 상자와 그림은 위아래로 나눠 겹치지 않게 놓는다(사용자 요청).
그림의 빈 가장자리는 잘라 내고, 카드 바탕은 그림 바탕색으로 칠해 경계가 보이지 않게 한다.
렌더링한 뒤 같은 브라우저에서 검사한다.
- 잘림: 글 상자 안의 글이 넘치거나 카드 밖으로 나갔는지
- 겹침·크기: 글 상자와 그림이 겹치는지, 글이 길어 그림이 너무 작아졌는지
- 대비: 글자를 숨긴 채 글 상자 영역을 찍어, 글자색과 배경 픽셀의 명암 대비(WCAG 방식)를 잰다
"""
from __future__ import annotations

import base64
import html
import io
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

CARD_W, CARD_H = 1080, 1350
MIN_CONTRAST = 4.5
MIN_ART_PX = 360
# 메모리가 작은 컨테이너(무료 호스팅 512MB 등)에서 Chromium이 덜 쓰게 한다
CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--no-zygote", "--renderer-process-limit=1"]

OVERFLOW_JS = """() => {
  const out = [];
  const card = document.querySelector('.card').getBoundingClientRect();
  for (const el of document.querySelectorAll('[data-check]')) {
    const r = el.getBoundingClientRect();
    if (el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2) out.push({part: el.dataset.check, kind: 'clipped'});
    if (r.right > card.right + 1 || r.bottom > card.bottom + 1 || r.left < card.left - 1 || r.top < card.top - 1) out.push({part: el.dataset.check, kind: 'outside'});
  }
  const img = document.querySelector('.art img');
  if (img) {
    const a = img.getBoundingClientRect(), t = document.querySelector('.textbox').getBoundingClientRect();
    if (a.left < t.right && a.right > t.left && a.top < t.bottom && a.bottom > t.top) out.push({part: 'art', kind: 'overlap'});
    if (Math.min(a.width, a.height) < %d) out.push({part: 'art', kind: 'small'});
  }
  return out;
}""" % MIN_ART_PX

RECT_JS = """(sel) => { const r = document.querySelector(sel).getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; }"""


def default_style(palette: list[str]) -> dict[str, Any]:
    darkest = min(palette, key=_luminance_hex) if palette else "#000000"
    return {
        "position": "top",
        "font_scale": 1.0,
        "text_color": "#ffffff",
        "box_color": darkest,
        # 실제 agy 그림 위에서 0.6은 대비 4.4:1로 기준(4.5:1)에 조금 못 미쳤다
        "box_opacity": 0.75,
    }


def card_html(card: dict[str, Any], image_src: str | None, index: int, total: int, background: str) -> str:
    s = card["style"]
    scale = float(s.get("font_scale", 1.0))
    top = s.get("position", "top") == "top"
    r, g, b = _rgb(s.get("box_color", "#000000"))
    box_bg = f"rgba({r},{g},{b},{float(s.get('box_opacity', 0.6)):.2f})"
    title_px = (88 if index == 1 else 70) * scale
    body_px = 40 * scale
    image = f'<img src="{html.escape(image_src, quote=True)}" alt="">' if image_src else ""
    tool = (card.get("image") or {}).get("tool", "")
    badge = {"agy": "AI 생성 이미지", "openai": "AI 생성 이미지", "svg": "코드로 그린 그림", "claude-svg": "코드로 그린 그림", "mock": "모의 이미지"}.get(tool, "")
    text = f"""<div class="textbox" data-check="text">
  <div class="title">{_text(card['title'])}</div>
  {f'<div class="body">{_text(card["body"])}</div>' if card.get('body') else ''}
</div>"""
    art = f'<div class="art">{image}</div>'
    # 글 위치가 '위'면 글 → 그림, '아래'면 그림 → 글 순서로 쌓는다. 둘은 겹치지 않는다.
    blocks = text + art if top else art + text
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: {CARD_W}px; height: {CARD_H}px; background: {background}; }}
.card {{ width: {CARD_W}px; height: {CARD_H}px; overflow: hidden; background: {background};
  display: flex; flex-direction: column; gap: 28px; padding: 72px 60px 40px;
  font-family: "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", "Noto Sans CJK KR", sans-serif; word-break: keep-all; overflow-wrap: break-word; }}
.textbox {{ flex: none; max-height: 640px; overflow: hidden;
  padding: 44px 50px; border-radius: 28px; background: {box_bg}; color: {s.get('text_color', '#ffffff')}; }}
.title {{ font-size: {title_px:.0f}px; line-height: 1.24; font-weight: 800; letter-spacing: -0.01em; }}
.body {{ font-size: {body_px:.0f}px; line-height: 1.5; font-weight: 500; margin-top: 28px; }}
.art {{ flex: 1 1 0; min-height: 0; display: flex; align-items: center; justify-content: center; }}
.art img {{ display: block; max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 24px; }}
.footer {{ flex: none; display: flex; gap: 20px; justify-content: space-between; align-items: flex-end; font-size: 24px; color: #ffffff; }}
.footer > div {{ background: rgba(0,0,0,0.62); padding: 10px 16px; border-radius: 12px; line-height: 1.4; }}
.src {{ max-width: 780px; max-height: 110px; overflow: hidden; }}
.meta {{ white-space: nowrap; text-align: right; }}
</style></head>
<body><div class="card">
{blocks}
<div class="footer">
  <div class="src" data-check="source">{_text(card.get('source_line', ''))}</div>
  <div class="meta">{badge + '<br>' if badge else ''}{index} / {total}</div>
</div>
</div></body></html>"""


def prepare_image(path: Path) -> tuple[str, str]:
    """그림의 빈 가장자리를 잘라 낸 data URI와, 카드 바탕으로 쓸 그림 바탕색을 돌려준다."""
    with Image.open(path) as src:
        im = src.convert("RGB")
    w, h = im.size
    k = max(4, min(w, h) // 64)
    corners = [im.crop(box).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
               for box in ((0, 0, k, k), (w - k, 0, w, k), (0, h - k, k, h), (w - k, h - k, w, h))]
    bg = tuple(sorted(c[i] for c in corners)[1] for i in range(3))  # 네 모서리의 중간값
    diff = ImageChops.difference(im, Image.new("RGB", im.size, bg)).convert("L").point(lambda p: 255 if p > 24 else 0)
    box = diff.getbbox()
    if box:
        pad = int(min(w, h) * 0.03)
        im = im.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad)))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}", "#%02x%02x%02x" % bg


class Renderer:
    """Playwright는 스레드마다 따로 띄운다. 묶음 하나를 렌더링할 때 브라우저를 한 번만 연다."""

    def render(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        results = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=CHROMIUM_ARGS)
            try:
                page = browser.new_page(viewport={"width": CARD_W, "height": CARD_H}, device_scale_factor=1, java_script_enabled=True)
                for job in jobs:
                    results.append(self._render_one(page, job))
            finally:
                browser.close()
        return results

    def svg_to_png(self, svg: str, out_path: Path, size: int = 1024) -> dict[str, Any]:
        """엔진이 그린 SVG를 PNG로 바꾼다. 스크립트를 끈 페이지에서 렌더링한다."""
        from playwright.sync_api import sync_playwright

        clean = _sanitize_svg(svg)
        page_html = f"""<!doctype html><html><head><style>html,body{{margin:0;width:{size}px;height:{size}px;overflow:hidden}}
svg{{width:{size}px;height:{size}px;display:block}}</style></head><body>{clean}</body></html>"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=CHROMIUM_ARGS)
            try:
                page = browser.new_page(viewport={"width": size, "height": size}, java_script_enabled=False)
                page.set_content(page_html, wait_until="load")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out_path), clip={"x": 0, "y": 0, "width": size, "height": size})
            finally:
                browser.close()
        with Image.open(out_path) as im:
            return {"width": im.width, "height": im.height, "format": im.format}

    def _render_one(self, page, job: dict[str, Any]) -> dict[str, Any]:
        card = job["card"]
        # 바탕색은 카드뉴스 한 편에 하나로 통일한다(그림 가장자리 색을 뽑아 쓰면 카드마다 제각각이 됐다)
        image_src, background = None, job["background"]
        if job.get("image_path"):
            image_src, _ = prepare_image(job["image_path"])
        page.set_content(card_html(card, image_src, job["index"], job["total"], background), wait_until="load")
        page.evaluate("document.fonts.ready.then(() => true)")
        out: Path = job["out_png"]
        out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": CARD_W, "height": CARD_H})

        overflow = page.evaluate(OVERFLOW_JS)
        text_rect = page.evaluate(RECT_JS, ".textbox")
        src_rect = page.evaluate(RECT_JS, ".footer .src")
        # 글자만 숨기고 배경(그림+글 상자)을 찍어 명암 대비를 잰다
        page.add_style_tag(content=".textbox, .textbox *, .footer, .footer * { color: transparent !important; }")
        text_bg = Image.open(io.BytesIO(page.screenshot(clip=text_rect)))
        src_bg = Image.open(io.BytesIO(page.screenshot(clip=src_rect)))
        text_ratio = _worst_contrast(card["style"].get("text_color", "#ffffff"), text_bg)
        src_ratio = _worst_contrast("#ffffff", src_bg)

        warnings = []
        names = {"text": "제목·본문", "source": "출처 줄", "art": "그림"}
        whats = {
            "clipped": "글이 상자를 넘쳐 잘립니다",
            "outside": "카드 밖으로 나갑니다",
            "overlap": "글 상자와 겹칩니다",
            "small": f"글이 길어 그림이 너무 작게 들어갑니다({MIN_ART_PX}px 미만). 본문을 줄이세요",
        }
        for o in overflow:
            warnings.append({"kind": "overflow", "message": f"{names.get(o['part'], o['part'])}: {whats[o['kind']]}"})
        if text_ratio < MIN_CONTRAST:
            warnings.append({"kind": "contrast", "message": f"제목·본문 명암 대비 {text_ratio:.1f}:1 (기준 {MIN_CONTRAST}:1 미만). 글 상자를 진하게 하거나 글자색을 바꾸세요"})
        if src_ratio < MIN_CONTRAST:
            warnings.append({"kind": "contrast", "message": f"출처 줄 명암 대비 {src_ratio:.1f}:1 (기준 미만)"})

        with Image.open(out) as im:
            size = [im.width, im.height]
        return {
            "file": out,
            "size": size,
            "checks": {"overflow": overflow, "contrast": {"text": round(text_ratio, 2), "source": round(src_ratio, 2)}},
            "warnings": warnings,
        }


def _text(s: str) -> str:
    return html.escape(s or "").replace("\n", "<br>")



def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return (0, 0, 0)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _channel(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _luminance_hex(hex_color: str) -> float:
    return _luminance(_rgb(hex_color))


def _worst_contrast(text_hex: str, background: Image.Image) -> float:
    """배경 픽셀 중 대비가 가장 나쁜 10% 지점의 대비. 배경이 고르지 않을 때도 읽히는지 본다."""
    lt = _luminance_hex(text_hex)
    small = background.convert("RGB").resize((64, max(1, int(64 * background.height / max(1, background.width)))))
    ratios = sorted(
        (max(lt, lb) + 0.05) / (min(lt, lb) + 0.05)
        for lb in (_luminance(p) for p in small.getdata())
    )
    return ratios[len(ratios) // 10] if ratios else 21.0


def _sanitize_svg(svg: str) -> str:
    svg = re.sub(r"(?is)<script.*?</script>", "", svg)
    svg = re.sub(r"(?is)<foreignObject.*?</foreignObject>", "", svg)
    svg = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", "", svg)
    svg = re.sub(r"(?i)(href\s*=\s*[\"'])(?!#)[^\"']*([\"'])", r"\1#\2", svg)
    start = svg.find("<svg")
    return svg[start:] if start >= 0 else ""
