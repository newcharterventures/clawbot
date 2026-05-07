# Clawbot

Monthly AI/ML resource auto-post for the NCV Whop community.

Crawls arXiv / Hugging Face Papers / Papers With Code / GitHub Trending → Claude
ranks + writes a community post and X post → Telegram approval flow with
inline buttons → posts to Whop on approve. State in Supabase. Per-call spend
flows into `~/.openclaw/usage/` so the dashboard tracks `project="clawbot"`.

## Layout

```
clawbot/
  .env.example         # secrets template (copy to .env, chmod 600)
  requirements.txt
  clawbot_prepare.py   # cron entry: crawl + draft + Telegram send
  clawbot_webhook.py   # FastAPI webhook: handles button callbacks
  prompts/             # editable prompt source-of-truth
  sql/                 # schema + seed
  systemd/             # webhook service unit
  scripts/
    smoke.py                    # validates env / Supabase / Telegram (no API spend)
    set_telegram_webhook.sh     # registers webhook URL with Telegram
```

## First-time setup

1. **Dedicated Telegram bot** — open `@BotFather`, `/newbot`, save token. Do NOT
   reuse the OpenClaw `Claudbot` bot; two pollers will eat each other's updates.
   Send any message to your new bot from your account so it can DM you back.

2. **Supabase** — create a project. In the SQL editor, run:
   ```bash
   sql/schema.sql      # creates the 3 tables
   sql/seed_prompts.sql  # inserts the ranker + writer prompt rows
   ```

3. **Whop** — forum posting requires a **private Whop App installed on your whop**.
   Company API keys do NOT work for `forum_posts` regardless of role/permission
   configuration (verified empirically against `https://api.whop.com/api/v1/forum_posts`).
   Setup:

   1. Whop dashboard → Developer → Apps → create a new app (it will be marked `Private` by default).
      Note the app id (`app_...`) — this becomes `WHOP_APP_ID`.
   2. App → **Permissions** tab → Add permissions:
      - `forum:post:create` (Required)
      - `forum:read` (Required)
   3. Install the app on your whop. The app must appear in the whop's left-sidebar
      app list — that confirms install succeeded. (The app's iframe will show a 404
      because we never set a `base_url`; that's fine for headless API use.)
   4. App → **API Key** tab → copy the key (`apik_...`) into `WHOP_API_KEY`.
   5. Get the forum's experience id from the Whop dashboard under the forum's
      settings — every forum is an `exp_...`. This becomes `WHOP_FORUM_ID`.

   Verify the wiring:

   ```bash
   curl -sS -X POST "https://api.whop.com/api/v1/forum_posts" \
     -H "Authorization: Bearer ${WHOP_API_KEY}" \
     -H "Content-Type: application/json" \
     -d "{\"experience_id\":\"${WHOP_FORUM_ID}\",\"title\":\"test\",\"content\":\"test\"}"
   ```

   `200` with a `post_...` id means it works. `400 "Actor is missing all required
   permissions: forum:post:create"` means the app isn't installed on the whop, OR
   permissions aren't added on the app, OR you're using a Company API key
   instead of the App API key.

4. **`.env`** —
   ```bash
   cp .env.example .env
   chmod 600 .env
   $EDITOR .env
   ```

5. **venv + deps** —
   ```bash
   cd /home/jae/clawbot
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

6. **Smoke test** —
   ```bash
   .venv/bin/python scripts/smoke.py
   ```
   All checks should pass before continuing.

## Running prepare manually

```bash
.venv/bin/python clawbot_prepare.py
```

Idempotent on `run_date`. A second invocation the same day prints `[skip]` and
exits. To re-run for testing, delete the row in `clawbot_resource_run_log` for
that date first.

## Webhook service

```bash
sudo cp systemd/clawbot-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clawbot-webhook
sudo systemctl status clawbot-webhook
```

The service binds to `127.0.0.1:8090`. Reverse-proxy `WEBHOOK_PUBLIC_URL` →
`http://127.0.0.1:8090/clawbot/callback` via your existing nginx (port 443) or
a Cloudflare Tunnel. Then once:

```bash
scripts/set_telegram_webhook.sh
```

## Cron registration

Use OpenClaw's scheduler so runs show up alongside everything else:

```bash
openclaw cron add \
  --name clawbot-monthly \
  --cron '0 8 1 * *' \
  --tz America/New_York \
  --command '/home/jae/clawbot/.venv/bin/python /home/jae/clawbot/clawbot_prepare.py' \
  --enabled
```

Verify:
```bash
openclaw cron list
openclaw cron run clawbot-monthly      # one-shot dry run
openclaw cron runs --name clawbot-monthly
```

## Costs

Sonnet 4.6 monthly run: ~$0.12 baseline, ~$0.22 worst case with 3 revisions.
~$1.50–$3 per year. All API spend logs to `~/.openclaw/usage/usage.jsonl` under
`project="clawbot"` and is visible in the dashboard.

## Common failures

| Symptom | Cause / fix |
|---|---|
| `[skip] run already exists` | Idempotency working — delete the day's row to re-run. |
| Claude returns non-JSON | Tighten the prompt's schema instructions; the parser tries 3 fallback strategies before giving up. |
| Telegram message > 4096 chars | `send_draft` truncates to 3900. If still too long, post Whop draft as a `sendDocument` attachment instead. |
| Whop POST 401/403 | API key rotated or forum id changed. Test with `curl` directly. |
| Buttons do nothing | Webhook unreachable. `getWebhookInfo` shows the last error. |
| Stuck `pending` for days | You forgot to click. Add a daily reaper that DMs you a reminder. |
