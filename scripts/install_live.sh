#!/usr/bin/env bash
# install_live.sh — wire Clawbot webhook into nginx + systemd + Telegram.
# Idempotent: re-run safely. Requires sudo.
#
# Usage:  sudo bash /home/jae/clawbot/scripts/install_live.sh

set -euo pipefail

ROOT=/home/jae/clawbot
NGINX_FILE=/etc/nginx/sites-enabled/default
SYSTEMD_UNIT=/etc/systemd/system/clawbot-webhook.service
LOCATION_SNIPPET="$ROOT/nginx/clawbot.location"
TS=$(date +%s)

if [[ $EUID -ne 0 ]]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi

echo "==> 1. Sanity checks"
test -f "$ROOT/.env"            || { echo "missing $ROOT/.env"; exit 1; }
test -f "$ROOT/.venv/bin/uvicorn" || { echo "missing venv (run: cd $ROOT && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"; exit 1; }
test -f "$LOCATION_SNIPPET"     || { echo "missing $LOCATION_SNIPPET"; exit 1; }
test -f "$ROOT/systemd/clawbot-webhook.service" || { echo "missing systemd unit source"; exit 1; }
chmod 600 "$ROOT/.env"

# Load WEBHOOK_PUBLIC_URL + TELEGRAM_BOT_TOKEN for setWebhook step
set -a; source "$ROOT/.env"; set +a
: "${WEBHOOK_PUBLIC_URL:?WEBHOOK_PUBLIC_URL not set in .env}"
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set in .env}"

echo "==> 2. Install systemd unit"
install -m 0644 "$ROOT/systemd/clawbot-webhook.service" "$SYSTEMD_UNIT"
systemctl daemon-reload
systemctl enable --now clawbot-webhook
sleep 2
systemctl is-active --quiet clawbot-webhook || {
  echo "service failed to start. logs:"
  journalctl -u clawbot-webhook -n 40 --no-pager
  exit 1
}
echo "    clawbot-webhook is active"

echo "==> 3. Local healthz probe (bypasses nginx)"
if curl -fsS --max-time 5 http://127.0.0.1:8090/healthz >/dev/null; then
  echo "    127.0.0.1:8090 OK"
else
  echo "    healthz failed locally; check journalctl -u clawbot-webhook" >&2
  exit 1
fi

echo "==> 4. Patch nginx (insert location block before catchall)"
if grep -q '/clawbot/callback' "$NGINX_FILE"; then
  echo "    /clawbot/callback already present, skipping insertion"
else
  cp "$NGINX_FILE" "${NGINX_FILE}.bak.${TS}"
  echo "    backup at ${NGINX_FILE}.bak.${TS}"
  awk -v snip="$LOCATION_SNIPPET" '
    /^[[:space:]]*location \/ \{/ && !done {
      while ((getline line < snip) > 0) print line;
      close(snip);
      done = 1;
    }
    { print }
  ' "$NGINX_FILE" > "${NGINX_FILE}.new"
  mv "${NGINX_FILE}.new" "$NGINX_FILE"
fi

echo "==> 5. nginx -t"
if ! nginx -t 2>&1; then
  echo "nginx config invalid. restoring backup."
  if [[ -f "${NGINX_FILE}.bak.${TS}" ]]; then
    mv "${NGINX_FILE}.bak.${TS}" "$NGINX_FILE"
  fi
  exit 1
fi

echo "==> 6. nginx reload"
systemctl reload nginx

echo "==> 7. Public healthz probe via TLS"
sleep 1
if curl -fsS --max-time 8 "${WEBHOOK_PUBLIC_URL%/clawbot/callback}/clawbot/healthz" >/dev/null; then
  echo "    public healthz OK"
else
  echo "    public healthz failed. Check DNS + cert + nginx routing." >&2
  echo "    Tried: ${WEBHOOK_PUBLIC_URL%/clawbot/callback}/clawbot/healthz" >&2
  exit 1
fi

echo "==> 8. Register Telegram webhook"
RESP=$(curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_PUBLIC_URL}" \
  -d 'allowed_updates=["callback_query","message"]')
echo "    $RESP"

echo "==> 9. Verify with getWebhookInfo"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool

cat <<EOF

==> install complete

Next:
  cd $ROOT && .venv/bin/python clawbot_prepare.py

You should see a draft arrive on Telegram from @Claude_Code_jlee_bot with three
buttons. Tap Reject for the safe first test (no Whop post), or Approve when
you're ready for a real post.

To register the monthly cron afterwards:
  openclaw cron add --name clawbot-monthly --cron '0 8 1 * *' \\
    --tz America/New_York --enabled \\
    --command '$ROOT/.venv/bin/python $ROOT/clawbot_prepare.py'
EOF
