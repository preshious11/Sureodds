# Rehearsal Side Effects Audit (stage5/9/10/11)

`write_target=server` only changes WHERE the plan/apply data lands. Every
OTHER side effect in these four workflows is controlled by separate
inputs/gates that have nothing to do with `write_target` - so combining
`write_target=server` with the wrong other input (or simply hitting one of
the ungated steps below) could spend real API credits, message real
people with the real bot, or write to production D1 through a path the
plan/apply mechanism doesn't cover at all. This document lists every one
found, by workflow, and what was done about each.

**All fixes below add exactly one condition to an existing step's `if:` -
no step's behaviour under the `d1` default changes, because
`(inputs.write_target || 'd1') == 'd1'` (or, on `workflow_dispatch`-only
inputs, plain `inputs.write_target == 'd1'`) evaluates to `true` by
default and on every schedule/`workflow_call` run, exactly as before this
change.**

---

## stage5-acceptance-production

| Side effect | Gated by (before) | Risk with `write_target=server` | Fix |
|---|---|---|---|
| Live provider calls (SharpAPI, OddsAPI, SportsGameOdds, FootballData) | `operation=live-generate` + `dry_run=false` + `live_confirm=RUN-LIVE` | Nothing stopped combining this with `write_target=server` - real credits spent for a rehearsal | **New guard step**: `write_target=server` + `operation=live-generate` now fails loudly before anything runs. Rehearsal must use `operation=fixture-replay` (the default) to exercise delivery without spending credits. |
| Real group post | `allow_telegram_posts=true` (default false) | Same gap - nothing stopped the combination | **Same new guard step**: `write_target=server` + `allow_telegram_posts=true` fails loudly. |
| Pro value shadow run (spends its own OddsAPI credits, `PRO_VALUE_CREDIT_CAP`) | `mode==live` only | Not gated by `write_target` - credits spent even testing pure delivery | Added `&& (inputs.write_target \|\| 'd1') == 'd1'` |
| "Send shadow Pro picks to the admin chat" (real Telegram send, real admin, real bot token) | `mode==live` only | Messages the real admin during a rehearsal | Same guard added |
| Pro value shadow cache restore/save (`pro-value-shadow-` prefix, **shared with the real scheduled `pro-value-closing-shadow.yml`**) | `mode==live` only | A rehearsal's cache save could become what a LATER real closing-shadow run restores from - cross-contaminates production's own shadow DB continuity | Same guard added to both restore and save steps |
| "Upload Pro value shadow results" artifact | `mode==live` only | Harmless on its own (an Actions artifact, nothing external), but tied to the block above | Same guard added, for consistency |
| "Tell the owner when backup providers were used" (real Telegram send) | `success()` + `mode==live` | Messages the real admin during a rehearsal | Same guard added |
| "Alert the owner on failure (A6)" (real Telegram send) | `failure()` only - **not even gated by mode** | Fires for ANY failed run, including an offline rehearsal dry-run that happens to error | Same guard added |

Everything else in stage5 (the engine run itself, the plan-building step,
the D1 pull/export, the existing D1 apply step) was either already
read-only, already gated correctly, or is the thing `write_target` is
meant to redirect in the first place.

## stage9-settle-production

| Side effect | Gated by (before) | Risk with `write_target=server` | Fix |
|---|---|---|---|
| Live provider calls (settlement's actual job - no fixture-replay equivalent exists) | `mode==live` | **Not eliminable** - settlement's whole purpose is calling real result providers. A live rehearsal of server delivery spends the same credits a real settlement run would, as an EXTRA run on top of the normal schedule (the schedule still fires separately) | **Not gated in code** - documented here as a real, unavoidable marginal cost. Mitigate by testing with `dry_run=true` first (zero cost, validates the plan/manifest/epoch-read logic) and reserving a live `write_target=server` run for when it's actually needed, not run repeatedly. |
| "Check alert dedup state" - **writes `job_state.last_result='failed'` directly to production D1**, independent of the plan/apply mechanism entirely | `failure()` only | A rehearsal failure would mark the REAL `stage9_alert` dedup row as failed in production | Added `&& (inputs.write_target \|\| 'd1') == 'd1'` |
| "Alert the owner on failure" (real Telegram send) | `failure()` + dedup check | Messages the real admin during a rehearsal | Same guard added |
| "Reset alert dedup state on success" - **writes `job_state.last_result='ok'` directly to production D1** | `success()` only | A rehearsal success would reset the REAL dedup row, which could suppress a genuine future alert if there's an actual unresolved incident | Same guard added |

## stage10-history-backfill-production

No Telegram usage at all in this workflow (confirmed by search - zero
matches). One live provider call (`FOOTBALL_DATA_KEY`), gated by
`mode==live`, with the same "no fixture equivalent" property as stage9's
provider calls - **documented, not gated**, same mitigation (test with
`dry_run=true` first).

No code changes needed beyond the `write_target` input itself and the
existing-step gate (already covered in the previous commit).

## stage11-closing-price-production

| Side effect | Gated by (before) | Risk with `write_target=server` | Fix |
|---|---|---|---|
| The "Gate - is any published pick inside the capture window?" read | Always runs | **Read-only**, and deliberately always reads real production D1 (it needs production's real pending-pick state to decide whether there's anything to capture at all) - harmless, left unchanged |
| Live OddsAPI call for closing prices | `due>0` + `mode==live`, capped by `CLOSING_PRICE_MAX_REQUESTS_PER_DAY` | Same "no fixture equivalent" property - real, marginal cost if genuinely exercised live | Documented, not gated, same mitigation |
| "Alert the owner on failure" (real Telegram send) | `failure()` only | Messages the real admin during a rehearsal | Added `&& (inputs.write_target \|\| 'd1') == 'd1'` |

---

## Confirming the default (`d1`) path is unchanged

Every fix above is the same shape: an existing step's `if:` gained
`&& (inputs.write_target || 'd1') == 'd1'` (or, where the input only
exists on `workflow_dispatch`, plain `inputs.write_target == 'd1'` on the
NEW server-only step, never on the existing one). Two facts make the
default path provably unaffected:

1. `inputs.write_target` defaults to `'d1'` on every `workflow_dispatch`
   run that doesn't explicitly set it.
2. On a `schedule` or `workflow_call` trigger, `inputs.write_target` does
   not exist at all (empty), and `(inputs.write_target || 'd1')` falls
   back to `'d1'` - the normal scheduled runs can never evaluate to
   anything else.

So for every run except an explicit `workflow_dispatch` with
`write_target=server`, every condition above evaluates exactly as it did
before this change - the steps run under the exact same conditions as
today, byte-for-byte.

See the git history on `feature/stage1-server-migration` for the literal
diffs (each `if:` line's before/after is a one-line change, visible in
the relevant commit).
