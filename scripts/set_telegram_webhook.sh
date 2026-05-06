#!/usr/bin/env bash
# One-shot: register the webhook URL with Telegram. Run AFTER nginx/Cloudflare
# Tunnel terminates HTTPS for $WEBHOOK_PUBLIC_URL and routes to localhost:8090.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${TELEGRAM_BOT_TOKEN:?need token}"
: "${WEBHOOK_PUBLIC_URL:?need public URL}"
curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_PUBLIC_URL}" \
  -d 'allowed_updates=["callback_query","message"]'
echo
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
