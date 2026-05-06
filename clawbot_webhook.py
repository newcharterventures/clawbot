"""Clawbot webhook — Telegram callback handler.

Run with:  uvicorn clawbot_webhook:app --host 127.0.0.1 --port 8090

Then once, after the public URL is wired through nginx/Cloudflare Tunnel:
  curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \\
       -d url="${WEBHOOK_PUBLIC_URL}" \\
       -d allowed_updates='["callback_query","message"]'
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

sys.path.insert(0, "/home/jae/.openclaw/usage")
from usage_log import LoggedAnthropic  # noqa: E402

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WHOP_KEY = os.environ["WHOP_API_KEY"]
WHOP_FORUM = os.environ["WHOP_FORUM_ID"]
SUPA_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPA_KEY = os.environ["SUPABASE_SERVICE_KEY"]

MODEL = "claude-sonnet-4-6"
MAX_REVISIONS = 3
PROJECT = "clawbot"

app = FastAPI()


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


def tg(method: str, **payload) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                   json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def post_to_whop(run: dict) -> str | None:
    # Whop public REST API v1. Forum posts are a top-level resource; the
    # target forum is identified by experience_id (exp_...) in the body.
    # API key prefix is apik_.
    body = {
        "experience_id": WHOP_FORUM,
        "title": f"Monthly AI/ML Resources - {run['run_date']}",
        "content": run.get("draft_post") or "",
    }
    headers = {
        "Authorization": f"Bearer {WHOP_KEY}",
        "Content-Type": "application/json",
        "Idempotency-Key": run["id"],
    }
    r = httpx.post("https://api.whop.com/api/v1/forum_posts",
                   headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("id") or (data.get("data") or {}).get("id")


def parse_claude_json(text: str) -> dict:
    candidates = [
        text,
        text.replace("```json", "").replace("```", "").strip(),
    ]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            continue
    raise RuntimeError("Claude output is not valid JSON")


def claude_revise(original_draft: str, edit_notes: str) -> dict:
    writer = supa("GET",
                  "clawbot_prompt_templates?key=eq.content_writer&select=*")[0]
    client = LoggedAnthropic(api_key=ANTHROPIC_KEY,
                             project=PROJECT, script="webhook_revise")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=writer["system_prompt"],
        messages=[
            {"role": "user", "content": (
                "Below is a previous draft of the monthly Whop post.\n"
                f"PREVIOUS DRAFT:\n{original_draft}\n\n"
                "Revise it per these notes, keeping the JSON output schema and "
                "voice rules exactly:\n"
                f"NOTES:\n{edit_notes}"
            )},
        ],
    )
    return parse_claude_json(resp.content[0].text)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/clawbot/callback")
async def callback(req: Request):
    upd = await req.json()

    if cb := upd.get("callback_query"):
        action, _, run_id = cb["data"].partition(":")
        chat_id = cb["message"]["chat"]["id"]

        rows = supa("GET",
                    f"clawbot_resource_run_log?id=eq.{run_id}&select=*") or []
        if not rows:
            tg("answerCallbackQuery", callback_query_id=cb["id"],
               text="Run not found", show_alert=True)
            return {"ok": False, "reason": "run_not_found"}
        run = rows[0]

        if action == "approve":
            try:
                whop_id = post_to_whop(run)
            except httpx.HTTPStatusError as e:
                supa("PATCH", f"clawbot_resource_run_log?id=eq.{run_id}",
                     json={"approval_status": "error",
                           "error_msg": f"whop {e.response.status_code}: {e.response.text[:200]}"})
                tg("answerCallbackQuery", callback_query_id=cb["id"],
                   text="Whop post failed", show_alert=True)
                tg("sendMessage", chat_id=chat_id,
                   text=f"Whop POST failed: HTTP {e.response.status_code}\n{e.response.text[:300]}")
                return {"ok": False, "reason": "whop_failed"}
            supa("PATCH", f"clawbot_resource_run_log?id=eq.{run_id}",
                 json={"approval_status": "approved",
                       "approved_at": "now()",
                       "whop_post_id": whop_id})
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Posted")
            tg("sendMessage", chat_id=chat_id,
               text=f"Posted to Whop. ID: {whop_id}")

        elif action == "reject":
            supa("PATCH", f"clawbot_resource_run_log?id=eq.{run_id}",
                 json={"approval_status": "rejected"})
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Rejected")
            tg("sendMessage", chat_id=chat_id,
               text="Rejected - no Whop post.")

        elif action == "edit":
            pending = supa("GET",
                           f"clawbot_pending_approval?run_id=eq.{run_id}&select=*") or []
            count = (pending[0].get("revision_count") if pending else 0) or 0
            if count >= MAX_REVISIONS:
                tg("answerCallbackQuery", callback_query_id=cb["id"],
                   text=f"Max revisions ({MAX_REVISIONS}) reached", show_alert=True)
                return {"ok": False, "reason": "max_revisions"}
            supa("PATCH", f"clawbot_pending_approval?run_id=eq.{run_id}",
                 json={"status": "awaiting_notes"})
            tg("answerCallbackQuery", callback_query_id=cb["id"])
            tg("sendMessage", chat_id=chat_id,
               text=f"Reply to this message with edit notes. (run {run_id[:8]})",
               reply_markup={"force_reply": True, "selective": True})

        else:
            tg("answerCallbackQuery", callback_query_id=cb["id"],
               text="Unknown action")
        return {"ok": True}

    # Edit-notes free-text reply path
    if msg := upd.get("message"):
        reply = msg.get("reply_to_message") or {}
        prompt_text = reply.get("text", "")
        chat_id = msg["chat"]["id"]
        if "(run " not in prompt_text:
            return {"ok": True, "ignored": True}
        short = prompt_text.split("(run ")[1].split(")")[0].strip()

        pending = supa(
            "GET",
            f"clawbot_pending_approval?run_id=ilike.{short}*&select=*",
        ) or []
        if not pending:
            return {"ok": False, "reason": "no_pending"}
        pa = pending[0]
        run_id = pa["run_id"]
        count = (pa.get("revision_count") or 0) + 1
        if count > MAX_REVISIONS:
            tg("sendMessage", chat_id=chat_id,
               text=f"Max revisions ({MAX_REVISIONS}) reached. Approve or reject.")
            return {"ok": False, "reason": "max_revisions"}

        run_rows = supa("GET",
                        f"clawbot_resource_run_log?id=eq.{run_id}&select=*") or []
        if not run_rows:
            return {"ok": False, "reason": "run_not_found"}
        run = run_rows[0]

        edit_notes = msg.get("text", "").strip()
        try:
            revised = claude_revise(run.get("draft_post") or "", edit_notes)
        except Exception as e:
            tg("sendMessage", chat_id=chat_id,
               text=f"Revision failed: {type(e).__name__}: {e}")
            return {"ok": False, "reason": "revise_failed"}

        selected_urls = [
            r.get("url") for r in revised.get("resources", []) if r.get("url")
        ]
        supa("PATCH", f"clawbot_resource_run_log?id=eq.{run_id}",
             json={
                 "draft_post": revised.get("whop_post", ""),
                 "draft_x_post": revised.get("x_post", ""),
                 "selected_urls": selected_urls,
             })
        supa("PATCH", f"clawbot_pending_approval?run_id=eq.{run_id}",
             json={"revision_count": count,
                   "edit_notes": edit_notes,
                   "status": "waiting"})

        text = (
            f"*Revised draft (rev {count}/{MAX_REVISIONS})*\n\n"
            f"*Whop draft:*\n{revised.get('whop_post','')}\n\n"
            f"*X draft:*\n{revised.get('x_post','')}"
        )[:3900]
        tg("sendMessage", chat_id=chat_id,
           text=text, parse_mode="Markdown",
           reply_markup={"inline_keyboard": [[
               {"text": "Approve", "callback_data": f"approve:{run_id}"},
               {"text": "Edit",    "callback_data": f"edit:{run_id}"},
               {"text": "Reject",  "callback_data": f"reject:{run_id}"},
           ]]})
        return {"ok": True}

    return {"ok": True, "ignored": True}
