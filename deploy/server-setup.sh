#!/usr/bin/env bash
# Разовая подготовка чистого сервера Ubuntu/Debian: Docker Engine + compose plugin.
# Запуск на сервере:  sudo bash deploy/server-setup.sh
set -euo pipefail

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker и compose уже установлены: $(docker --version)"; exit 0
fi

. /etc/os-release
apt-get update -y
apt-get install -y ca-certificates curl gnupg rsync make git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker   # docker стартует при загрузке -> контейнеры с restart: unless-stopped поднимутся сами

if [ -n "${SUDO_USER:-}" ]; then
  usermod -aG docker "$SUDO_USER"
  echo "Пользователь $SUDO_USER добавлен в группу docker (перелогинься)"
fi
# синхронизация времени: подпись API требует часы в пределах ±1-5 секунд
timedatectl set-ntp true 2>/dev/null || true
docker --version && docker compose version
