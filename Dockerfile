# 카드뉴스 에이전트 서버 이미지 (리눅스 VM용)
# - 조사·원고·그림은 모두 OpenAI API를 쓴다(OPENAI_API_KEY 필요).
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
CMD ["uv", "run", "--no-sync", "python", "-m", "cardnews", "--host", "0.0.0.0", "--public", "--data-dir", "/data"]
