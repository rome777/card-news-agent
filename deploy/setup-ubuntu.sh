#!/usr/bin/env bash
# 우분투(22.04/24.04) VM에서 실행한다: Docker 설치 → 저장소 받기 → .env 확인 → 컨테이너 실행
# 사용: bash setup-ubuntu.sh [저장소 주소]
# 다시 실행하면 최신 코드로 갱신해 다시 띄운다.
set -euo pipefail

REPO_URL="${1:-https://github.com/rome777/card-news-agent.git}"
APP_DIR="$HOME/card-news-agent"

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2 git
  sudo systemctl enable --now docker
fi

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git pull --ff-only

if [ ! -s .env ] || ! grep -q '^GEMINI_API_KEY=.\+' .env; then
  cp -n .env.example .env || true
  chmod 600 .env
  echo ".env에 Gemini API 키가 없습니다. 아래 파일을 열어 GEMINI_API_KEY= 뒤에 키를 넣고 이 스크립트를 다시 실행하세요."
  echo "  nano $APP_DIR/.env"
  exit 1
fi

mkdir -p data
sudo docker compose up -d --build
sudo docker compose ps
echo "브라우저에서 http://<이 서버의 공인 IP>/ 를 여세요. 클라우드 방화벽(보안 그룹)에서 80번 포트를 열어야 합니다."
