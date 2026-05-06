"""Clawbot prepare — monthly cron entry point.

Crawls AI/ML sources, calls Claude to rank + write, logs the draft to Supabase,
sends a Telegram approval message with inline buttons. Exits cleanly. Approval
is handled by clawbot_webhook.py.

Idempotent on (run_date) — a duplicate cron firing the same day is a no-op.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import feedparser
import httpx
from dotenv import load_dotenv

# Wire into Jae's per-call usage logger so spend shows up in the dashboard.
sys.path.insert(0, "/home/jae/.openclaw/usage")
from usage_log import LoggedAnthropic  # noqa: E402

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
SUPA_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPA_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GH_TOKEN = os.environ.get("GITHUB_TOKEN") or None

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2000
PROJECT = "clawbot"

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def supa(method: str, path: str, **kw) -> Any:
    headers = {
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Prefer": "return=representation",
        "Content-Type": "application/json",
    }
    r = httpx.request(method, f"{SUPA_URL}/rest/v1/{path}",
                      headers=headers, timeout=30, **kw)
    r.raise_for_status()
    if r.status_code == 204 or not r.text:
        return None
    return r.json()


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def crawl_arxiv() -> list[dict]:
    out = []
    for cat in ("cs.LG", "cs.AI", "stat.ML"):
        feed = feedparser.parse(f"http://export.arxiv.org/rss/{cat}")
        for e in feed.entries[:25]:
            out.append({
                "source": "arxiv",
                "title": (getattr(e, "title", "") or "").strip(),
                "url": getattr(e, "link", ""),
                "summary": (getattr(e, "summary", "") or "")[:600],
                "date": getattr(e, "published", ""),
            })
    return out


def crawl_huggingface() -> list[dict]:
    feed = feedparser.parse("https://huggingface.co/papers")
    return [{
        "source": "huggingface",
        "title": (getattr(e, "title", "") or "").strip(),
        "url": getattr(e, "link", ""),
        "summary": (getattr(e, "summary", "") or "")[:600],
        "date": getattr(e, "published", ""),
    } for e in feed.entries[:30]]


def crawl_paperswithcode() -> list[dict]:
    try:
        r = httpx.get("https://paperswithcode.com/api/v1/papers/",
                      params={"ordering": "-stars", "items_per_page": 30},
                      timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception:
        return []
    return [{
        "source": "paperswithcode",
        "title": p.get("title", ""),
        "url": p.get("url_abs") or p.get("url_pdf") or "",
        "summary": (p.get("abstract") or "")[:600],
        "date": p.get("published", ""),
    } for p in results if p.get("title")]


def crawl_github() -> list[dict]:
    last_month = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).isoformat()
    headers = {"Authorization": f"Bearer {GH_TOKEN}"} if GH_TOKEN else {}
    try:
        r = httpx.get("https://api.github.com/search/repositories",
                      params={
                          "q": f"topic:machine-learning pushed:>{last_month} stars:>50",
                          "sort": "stars",
                          "per_page": 20,
                      },
                      headers=headers, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception:
        return []
    return [{
        "source": "github",
        "title": it["full_name"],
        "url": it["html_url"],
        "summary": it.get("description") or "",
        "stars": it.get("stargazers_count", 0),
        "date": it.get("pushed_at", ""),
    } for it in items]


def crawl_all() -> list[dict]:
    items = []
    for fn in (crawl_arxiv, crawl_huggingface, crawl_paperswithcode, crawl_github):
        try:
            items.extend(fn())
        except Exception as e:
            print(f"[warn] {fn.__name__} failed: {e}", file=sys.stderr)
    seen: set[str] = set()
    deduped = []
    for x in items:
        u = x.get("url")
        if u and u not in seen:
            seen.add(u)
            deduped.append(x)
    return deduped


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def _parse_claude_json(text: str) -> dict:
    candidates = [
        text,
        text.replace("```json", "").replace("```", "").strip(),
    ]
    if "{" in text and "}" in text:
        first = text.index("{")
        last = text.rindex("}") + 1
        candidates.append(text[first:last])
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            continue
    raise RuntimeError(f"Claude output is not valid JSON: {text[:300]!r}")


def claude(client: LoggedAnthropic, system: str, user: str) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text
    return _parse_claude_json(text)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg(method: str, **payload) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                   json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def send_draft(run_id: str, run_date: str, found: int,
               drafted: dict) -> str | None:
    whop = drafted.get("whop_post", "")
    x = drafted.get("x_post", "")
    selected = drafted.get("resources", [])
    keyboard = {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"approve:{run_id}"},
            {"text": "Edit",    "callback_data": f"edit:{run_id}"},
            {"text": "Reject",  "callback_data": f"reject:{run_id}"},
        ]],
    }

    # Try Markdown for nice formatting; fall back to plain text if Telegram's
    # parser chokes on an unescaped char in a paper title (very common with
    # _, *, [, ` in arXiv/HF titles).
    md_text = (
        f"*Clawbot - {run_date}*\n"
        f"Found {found} - Selected {len(selected)}\n\n"
        f"*Whop draft:*\n{whop}\n\n"
        f"*X draft:*\n{x}"
    )[:3900]
    try:
        res = tg("sendMessage",
                 chat_id=TG_CHAT, text=md_text,
                 parse_mode="Markdown", reply_markup=keyboard)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 400:
            raise
        print(f"[warn] Telegram 400 on Markdown send; retrying plain text. "
              f"({e.response.text[:200]})", file=sys.stderr)
        plain_text = (
            f"Clawbot - {run_date}\n"
            f"Found {found} - Selected {len(selected)}\n\n"
            f"Whop draft:\n{whop}\n\n"
            f"X draft:\n{x}"
        )[:3900]
        res = tg("sendMessage",
                 chat_id=TG_CHAT, text=plain_text, reply_markup=keyboard)
    return str((res.get("result") or {}).get("message_id") or "") or None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    today = dt.date.today().isoformat()

    existing = supa("GET", f"clawbot_resource_run_log?run_date=eq.{today}&select=id")
    if existing:
        print(f"[skip] run already exists for {today}: {existing[0]['id']}")
        return 0

    run_id = str(uuid.uuid4())
    print(f"[start] run_id={run_id} date={today}")

    items = crawl_all()
    print(f"[crawl] {len(items)} unique items")
    if not items:
        raise RuntimeError("crawl returned 0 items; refusing to call Claude")

    prev_rows = supa(
        "GET",
        "clawbot_resource_run_log?order=run_date.desc&limit=2&select=selected_urls",
    ) or []
    prev_urls = sum((row.get("selected_urls") or [] for row in prev_rows), [])
    print(f"[dedup] {len(prev_urls)} previous urls to avoid")

    ranker = supa("GET",
                  "clawbot_prompt_templates?key=eq.resource_ranker&select=*")[0]
    writer = supa("GET",
                  "clawbot_prompt_templates?key=eq.content_writer&select=*")[0]

    client = LoggedAnthropic(api_key=ANTHROPIC_KEY,
                             project=PROJECT, script="prepare")

    print("[claude] ranking...")
    ranked = claude(
        client,
        ranker["system_prompt"],
        ranker["user_template"]
            .replace("{{RESOURCES}}", json.dumps(items))
            .replace("{{PREVIOUS_URLS}}", json.dumps(prev_urls)),
    )
    print(f"[claude] selected {len(ranked.get('selected_resources', []))}")

    print("[claude] writing...")
    drafted = claude(
        client,
        writer["system_prompt"],
        writer["user_template"].replace("{{RANKED_RESOURCES}}", json.dumps(ranked)),
    )
    selected_urls = [
        r.get("url") for r in drafted.get("resources", []) if r.get("url")
    ]

    supa("POST", "clawbot_resource_run_log", json={
        "id": run_id,
        "run_date": today,
        "resources_found": len(items),
        "resources_selected": len(selected_urls),
        "selected_urls": selected_urls,
        "draft_post": drafted.get("whop_post", ""),
        "draft_x_post": drafted.get("x_post", ""),
        "approval_status": "pending",
    })

    try:
        msg_id = send_draft(run_id, today, len(items), drafted)
    except Exception:
        # Don't strand the day's slot — let the next run try again.
        # Draft+resources are still safe in Supabase if anyone wants to recover.
        try:
            supa("DELETE", f"clawbot_resource_run_log?id=eq.{run_id}")
        except Exception as cleanup_err:
            print(f"[warn] cleanup failed: {cleanup_err}", file=sys.stderr)
        raise

    supa("POST", "clawbot_pending_approval", json={
        "run_id": run_id,
        "telegram_msg_id": msg_id,
        "status": "waiting",
    })

    print(f"[done] sent draft, telegram_msg_id={msg_id}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"[fail] {err}", file=sys.stderr)
        traceback.print_exc()
        # Best-effort failure notice to Telegram so a botched run isn't silent.
        try:
            tg("sendMessage", chat_id=TG_CHAT,
               text=f"Clawbot prepare failed:\n```\n{err}\n```",
               parse_mode="Markdown")
        except Exception:
            pass
        sys.exit(1)
