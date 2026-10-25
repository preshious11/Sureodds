"""Send the shadow Pro picks of this run to the owner's private admin chat. Nothing is published.

  python tools/pro_value_notify.py --result-json /tmp/prodacc_pro_shadow.json --db /tmp/pro_value_shadow.db
Needs TELEGRAM_BOT_TOKEN in the environment. The chat id comes from workers/wrangler.toml (TELEGRAM_ADMIN_IDS).
Errors are printed through safe_error, so the token never reaches the log.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import requests  # noqa: E402

from predbot.pro_value import notify  # noqa: E402
from predbot.util.redact import safe_error  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--result-json", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--wrangler-toml", default=os.path.join(HERE, "..", "..", "workers", "wrangler.toml"))
    args = p.parse_args(argv)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    try:
        with open(args.wrangler_toml, encoding="utf-8") as fh:
            admin = notify.admin_chat_id(fh.read())
    except OSError as exc:
        print("could not read the Worker config:", safe_error(exc, [token]))
        return 1
    if not admin:
        print("admin chat id is not configured in the Worker config; nothing sent")
        return 2
    result = None
    if os.path.exists(args.result_json):
        try:
            with open(args.result_json, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except ValueError:  # a shadow run cut short leaves partial output: treat it as no result
            loaded = {}
        result = loaded if loaded.get("status") == "ok" else None
    picks, near = [], []
    try:
        if result is not None:
            picks, near = notify.load_run(args.db, result["run_id"])
    except (sqlite3.Error, KeyError) as exc:
        print("could not read the shadow DB:", safe_error(exc, [token]))
        return 1
    text = notify.build_message(result, picks, near)
    try:
        sent = notify.send_to_admin(token, admin, text, requests.post)
    except Exception as exc:  # noqa: BLE001 - reported with secrets masked, never re-raised
        print("send to the admin chat failed:", safe_error(exc, [token]))
        return 1
    print(f"sent {sent} message(s) to the admin chat ({len(picks)} picks, {len(near)} near misses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
