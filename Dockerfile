# 카드뉴스 에이전트 서버 이미지 (리눅스 VM용)
# - 조사·원고·그림은 모두 Gemini API를 쓴다(GEMINI_API_KEY 필요).
# - 카드 PNG는 Playwright(Chromium)로 찍고, 한글은 Noto CJK 글꼴로 그린다.
FROM python:3.12-slim

# 조사 기간(최근 7일)과 조사 기준 시각을 한국 시간으로 잡는다
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Seoul

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.7

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev \
    && uv run playwright install --with-deps chromium

COPY cardnews ./cardnews

EXPOSE 8765
# Render 같은 호스팅은 프록시 뒤에서 돌므로 접속 주소를 프록시 헤더에서 읽는다(--trust-proxy).
# 프록시 없이 직접 여는 VM용 docker-compose.yml은 이 옵션을 쓰지 않는다.
CMD ["uv", "run", "--no-sync", "python", "-m", "cardnews", "--host", "0.0.0.0", "--public", "--trust-proxy", "--data-dir", "/data"]
