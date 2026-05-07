# Clawbot

Monthly AI/ML resource auto-post for the NCV Whop community.

Crawls arXiv / Hugging Face Papers / Papers With Code / GitHub Trending → Claude
ranks + writes a community post and X post → Telegram approval flow with
inline buttons → posts to Whop on approve. State in Supabase. Per-call spend
flows into `~/.openclaw/usage/` so the dashboard tracks `project="clawbot"`.

## Architecture

### Why it exists

NCV runs a paid AI/ML community on Whop. Members expect fresh, technically
substantive resources every month. Hand-curating that takes hours of triage
across arXiv, HF Papers, PwC, and GitHub Trending; skipping a month erodes the
paid-community thesis. Clawbot automates the triage + drafting (Claude does the
ranking and the writing in NCV's voice) but keeps a human (Jae) in the loop for
the publish decision via a one-tap Telegram approval. Result: a monthly post
goes out reliably with ~30 seconds of human attention.

### Data flow (one monthly run)

```
                       cron (1st of month, 08:00 ET)
                                  │
                                  ▼
                        clawbot_prepare.py
              ┌───────────────────┼───────────────────┐
              │                   │                   │
              ▼                   ▼                   ▼
        crawl 4 sources    GET prompt rows     GET last 2 runs
        (arXiv / HF /      from Supabase       (selected_urls
         PwC / GitHub)                          for de-dup)
              │                   │                   │
              └─────────┬─────────┴─────────┬─────────┘
                        ▼                   │
                 dedup by URL                │
                        │                   │
                        └────────┬──────────┘
                                 ▼
                        Claude (ranker)
                        → top resources JSON
                                 │
                                 ▼
                        Claude (writer)
                        → {whop_post, x_post, resources}
                                 │
                                 ▼
                  INSERT clawbot_resource_run_log (pending)
                                 │
                                 ▼
                  Telegram sendMessage to Jae
                  with [Approve] [Edit] [Reject] buttons
                                 │
                                 ▼
                  INSERT clawbot_pending_approval (waiting)
                                 │
                  ─── prepare exits ─── (cron job done)


                        Jae taps a button
                                 │
                                 ▼
                  Telegram POST → nginx 443
                                 │
                                 ▼
              clawbot_webhook.py  /clawbot/callback
              (FastAPI on 127.0.0.1:8090, systemd-managed)
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
          Approve              Edit              Reject
              │                  │                  │
              ▼                  ▼                  ▼
    POST api.whop.com   prompt for notes,    UPDATE status
    /api/v1/forum_posts revise via Claude,   = rejected
    via App API key     re-send draft
              │
              ▼
    UPDATE status = approved,
    whop_post_id = post_...
```

### Components

| Component | Role | Where it runs |
|---|---|---|
| `clawbot_prepare.py` | Monthly cron entry. Crawls, calls Claude twice, writes to Supabase, sends Telegram approval message, exits. | Server (Linux, jae user). Triggered by `openclaw cron`. |
| `clawbot_webhook.py` | FastAPI service. Handles Telegram button callbacks (approve/edit/reject) and edit-notes free-text replies. Posts to Whop on approve. | systemd unit `clawbot-webhook.service`, bound to `127.0.0.1:8090`. |
| Supabase (Postgres + REST) | System of record. Stores prompt templates, run history, pending-approval state. Queried via PostgREST with the service-role key. | Hosted (Supabase cloud). |
| Anthropic Claude (Sonnet 4.6) | Two calls per run: rank crawled items, then write the post. One additional call per `Edit` revision. | Anthropic API. Wrapped by `LoggedAnthropic` (per-call spend logged to `~/.openclaw/usage/usage.jsonl`). |
| Telegram Bot API | Approval UI surface. Inline-keyboard buttons for one-tap approval; free-text reply for edit notes. Bot is dedicated to Clawbot (do not reuse OpenClaw's bot — two pollers fight). | api.telegram.org. Webhook delivery to our public URL. |
| Whop API (`/api/v1/forum_posts`) | Posting target. Authenticated as a **private Whop App** installed on NCV with `forum:post:create` permission. App API key (`apik_...`) on the wire as `Authorization: Bearer`. | api.whop.com. |
| nginx (existing on this server) | TLS terminator for the Telegram webhook. Reverse-proxies `https://newcharterventures.com/clawbot/callback` → `http://127.0.0.1:8090/clawbot/callback`. | Same server. Snippet in `nginx/clawbot.location`. |
| OpenClaw scheduler | Cron runner that already manages Jae's other recurring jobs. Clawbot registers `0 8 1 * *` America/New_York. | Same server. |
| Per-call usage logger | `~/.openclaw/usage/usage_log.py` wraps every Anthropic call and appends a JSONL row tagged `project="clawbot"` so spend shows up in the unified dashboard. | Same server. Imported by `clawbot_prepare.py` and `clawbot_webhook.py`. |

### Data model (Supabase, 3 tables)

| Table | Purpose | Key columns |
|---|---|---|
| `clawbot_prompt_templates` | Editable source-of-truth for Claude prompts. Two rows: `resource_ranker`, `content_writer`. Each has `system_prompt` + `user_template`. Editing the row changes behavior on next run — no redeploy. | `key` (unique), `system_prompt`, `user_template`, `model`, `max_tokens` |
| `clawbot_resource_run_log` | One row per monthly run. Holds the draft, the URLs that were selected, approval status, and (on success) the resulting Whop post id. The `selected_urls` array is also read by the next run for de-duplication (don't recommend the same paper two months in a row). | `run_date` (unique → idempotency), `resources_found`, `resources_selected`, `selected_urls[]`, `draft_post`, `draft_x_post`, `approval_status`, `whop_post_id`, `error_msg` |
| `clawbot_pending_approval` | Per-run approval-loop state. Tracks the Telegram message id, current status (`waiting` / `awaiting_notes`), accumulated edit notes, and revision count (capped at 3). | `run_id` (FK), `telegram_msg_id`, `status`, `edit_notes`, `revision_count` |

Idempotency is enforced by the `UNIQUE` on `clawbot_resource_run_log.run_date` —
a duplicate cron firing the same day inserts nothing and `prepare` exits early.

### Why Supabase (vs. SQLite or files)

- **Multi-process state:** `clawbot_prepare.py` (cron) and `clawbot_webhook.py`
  (long-running service) both need to read/write the same rows. Supabase removes
  any "who holds the file lock" concerns.
- **Editable prompts:** Prompts in a table, not in source. Tweaking voice or
  ranking criteria is a one-row UPDATE, no deploy.
- **Free tier is enough:** ~12 rows/year per table. Costs $0.

### Network topology

| Surface | Bind | Public? | Auth |
|---|---|---|---|
| `clawbot_prepare.py` | none (CLI process) | No | — |
| `clawbot_webhook.py` | `127.0.0.1:8090` | No (loopback only) | — |
| nginx → webhook | `0.0.0.0:443` (existing) | Yes (HTTPS) | TLS, plus webhook URL is a non-guessable path |
| Telegram → nginx | inbound from Telegram's IPs | Yes | Telegram signs via the bot token; we only register one URL |
| Whop API | outbound only | — | App API key in `Authorization: Bearer` |
| Supabase | outbound only | — | Service-role JWT in `Authorization: Bearer` |
| Anthropic API | outbound only | — | API key in `x-api-key` header (handled by SDK) |

### Secrets (`.env`)

| Var | Purpose | Where used |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude calls (rank, write, revise) | both processes |
| `TELEGRAM_BOT_TOKEN` | Send draft, send buttons, answer callbacks | both processes |
| `TELEGRAM_CHAT_ID` | Where to DM the approval message | prepare only |
| `WHOP_API_KEY` | App API key (`apik_...`) for the private Whop app installed on NCV | webhook only |
| `WHOP_APP_ID` | Reference only (not currently sent in headers — Whop infers app from key) | informational |
| `WHOP_FORUM_ID` | Target forum's experience id (`exp_...`) | webhook only |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase REST + service-role auth (bypasses RLS — server-only) | both processes |
| `GITHUB_TOKEN` | Optional. Lifts the unauthenticated 60/hr GitHub API rate limit during crawl. | prepare only |
| `WEBHOOK_PUBLIC_URL` | Used once by `scripts/set_telegram_webhook.sh` to register with Telegram | one-shot |

### Cost

| Item | Monthly | Annual |
|---|---|---|
| Anthropic (Sonnet 4.6 — 1 rank + 1 write per run, baseline) | ~$0.12 | ~$1.50 |
| Anthropic (worst case: 3 revisions per run) | ~$0.22 | ~$2.65 |
| Supabase | $0 (free tier) | $0 |
| Telegram Bot API | $0 | $0 |
| Whop API | $0 | $0 |
| Server (existing) | $0 incremental | $0 incremental |
| **Total** | **~$0.12–$0.22** | **~$1.50–$3.00** |

All Anthropic spend lands in `~/.openclaw/usage/usage.jsonl` tagged
`project="clawbot"` and is visible in the OpenClaw usage dashboard.

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
