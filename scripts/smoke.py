"""Smoke test — verifies imports, .env wiring, Supabase reachability, and
Telegram bot auth WITHOUT making any Claude calls or posting anywhere.

Run:  /home/jae/clawbot/.venv/bin/python scripts/smoke.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}{(': ' + detail) if detail else ''}")
    return ok


def main() -> int:
    print("clawbot smoke test")
    all_ok = True

    # 1. Required env vars
    required = [
        "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "WHOP_API_KEY", "WHOP_FORUM_ID",
        "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
    ]
    print("\n[env]")
    for k in required:
        v = os.environ.get(k, "")
        present = bool(v) and not v.endswith("...")
        all_ok &= check(k, present, f"len={len(v)}")

    # 2. Imports
    print("\n[imports]")
    try:
        sys.path.insert(0, "/home/jae/.openclaw/usage")
        from usage_log import LoggedAnthropic  # noqa: F401
        all_ok &= check("usage_log.LoggedAnthropic", True)
    except Exception as e:
        all_ok &= check("usage_log.LoggedAnthropic", False, str(e))
    try:
        import feedparser  # noqa: F401
        all_ok &= check("feedparser", True)
    except Exception as e:
        all_ok &= check("feedparser", False, str(e))
    try:
        import fastapi  # noqa: F401
        all_ok &= check("fastapi", True)
    except Exception as e:
        all_ok &= check("fastapi", False, str(e))

    # 3. Supabase reachability (requires schema.sql to be applied)
    print("\n[supabase]")
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if url and key and url.endswith("supabase.co"):
        try:
            r = httpx.get(
                f"{url}/rest/v1/clawbot_prompt_templates?select=key&limit=10",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                timeout=10,
            )
            ok = r.status_code == 200
            all_ok &= check("REST /clawbot_prompt_templates", ok,
                            f"status={r.status_code}")
            if ok:
                keys = [row["key"] for row in r.json()]
                all_ok &= check("seeded prompts present",
                                {"resource_ranker", "content_writer"}.issubset(set(keys)),
                                f"found={keys}")
        except Exception as e:
            all_ok &= check("Supabase reachable", False, str(e))
    else:
        all_ok &= check("Supabase URL configured", False, "fill in .env")

    # 4. Telegram bot auth
    print("\n[telegram]")
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tok and ":" in tok:
        try:
            r = httpx.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10)
            ok = r.status_code == 200 and r.json().get("ok")
            name = (r.json().get("result") or {}).get("username", "?")
            all_ok &= check("getMe", ok, f"@{name}")
        except Exception as e:
            all_ok &= check("getMe", False, str(e))
    else:
        all_ok &= check("TELEGRAM_BOT_TOKEN configured", False, "fill in .env")

    # 5. Crawl smoke (no API costs)
    print("\n[crawl]")
    try:
        import feedparser
        feed = feedparser.parse("http://export.arxiv.org/rss/cs.LG")
        all_ok &= check("arxiv cs.LG", len(feed.entries) > 0,
                        f"{len(feed.entries)} entries")
    except Exception as e:
        all_ok &= check("arxiv cs.LG", False, str(e))

    print(f"\n{'PASS' if all_ok else 'FAIL'} - smoke {'looks good' if all_ok else 'has issues above'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
