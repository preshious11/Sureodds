# Top Picks investigation — 3 October 2026

The supplied production run becomes empty **at the bookmaker gate, before accumulator construction**. The investigation also confirmed a provider-cache bug that can preserve old collection settings and empty discovery results through a force refresh. Probability, bookmaker, edge and evidence thresholds were not reduced.

## 1. Production cause and limits of the evidence

Production reported only SharpAPI baseball/tennis events, no Odds API events, and no SGO events. Its Top rejections exhaust the 55 base-valid candidates: 51 `BELOW_THRESHOLD`, then four `INSUFFICIENT_BOOKS`. No candidate reached ticket construction; zero tickets were evaluated.

The old `BELOW_THRESHOLD` bucket includes several different rules. It does not establish that all 51 failed probability specifically. The four book rejections passed the preceding Top checks, but later shared evidence gates were never reached. The production report does not contain raw provider rows, candidate probabilities, its effective configuration, or a deployed commit ID. Those missing details cannot be reconstructed exactly from this text.

The production force refresh spent zero SharpAPI/Odds API calls. Its cached SharpAPI feed list lacks all three international feeds now configured in the repository. That is consistent with old cached collection settings, an older deployment, or an explicit international opt-out. It does not prove which of those originally caused missing football. The cache defect below is independently reproduced by regression tests.

## 2. Real funnels

Production's available counts:

| Stage | Count |
|---|---:|
| Raw scored candidates | 64 |
| Football events/candidates | 0 / 0 |
| Base-valid candidates | 55 |
| Rejected by aggregated earlier Top gates | 51 |
| Remaining before bookmaker requirement | 4 |
| Rejected for insufficient books | 4 |
| Final Top candidates | 0 |
| Tickets evaluated / selections | 0 / 0 |
| Daily-qualified / selected | 4 / 4 |

Intermediate production main/probability/evidence counts are unavailable in the old report. New diagnostics record them directly rather than infer them.

Live collection with the local credentials at **13:57:51 WAT**, on a database copy, returned these real counts:

| Stage | SharpAPI + Odds API, SGO exhausted | Remove Odds API as well |
|---|---:|---:|
| Raw candidates | 240 | 208 |
| Base-valid | 229 | 199 |
| Main markets | 161 | 137 |
| Probability-qualified | 27 | 28 |
| Book-qualified | 2 | 0 |
| Evidence-qualified | 2 | 0 |
| Edge-qualified | 2 | 0 |
| Deduped / final candidates | 2 / 2 | 0 / 0 |
| Top selections | 2 | 0 |
| Daily selections | 6 | 5 |

The live Top was Iceland to Win at 1.426 and Switzerland to Win at 1.25, combined **1.7825**, within the existing 1.75–2.75 fallback band. Both had 14 distinct books. Their model probabilities were 0.672402 and 0.776670; sharp-reference EVs were approximately -3.0616% and +0.3767%. Daily combined odds were 11.2055. This is a later local sample, not a claim that production served this ticket.

Replaying that later capture at 09:34 WAT produced 268 raw, 254 base-valid, 175 main, 29 probability-qualified, two book-qualified and two Top selections. Provider data can change during a day; this replay does not recreate production's earlier upstream responses.

## 3. Daily versus Top

Every production Daily selection was priced from two books. With the unchanged code-default `TOP_MIN_BOOKS=3`, each fails Top coverage independently of its other attributes:

| Production Daily selection | Daily | Top | Established coverage issue |
|---|---|---|---|
| Los Angeles Dodgers to Win, 1.46 | Yes | No | Two books |
| Jakub Mensik to Win, 1.46 | Yes | No | Two books |
| Cleveland Guardians to Win, 1.65 | Yes | No | Two books |
| Antonia Ruzic to Win, 1.70 | Yes | No | Two books |

Their individual additional Top failures cannot be determined from the old report. Its exact effective book minimum is also not printed. These four rounded prices multiply to approximately 5.98; Daily was populated but under its 10–12 target, consistent with refusing weak padding.

The later live sample provides complete per-selection diagnostics:

| Daily selection | Books | Top rejection |
|---|---:|---|
| Spain–Czech Republic Over 2.5 | 3 | Secondary market |
| Scotland to Win | 14 | Probability floor |
| India–Brazil Over 4.5 | 2 | Probability floor; insufficient books |
| Los Angeles Dodgers to Win | 2 | Insufficient books |
| New York Yankees +1.5 | 2 | Insufficient books |
| Milwaukee Brewers to Win | 2 | Insufficient books |

The CLI CSV records every candidate, all its failed Top gates, bookmaker names, provider/model-signal coverage, uncertainty, edge, and ticket outcome. Daily's general trace no longer overwrites the separate Top audit.

## 4. Book coverage and deduplication

For the live candidates passing every other Top quality gate, the book distribution was:

| Books | Candidates |
|---|---:|
| 1 | 19 |
| 2 | 5 |
| 3 | 0 |
| 4+ | 2 |

Three-book coverage excluded 24 of 26 otherwise-qualified candidates, but allowed the two 14-book candidates. Without Odds API, every base-valid line had fewer than three books; 23 one-book and four two-book candidates passed all the other Top gates. Top was then physically impossible under the existing coverage requirement.

Named bookmaker quotes are unioned across providers. DraftKings reported by SharpAPI and Odds API counts once; FanDuel and BetMGM remain separate books. The regression verifies that union yields three, not four. No bookmaker count or provider-count policy was weakened.

## 5. SGO degradation

The live usage probe reported **2,505 / 2,500 objects**, exhausted. SGO supplied no events. Removing SGO from that capture changed no counts, and Top remained populated through Odds API/SharpAPI. Both surviving candidates carried quotes from **SharpAPI + Odds API**. No available candidate lost SGO quotes because none were present.

To compare genuinely available versus absent SGO without inventing quotes, the real 19 September recording was replayed after removing SGO before merging/scoring:

| Stage | Recorded SharpAPI + SGO | Same recording without SGO |
|---|---:|---:|
| Raw candidates | 2,449 | 276 |
| Base-valid | 1,751 | 264 |
| Main | 448 | 205 |
| Probability-qualified | 73 | 41 |
| Book-qualified | 16 | 0 |
| Evidence/edge-qualified | 10 / 10 | 0 / 0 |
| Top selections | 2 | 0 |
| Daily selections | 8 | 7 |

Without SGO, that historical sample had 17 one-book and 19 two-book candidates passing every other Top gate. These are historical effects, not estimates of today's unavailable SGO feed. Current international Top selections were supplied by Odds API with sufficient books; production's empty international coverage is therefore material.

## 6. Sharp-reference and edge handling

The shared gate applies `edge_ev < -0.04` only when a real sharp reference exists. Missing reference preserves `p_fair=None`, `edge_ev=None`, and no invented edge; uncertainty increases through `SE_NO_SHARP`. Such candidates remain eligible when other gates pass.

The live all-failure audit counted 12 negative-sharp-edge failures; none blocked its two survivors. The final Top cutoff is regression-tested at exactly -4%, just below -4%, and positive edge. The separate model-versus-market disagreement floor remains -5% for independent signals. Lower confidence bound and sharp edge determine ranking; there is no separate Top LCB floor in the existing policy.

## 7. Main-market classification

Result/moneyline, DNB, DC, full-match main totals and main handicaps are tested after each supported provider's normalization. Odds API exposes its supported result/spread/total feed; SharpAPI and SGO cover the wider set.

A confirmed bug treated any `entity=home/away` as a team total. SGO uses those entities for normal spreads. Classification now applies that secondary-market rule only to totals. Both handicap sides share a canonical main-line group, so far alternate handicaps remain restricted. This did not cause the supplied production empty because SGO supplied no events there.

## 8. Accumulator construction

The production constructor received zero candidates. It was not the immediate cause. The existing exhaustive band-aware search retains different-price alternatives on one fixture, excludes combinations sharing a real fixture, and does not require exactly 2.00.

Two additional defects were fixed: the default excluded permitted one-leg tickets, and the final odds comparison ignored the floating-point tolerance used by the search. For example, `1.27 * (2 / 1.27)` evaluates to `1.9999999999999998` and was rejected at a 2.00 boundary. Final and priority-pool checks now use consistent numerical tolerance. This is numerical error tolerance, not a broader odds policy.

Strong singles can satisfy the strict 2.00–2.50 band. The widened fallback remains limited to 2–3 legs. The maximum is bounded to three, including stale zero/out-of-range configuration. No weak selection is added to satisfy a target.

## 9. Effective configuration

Verified local runtime settings, retained after the fix unless stated:

| Setting | Effective value |
|---|---|
| `TOP_MIN_PROB` | 0.62 |
| `TOP_RELAX_PROB` | 0.00 |
| `TOP_MIN_BOOKS` | 3 |
| `TOP_MIN_AGREEMENT` | 0.50 |
| `TOP_MAX_NEG_EV` | -0.05 |
| `TOP_LEG_ODDS_MIN` | 1.05, with shared price sanity floor 1.15 |
| `TOP_LEGS_MIN` | 1, changed from 2 |
| `TOP_LEGS_MAX` / `TOP_MAX_PICKS` | 3; alias supported, canonical name wins |
| Strict odds / fallback odds | 2.00–2.50 / 1.75–2.75 |
| Sharp edge cutoff / price | -0.04 / best available soft price |
| Maximum book dispersion | 0.08 implied probability points |
| Unsupported model edge ceiling | 0.08 |
| Market quality | Main only |
| Minimum provider count / sharp required | None / no |
| Model signals | At least two, a coherence check rather than independent evidence |
| Sports | Football, basketball, American football, baseball, tennis |
| Leagues | Configured collection lists plus results-path eligibility |
| International football | Enabled; Nations League and friendly coverage included |
| Window | 30-minute lead; at most 24 hours; local midnight + 3-hour end offset |
| Dedupe/diversity | One fixture per Top ticket; family variety breaks ties |

Existing stale Daily pins and `TOP_RELAX_PROB=0.04` protection remain. The old default `TOP_LEGS_MIN=2` is also ignored with a warning, unless `HONOR_PINNED_VALUES=1`. Effective values and nonsecret Top environment inputs are captured for future comparisons. Production's dashboard values remain unverified from the old text.

## 10. Exact changes and why they preserve quality

- `cache/providers.py`: collection-setting signatures; refresh unknown legacy settings once; expire complete empty responses at the existing retry interval during generation; report reuse/refresh reason. Normal successful matching portions remain reusable. Changed-setting failures cannot restore old excluded quotes. Existing provider budgets, reserve credits, quota checks and SGO attribution remain the enforcement path.
- `selection/picker.py`, `selection/top_diagnostics.py`: separate complete Top gate audit, stage counts, coverage histogram, effective configuration, explicit quality-versus-construction status, consistent numerical odds boundaries.
- `selection/evidence.py`, `selection/families.py`: canonical handicap main-line classification; preserve actual team totals and far-alternate restrictions.
- `config.py`, `.env.example`: permitted one-leg default, stale minimum handling, supported maximum and alias, example relaxation probability corrected to zero.
- `jobs/daily_picks.py`, `bot/lastrun.py`: admin-only funnel, runtime revision/version and provider cache provenance.
- `diagnose_top.py`: reproducible live capture or offline audit on a SQLite copy, provider-removal comparisons, JSON summary and per-candidate CSV. No subscriber messages, stored-pick writes or served-snapshot changes.

The fix restores fresh eligible data and correct interpretation of main markets. It does not reduce probability, book coverage, sharp edge, agreement, dispersion or evidence requirements. API-Football was not reintroduced. Subscriber formatting/navigation code was not changed.

## 11. Diagnostic reproduction

Run from `predictions-bot`:

```powershell
python diagnose_top.py --db tests/fixtures/replay/replay.db --json data/top_diagnostic.json --csv data/top_candidates.csv
python diagnose_top.py --live --record data/top_recording.db --json data/top_live_diagnostic.json --csv data/top_live_candidates.csv
python diagnose_top.py --db data/top_recording.db --at 2026-10-03T08:34:00Z --json data/top_morning_diagnostic.json
```

The live command spends real provider calls under their existing limits and records counters in its database copy. Offline runs spend no calls. On the next production generation, incompatible legacy provider rows refresh through the existing quota gates. Subscriber cache reads continue to make no provider calls. The resulting `/lastrun` reports diagnostic v1 and the deployment revision when supplied by Railway.

## 12. Regression coverage

Added `tests/test_top_diagnostics.py` and `tests/test_provider_cache_settings.py`: complete/empty cache recovery; league and market-setting changes; unchanged successful cache reuse; no old quote restoration on failed changed-settings refresh; Top populated without SGO; Daily-versus-Top independent qualification; missing sharp handling; -4% boundary; 1/2/3-leg and approximate target combinations; floating-point boundaries; overshoot protection; alternative lines on one fixture; bookmaker union; provider main-market classification; SGO main versus far-alternate spread; complete rejection trace; effective limits; and subscriber diagnostic privacy.

Replay expectations include new diagnostics and corrected classification. Recorded selections and odds remain the same. The last two historical Daily legs exchange rank; position-based seeded recap examples consequently change their artificial results. These are intentional effects of corrected evidence/ranking, not a subscriber layout change. The replay harness stamps collection signatures only in its temporary frozen-data copy; production cache invalidation is tested separately.

## 13. Validation and production follow-through

Full suite: **1,247 passed in 23.00 seconds** (`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`). `git diff --check` passed. Live and historical provider-removal audits completed; the final code reproduces a healthy local Top without SGO and the empty fallback at the book gate without sufficient alternative-provider coverage.

The production count-loss stage is proven. Its original missing-football cause cannot be narrowed beyond cached/old/opted-out collection without the deployed revision, effective settings and provider rows. Deployment itself and a new production generation have not been verified from this workspace. After deployment, diagnostic v1, the runtime revision, refreshed cache reasons, configured international feeds and the new Top funnel make those distinctions directly observable. If genuinely current providers still offer only two books, Top remains safely empty under the unchanged three-book policy.
