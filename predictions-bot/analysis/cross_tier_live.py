"""Phase 5 driver: ``cli.py picks --dry-run`` in-process, with the pickset and the scored pool recorded.

Run it against a THROWAWAY copy of the database (``DB_PATH=<temp copy>``), with the Telegram variables blanked:

    DB_PATH=/path/to/copy.db TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= python analysis/cross_tier_live.py <state.json>

It is exactly ``cli.main(["cli.py", "picks", "--dry-run"])`` - same providers, same picker, same card - plus:
  * the scored pool and the resulting PickSet are captured by wrapping ``daily_picks.select``;
  * after the card, it prints (1) every cross-provider fixture link the picker formed, (2) every fixture present
    in both tiers with both selections, (3) the cross-tier rejection counts and top_relaxed;
  * the slate fingerprint goes to <state.json>; when that file already exists the run is a REPEAT: the clock
    is pinned to the first run's ``now`` and the two slates are compared.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli                                                   # noqa: E402
from predbot.cache import fetcher                            # noqa: E402
from predbot.jobs import daily_picks                         # noqa: E402
from predbot.selection import cross_tier                     # noqa: E402
from predbot.selection.picker import fixture_relation, link_events   # noqa: E402
from predbot.util.timeutil import iso, parse_iso, utcnow     # noqa: E402

state_path = Path(sys.argv[1])
previous = json.loads(state_path.read_text()) if state_path.exists() else None
now = parse_iso(previous["now"]) if previous else utcnow()
daily_picks.utcnow = lambda: now
fetcher.utcnow = lambda: now

captured = {}
_real_select = daily_picks.select


def _recording_select(scored, cfg, when, *a, **k):
    ps = _real_select(scored, cfg, when, *a, **k)
    captured["scored"], captured["ps"], captured["cfg"] = scored, ps, cfg
    return ps


daily_picks.select = _recording_select
cli.main(["cli.py", "picks", "--dry-run"])

ps, scored = captured["ps"], captured["scored"]
print("\n================ CROSS-TIER EVIDENCE ================")
events = list({id(s.event): s.event for s in scored}.values())
links, swapped = link_events(events)
print(f"events in the scored pool: {len(events)}   merged inside the chain (>=2 providers): "
      f"{sum(1 for e in events if len(set(e.sources)) >= 2)}")
print("\n[2] fixture links the picker formed between DIFFERENT provider views of one match")
n = 0
for i, others in enumerate(links):
    for j in sorted(o for o in others if o > i):
        a, b = events[i], events[j]
        if set(a.sources) == set(b.sources) and a.provider == b.provider:
            continue
        n += 1
        print(f"  {a.sport}/{a.league!r} vs {b.league!r}{'  [home/away SWAPPED]' if (i, j) in swapped else ''}\n"
              f"    {a.provider:<15} {a.home} v {a.away}  {iso(a.start)}  sources={a.sources}\n"
              f"    {b.provider:<15} {b.home} v {b.away}  {iso(b.start)}  sources={b.sources}")
print(f"  -> {n} cross-provider link(s)")

print("\n[3] fixtures present in BOTH tiers")
shared = 0
for t in ps.top:
    for d in ps.daily:
        rel = fixture_relation(t.event, d.event)
        if rel is None:
            continue
        shared += 1
        verdict = cross_tier.relation(t.line, d.line, rel)
        print(f"  {t.event.home} v {t.event.away} ({t.event.provider}) | TOP: {t.label}  <->  DAILY: {d.label} "
              f"({d.event.provider}: {d.event.home} v {d.event.away}{', swapped' if rel else ''}) | relation={verdict}")
print(f"  -> {shared} shared fixture(s)")

print("\n[3b] Daily candidates the cross-tier rule refused (their notes)")
for s in scored:
    why = s.notes.get("cross_tier")
    if why:
        print(f"  {s.event.provider:<15} {s.event.home} v {s.event.away}  {s.label}  <- {why}")

diag = ps.diagnostics
rej = diag.get("selection_rejections", {})
print("\n[4] rejection_counts: " + json.dumps({k: v for k, v in rej.items() if k.startswith("CROSS_TIER")}))
print("    CROSS_TIER_DUPLICATE_REMOVED =", rej.get("CROSS_TIER_DUPLICATE_REMOVED", 0))
print("[5] top_relaxed =", diag.get("top_relaxed"))
print("    API usage:", {k: v for k, v in (diag.get("api_usage") or {}).items()})

fingerprint = [[tier, s.event.provider, s.event.provider_event_id, s.line.market, s.line.period, s.line.stat,
                s.line.entity, s.line.line, s.line.selection, round(s.price, 4)]
               for tier, items in (("top", ps.top), ("daily", ps.daily)) for s in items]
if previous is None:
    state_path.write_text(json.dumps({"now": iso(now), "slate": fingerprint}))
    print("\nfirst run recorded ->", state_path.name, f"({len(fingerprint)} picks)")
else:
    same = previous["slate"] == fingerprint
    print(f"\n[6] REPEAT run: slate identical to the first run: {same} ({len(fingerprint)} vs {len(previous['slate'])} picks)")
    if not same:
        for row in previous["slate"]:
            if row not in fingerprint:
                print("   only in first :", row)
        for row in fingerprint:
            if row not in previous["slate"]:
                print("   only in second:", row)
