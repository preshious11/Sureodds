"""One SportyBet catalogue request, to see what a runner gets back. Prints the status and headers only.

Nothing is stored, no body is printed, and there are no OddsAPI calls. Run it from a GitHub runner with
workflow_dispatch (sporty-catalogue-probe.yml) to check whether data-centre IPs are blocked.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from predbot.pro_value.sporty_catalogue import CATALOGUE_PATH, CatalogueError, CatalogueTransport, envelope  # noqa: E402

PARAMS = {"sportId": "sr:sport:1", "marketId": "1", "pageSize": 100, "pageNum": 1,
          "todayGames": "false", "timeline": 72}


def main() -> int:
    transport = CatalogueTransport(limit=1, total_timeout=30)
    try:
        body = envelope(transport.request("GET", CATALOGUE_PATH, params=PARAMS))
        events = sum(len(t.get("events", [])) for t in body.get("tournaments", []))
        print(f"result: ok, events={events}")
    except CatalogueError as exc:
        print(f"result: refused, {exc.describe()}")
    meta = transport.last_meta
    print("http_status:", meta.get("http_status"))
    print("server:", meta.get("server"))
    print("cf-ray:", meta.get("cf_ray"))
    print("content-type:", meta.get("content_type"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
