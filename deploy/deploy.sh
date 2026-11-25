#!/usr/bin/env bash
# Деплой с локальной машины на сервер по SSH — одной командой.
# На чистом Ubuntu/Debian сервере сам поставит Docker. Python на сервере НЕ нужен (он в образе).
#
#   ./deploy/deploy.sh user@host [путь/к/.env] [папка_на_сервере]
#
#   ./deploy/deploy.sh root@1.2.3.4                        # .env из текущей папки -> ~/gridbot
#   ./deploy/deploy.sh root@1.2.3.4 envs/eth.env gridbot-eth
#   ./deploy/deploy.sh ubuntu@5.6.7.8 envs/btc.env gridbot-btc
set -euo pipefail

HOST="${1:?usage: deploy.sh user@host [env_file] [remote_dir]}"
ENV_FILE="${2:-.env}"
REMOTE_DIR="${3:-gridbot}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "$ENV_FILE" ] || { echo "Нет файла $ENV_FILE (скопируй .env.example и заполни)"; exit 1; }
grep -Eq '^VAR_API_SECRET=[0-9a-fA-Fx]{64,}' "$ENV_FILE" || { echo "В $ENV_FILE не заполнен VAR_API_SECRET"; exit 1; }

echo ">> 1/3 копирую код в $HOST:~/$REMOTE_DIR"
ssh "$HOST" "mkdir -p ~/$REMOTE_DIR"
COPYFILE_DISABLE=1 tar -C "$ROOT" -czf - \
  --exclude .git --exclude .env --exclude 'envs' --exclude data --exclude '__pycache__' --exclude .pytest_cache . \
  | ssh "$HOST" "tar -xzf - -C ~/$REMOTE_DIR"
ssh "$HOST" "umask 077 && cat > ~/$REMOTE_DIR/.env" < "$ENV_FILE"

echo ">> 2/3 ставлю Docker (если его нет)"
ssh -t "$HOST" "cd ~/$REMOTE_DIR && if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then echo 'Docker уже есть'; \
  elif [ \"\$(id -u)\" = 0 ]; then bash deploy/server-setup.sh; else sudo bash deploy/server-setup.sh; fi"

echo ">> 3/3 сборка, проверка ключа и запуск"
ssh -t "$HOST" "cd ~/$REMOTE_DIR && D=docker; \$D info >/dev/null 2>&1 || D='sudo docker'; \
  \$D compose build && \$D compose run --rm gridbot check && \$D compose up -d && \$D compose ps"

echo
echo ">> готово. Логи:  ssh $HOST 'cd ~/$REMOTE_DIR && docker compose logs -f --tail=100'"
