#!/usr/bin/env bash
# Деплой с локальной машины на сервер по SSH.
#   ./deploy/deploy.sh user@host [путь/к/.env] [папка_на_сервере]
# Примеры:
#   ./deploy/deploy.sh root@1.2.3.4                       # .env из текущей папки -> ~/gridbot
#   ./deploy/deploy.sh root@1.2.3.4 envs/eth.env ~/bot-eth
#   ./deploy/deploy.sh root@5.6.7.8 envs/btc.env ~/bot-btc
set -euo pipefail

HOST="${1:?usage: deploy.sh user@host [env_file] [remote_dir]}"
ENV_FILE="${2:-.env}"
REMOTE_DIR="${3:-gridbot}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "$ENV_FILE" ] || { echo "Нет файла $ENV_FILE (скопируй .env.example и заполни)"; exit 1; }
grep -q '^VAR_API_SECRET=.\+' "$ENV_FILE" || { echo "В $ENV_FILE не заполнен VAR_API_SECRET"; exit 1; }

echo ">> $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR"
rsync -az --delete \
  --exclude .git --exclude .env --exclude 'envs/' --exclude data --exclude '__pycache__' --exclude .pytest_cache \
  "$ROOT"/ "$HOST:$REMOTE_DIR/"
scp -q "$ENV_FILE" "$HOST:$REMOTE_DIR/.env"
ssh "$HOST" "chmod 600 $REMOTE_DIR/.env && cd $REMOTE_DIR && docker compose run --rm --build gridbot check && docker compose up -d --build && docker compose ps"
echo ">> готово. Логи: ssh $HOST 'cd $REMOTE_DIR && docker compose logs -f --tail=100'"
