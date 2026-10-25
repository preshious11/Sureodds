# Pro value publish wiring: sizing and plan (not built)

Status: plan only. Nothing here is implemented, deployed or enabled. `PRO_VALUE_MODE` stays `shadow`.

## What "publish" has to touch

1. **Selection hand-off (generation, Python).** Today the Pro value shadow runs after the generation step and only
   writes the shadow DB. Publish mode needs the Pro value picks computed before Bet of the Day is picked, so that
   `PredictionPool.package` (`predbot/selection/picker.py`) can drop every Pro event before `select_top`. The inputs
   (the raw OddsAPI files and the candidate archive) already exist by then, but the ordering in the generation run
   has to change: the Pro selection must run between the candidate archive write (`daily_picks.py` store step) and
   the Bet of the Day selection.
2. **Storage (D1).** A new table (e.g. `pro_value_picks`, migration `0018`) holding the day's published Pro value
   picks. Writes go through the existing D1 sync path (`workers/scripts/d1_sync.py` plan/apply, with its write and
   destructive budgets and the settlement guard). This is production write path code and needs a dry run against
   the staging DB before any production run.
3. **Publishing (Worker, TypeScript).** The live Pro ticket is built in the Worker (`workers/src/pro_picks.ts`,
   `buildProPicks`) from `custom_pick_candidates`. Publish mode needs a formatter and a delivery path for the
   Pro value picks, and the "No Pro pick today" message (`PRO_VALUE_GO_LIVE.md` section 3) when the day is empty.
4. **Settlement.** Published Pro value picks must be settled, or the track record the criteria rely on does not
   exist. The settlement job has to recognise the new tier.
5. **Switch.** `PRO_VALUE_MODE` is already in `Config` and `publish_plan`. Both directions are tested. The Worker
   and generation paths must read the same value and default to shadow.

## Estimate

- Python (hand-off, storage writes, publish plan, settlement tier): about 2 days, with tests.
- Worker (formatter, delivery, "No Pro pick" message, reading the table): about 1 day, with tests.
- D1 migration and sync dry runs on staging, then a supervised production dry run: about 1 day.
- Total: about 4 working days, plus your review. This is a sizing estimate, not a commitment.

## Decisions you need to make

1. **Replace or run alongside?** Does publish mode replace the live Pro ticket (`buildProPicks`), or publish the
   Pro value picks as a separate message? Replacing is simpler to roll back by switch, but changes live Pro output.
2. **Bet of the Day exclusion.** Confirm that Bet of the Day drops every Pro event in publish mode, as the
   requirement says, even when Pro's picks are not the live Pro ticket.
3. **Settlement track record.** Confirm that published Pro value picks are settled the same way as other picks,
   and that the go-live criteria count only published picks or only shadow picks (the plan assumes shadow picks,
   which is what the criteria were written against).
4. **Empty-day message.** Approve the wording in `PRO_VALUE_GO_LIVE.md` section 3 before the message is built.

## Risks

- Changing the generation ordering touches the core run. A bug there can break Bet of the Day on every day, not
  only Pro days. The hand-off must be wrapped so that any failure leaves the pre-switch selection in place.
- The D1 sync guard is production-critical. The new table needs its own guard entries and must not widen any
  existing write budget.
- The go-live criteria were written against shadow picks. Publishing changes the picks (stake, timing and
  selection pressure), so the first weeks after the switch are a new observation period, not a continuation.
- Rollback is a single env edit (`PRO_VALUE_MODE=shadow`) only after the hand-off is in place. Until then, there is
  no live switch for the Pro path to roll back.
