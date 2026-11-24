#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

SERVICE="${SERVICE:-revolution-bot}"
if ! systemctl list-unit-files 2>/dev/null | grep -q "revolution-bot"; then
  if systemctl list-unit-files 2>/dev/null | grep -q "noreply-bot"; then
    SERVICE="noreply-bot"
  fi
fi
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
HEALTH_PORT="${HEALTH_PORT:-8080}"
HEALTH_URL="http://${HEALTH_HOST}:${HEALTH_PORT}"

LOG_FILE="${LOG_FILE:-logs/bot.log}"
[ ! -e "$LOG_FILE" ] && LOG_FILE="bot.log"

has_systemd() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

fail() {
  echo "!! $*" >&2
  if has_systemd; then
    echo "--- service ---" >&2
    systemctl --no-pager --lines=1 status "$SERVICE" 2>&1 | tail -8 >&2 || true
  fi
  echo "--- log ---" >&2
  tail -20 "$LOG_FILE" 2>/dev/null >&2 || true
  exit 1
}

tmux kill-session -t revolution 2>/dev/null || true
tmux kill-session -t noreply 2>/dev/null || true
sleep 1

if has_systemd; then
  if pgrep -f "python -u main.py" >/dev/null 2>&1 && ! systemctl is-active -q "$SERVICE"; then
    echo "!! чужой main.py вне systemd:" >&2
    pgrep -af "main.py" | head -3 >&2
    exit 1
  fi

  echo "--- restart ${SERVICE} (systemd) ---"
  systemctl restart "$SERVICE" || fail "systemctl restart завершился с ошибкой"
else
  echo "--- restart ${SERVICE} (container/direct) ---"
  pkill -f "python.*main.py" 2>/dev/null || true
  sleep 1
  if [ -x ".venv/bin/python" ]; then
    nohup .venv/bin/python -u main.py >> "$LOG_FILE" 2>&1 &
  else
    nohup python3 -u main.py >> "$LOG_FILE" 2>&1 &
  fi
fi

echo "--- waiting for ${HEALTH_URL}/healthz ---"
healthy=0
for _ in $(seq 1 120); do
  if curl -fsS -m 2 "${HEALTH_URL}/healthz" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  if has_systemd && ! systemctl is-active -q "$SERVICE"; then
    fail "сервис упал во время ожидания healthz"
  fi
  sleep 0.5
done
[ "$healthy" = "1" ] || fail "healthz не ответил за 60 секунд"

echo "--- waiting for ${HEALTH_URL}/readyz ---"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS -m 3 "${HEALTH_URL}/readyz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if has_systemd && ! systemctl is-active -q "$SERVICE"; then
    fail "сервис упал во время ожидания readyz"
  fi
  sleep 1
done
[ "$ready" = "1" ] || fail "readyz не ответил за 60 секунд"

if has_systemd; then
  systemctl is-active --quiet "$SERVICE" || fail "сервис не active после рестарта"
  echo "--- service ---"
  systemctl is-active "$SERVICE"
fi
echo "--- healthz ---"
curl -fsS -m 5 "${HEALTH_URL}/healthz"
echo
echo "--- readyz ---"
curl -fsS -m 15 "${HEALTH_URL}/readyz"
echo
echo "--- log ---"
tail -5 "$LOG_FILE" 2>/dev/null || true
