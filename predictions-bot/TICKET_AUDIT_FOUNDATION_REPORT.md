# Ticket Audit foundation and Gemini free-model validation

Updated: 2026-10-08 (Africa/Lagos). **Latest decision, Phase 2C.3: CONDITIONAL GO for private Telegram Ticket Audit V1, after manual production configuration and Telegram acceptance checks.** Section 29 describes the implemented private interface and supersedes the interface/deployment recommendations in Section 28. Phase 2C.2 live model validation remains historical evidence; Phase 2C.3 tests use mocked transports only. Public research remains PARTIAL. The real `.env`, prediction engine, providers, scheduling, automatic delivery and production database schema were not changed. Ticket Converter remains parked.

**Screenshot acceptance follow-up:** Section 30 identifies the local terminal failure as `private_audit_disabled`: both audit opt-in flags remain false. Safe failure diagnostics now distinguish configuration, quota, HTTP and parsing stages without logging secrets or ticket/image contents. No opt-in flag, model, quota limit or production behavior was changed automatically.

Sections 1–26 preserve the Phase 2C.1 foundation audit and historical results. **Section 27 supersedes their Gemini model/access/test recommendations with the Phase 2C.1B observations**, including the successful newer model probe and remaining blockers.

## 1. Existing local-data inventory

The normal path is `jobs/daily_picks.build_picks` → cached provider collection/`ProviderChain.collect` → canonical identity resolution/merged events → `model.engine.generate` → `EventModel.score_all` → validated scored lines → `capture_candidate` **before** representative selection → representative predictions → evidence annotations → `store_picks`. `store_picks` persists All Predictions and the complete captured candidate pool in its normal transaction. Top/Daily memberships select from the stored predictions. Audit never enters this path.

| Persisted source | Actual evidence available | Important limits |
| --- | --- | --- |
| `custom_pick_candidates` | Application date/timezone, candidate ID, canonical event ID, detached event/MarketLine/Scored JSON, first-seen/evaluation timestamps and archive schema version | Richest source. Includes evaluated alternatives that never became representatives. Not a fresh odds feed; candidates survive unsuccessful refreshes. |
| `custom_pick_pools` | Date/timezone, COMPLETE/PARTIAL status, generation time and diagnostics | Pool coverage metadata, not proof every fixture/market exists. Inspected; not needed to price or regenerate any leg. |
| `picks` | Published fixture/market coordinates, provider/canonical IDs, odds, `prob`, `prob_raw`, `p_market`, EV; migrations add `p_fair`, `edge_ev`, books/quality/rank and participant/competition IDs | First-write forecasts. Notes/signals are in `sources` JSON. Agreement/liquidity/full book quotes are not standalone pick columns. |
| `prediction_memberships` | Pick-to-All/Top/Daily membership and rank | Not independent evidence. Audit neither reads collections through generation nor inserts memberships. |
| `prediction_cache` | Application date/timezone, generated/updated/expiry times, status/version, snapshot JSON containing All/Top/Daily `PickRecord`s | Snapshot includes price, `prob`, EV, market probability, book count and fixture/provider IDs; fewer model internals than archive. Duplicate view entries are not different events. |
| `provider_cache` | Today's encoded Event/MarketLine payloads, metadata, expiry/version | Can supply fixture metadata/odds/book quotes already fetched. Raw event caches do not provide newly evaluated model probabilities. Result/history cache payloads are not treated as prediction candidates. |
| Canonical tables | `canonical_events`, `canonical_participants`, `participant_aliases`, `provider_identity`; canonical competition/home/away IDs on stored candidates/picks | Available for future direct SELECTs. Current resolver uses already persisted canonical IDs; it never constructs `IdentityRegistry`, learns aliases or calls its fuzzy matching. |
| Historical tables | `match_results`, `match_odds`, `match_odds_ext`, `history_fetches`, model ratings/calibration state | Existing result/odds/provenance can be inspected locally. This foundation does not fetch history, rerun an independent model or reconstruct new numerical probabilities from it. |

`MarketLine` coordinates are market family, selection, period, stat, entity and numeric line. It also carries price, optional fair price/provider probability, bookmaker/book count, provider odd ID, book prices/quotes and contributing providers. Signed handicap lines refer to the selected side. `Event` carries sport/league/participants/start, provider identity, canonical event/competition/participant IDs and event sources.

Captured `Scored` evidence includes `p_model`, `p_market`, `p_raw`, `p_fair`, `ev`, `edge`, `edge_ev`, agreement, liquidity, independent probability/status/model basis, fair books, model signals and notes. Post-representative rank/quality/error/lower-bound/tier fields are deliberately unavailable in captured records. Some archived numerical estimates are **market-derived**, not independent forecasts: `p_model_basis` and independent status must accompany interpretation. Agreement measures signal coherence; liquidity reflects bookmaker coverage. Neither is a guarantee of independent confirmation.

Files inspected: `main.py`; `predbot/config.py`; `providers/base.py`, `chain.py`, `oddsapi.py`, `sportsgameodds.py`, `sharpapi.py`, `espn.py`, `football_data.py`; `cache/providers.py`, `fetcher.py`, `models.py`; `model/engine.py`, `ensemble.py`, `candidates.py`; `selection/representatives.py`, `picker.py`, `evidence.py`; `storage/db.py`, `identity.py`; `util/names.py`; `jobs/daily_picks.py`, `settle.py`, `live_results.py`, `stats.py`; and existing candidate/archive and diagnostic regression tests. These were inspected, not modified.

## 2. Exact zero-provider boundary

`predbot.ticket_audit` imports only its own modules, standard-library utilities and `requests` for fixed-origin Gemini HTTPS. It cannot import providers, models, production cache/fetcher, jobs, storage constructors, Telegram or Custom Picks. The local reader opens a SQLite URI with `mode=ro`, enables `query_only`, and takes a read transaction containing SELECTs. Missing files are not created. Missing/corrupt/unavailable local data returns a limited result without refresh.

Forbidden read paths include `Database(...)` (migrations), `UnifiedFetcher.get_daily_snapshot`, `build_picks`, `generate`, `EventModel.score/score_all`, `ProviderChain.collect` and `ResultsResolver.resolve`. Every new regression test installs explicit guards on these entry points and all five current sports-provider event/result methods, plus real HTTP. A static import test guards future changes. Gemini failures never enter a sports fallback.

Audit inputs stay detached. No subscriber records, settlement memberships, live watches, model history, calibration, recaps or community messages are created. Whole-database dumps before/after audit prove records/schema remain unchanged, including already populated picks/memberships/caches.

## 3. Local resolver

Read priority: **today's candidate archive → today's picks → today's timezone-scoped prediction snapshot → today's raw provider cache**. Selected evidence retains actual fields and its timestamp. Missing metrics remain `None`; reference prices/model outputs are never synthesized.

Canonical event ID is preferred and must match exactly. Known submitted sport/participant/competition/time coordinates cannot contradict it. With a canonical ID, unknown names/competition/time may remain unknown; the stored row provides evidence identity. Without it, sport, exact Unicode-NFKC/case-folded participant and competition names, an aware kickoff within **15 minutes**, and complete market coordinates are required. No transliteration, stopword removal, fuzzy matching, side reversal, age/reserve/women qualifier stripping or learned aliases are used. More than one canonical ID or distinct kickoff returns AMBIGUOUS. Uncanonicalized raw-cache copies of the same exact fixture do not manufacture ambiguity.

Market family, selection, period, stat and entity must match exactly; Decimal comparisons preserve numeric lines without rounding. Over 2.5 cannot use Over 3.5; -1 cannot use -1.5; result3 cannot use dc. A submitted Under line can use its own evaluated Under candidate, never its Over counterpart. Unspecified period/stat/entity does not silently become a full-match market. Stale evidence is retained with a limitation; it never refreshes or initiates research just to fill missing local metrics.

## 4. Gemini model tested

Candidate: **`gemini-2.5-flash`**, using the official v1beta REST endpoint and `x-goog-api-key` HTTPS header. No SDK, alternative LLM, retry, billing operation or model switch was added. Only this reviewed model is allowed by the application gate; another configured model returns `unsupported_free_model` until explicitly reviewed/validated in a subsequent change.

Live model-list GET returned **HTTP 200**, listed `models/gemini-2.5-flash`, and exposed `generateContent`, `countTokens`, `createCachedContent`, `batchGenerateContent`. The one actual generation POST returned **HTTP 404**, mapped to `model_unavailable`. Listing a model is therefore not proof generation works.

Google currently says 2.5-model access is restricted to users with previous active usage; new projects should use newer models. This is a **possible explanation**, not a confirmed diagnosis of this project's 404. Do not blindly use Google's suggested replacements: their Search/free-tier eligibility must be checked separately. [Official access/deprecation notice](https://ai.google.dev/gemini-api/docs/deprecations).

## 5. Free-tier access

The user explicitly confirmed the project behind the configured key is **Free tier with Cloud Billing disabled**. Gemini's models API does not expose billing tier. No generation success or invoice proof was obtained, and no API operation enabled billing. Consumer Google AI Pro is unrelated to Developer API project billing. [Official billing documentation](https://ai.google.dev/gemini-api/docs/billing).

Current pricing documents free text/image input and output plus a free Google Search allowance for 2.5 Flash; published allowance is not a guarantee the configured project can use the model. Runtime generation access: **FAIL** on the current project/model. Keep research disabled. [Official pricing](https://ai.google.dev/gemini-api/docs/pricing).

## 6. Google Search grounding result

**DEFERRED / NOT RUN live** after model generation failed. There were **zero grounded requests**, not an assumed successful free search. The isolated `search` diagnostic is ready for one harmless official-NBA season-opening-date query after generation access is resolved.

Designed request: `tools: [{googleSearch: {}}]`. Retained metadata: `groundingChunks[].web.uri/title`, `groundingSupports[].segment` with chunk indices, `webSearchQueries`, and `searchEntryPoint` attribution. These are documented shapes and mocked-tested, **not an observed successful live response schema on this project**. Official model docs do not justify assuming schema-constrained output with Search on 2.5; that combination is gated off. Search responses use requested JSON plus strict application validation, not an unvalidated arbitrary-prose truth store. [Official grounding documentation](https://ai.google.dev/gemini-api/docs/google-search), [structured-output documentation](https://ai.google.dev/gemini-api/docs/structured-output).

## 7. Structured-output result

**FAIL live**: one synthetic text/JSON-schema extraction POST returned 404 before an output was obtained. The same call was intended to establish model operation, pasted-text extraction and structured output together. Application parsing/schema/null/error behavior: **PASS in mocked tests**.

The ungrounded adapter supplies `responseMimeType=application/json` and `responseJsonSchema`; it validates every required field/type, rejects unknown probability/alternative fields, nonfinite numbers, booleans as odds, invalid timestamps, unknown enums and excessive selections. Truncated/safety-refused outputs are rejected without retry. Grounded output requires an exact echo of submitted fixture and market coordinates; mismatches are controlled failures.

## 8. Image-input result

**DEFERRED live** because the model cannot currently generate. No image request or file upload was made. `image` mode produces a readable synthetic slip PNG in memory using a standard-library bitmap font; it sends inline image data only when explicitly live. No image file is persisted, no paid OCR is used, and there is no Files API upload. PNG/JPEG inputs are capped at 5 MB; invisible fields must stay null. Mocked image/null/schema tests pass. [Official image-input documentation](https://ai.google.dev/gemini-api/docs/image-understanding).

## 9. Pasted-text extraction

**FAIL live** from the same 404 structured-extraction request, not an independent second call. Mocked extraction: **PASS**. Deterministic, already normalized JSON can be parsed without Gemini through `ticket_from_json`. Free-form bookmaker parsing heuristics and booking-code decoding are deliberately absent; unclear fields are not guessed. Extraction never uses Search. Screenshot/text unknown sport, competition, kickoff and period/stat/entity remain null.

## 10. Actual request/rate/quota observations

Exactly **two live Google HTTP requests**: one model-list GET succeeded, one generation POST failed with 404. **One generation attempt; zero successful generations; zero Search attempts; zero image attempts; zero sports-provider requests.** The generation attempt/failure was recorded in `.venv/phase2c1-validation-usage.sqlite3`, outside production data. The earlier one-off model GET preceded the ledger and is counted explicitly here. Token consumption was not exposed in the retained failed response metadata; do not claim a token count or completed free allowance.

No live RPM/TPM/RPD values or Retry-After were observed. Quota exhaustion was not intentionally induced. 429 and quota behavior are mocked only. Google limits apply per project, not key, and daily quota resets at Pacific midnight; actual project limits should be inspected in AI Studio. [Official rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits).

## 11. Research sources

`SourcePolicy` classifies **reviewed domain ownership**, not model-supplied quality labels or a URL substring. A: official league/competition/team/governing body; B: established statistics/journalism; C: other reviewed reporting; LOW: unreviewed/unverifiable sources. The initial registry covers official multi-sport bodies and established journalism/statistics; callers can add reviewed clubs/reporting domains. Unknown sources are retained as LOW rather than blocking retrieval. LOW evidence cannot satisfy critical completeness criteria alone.

Every accepted fact's URL must occur in Google's grounding chunks, and its claim must occur in a support segment referencing that exact source index. Actual chunk titles replace model-provided titles. Unsupported claims are dropped; no supported facts means unavailable. This verifies grounding linkage, **not the real-world truth of a source**. Google redirect/proxy URLs remain LOW until actual ownership can be verified; there is no automatic redirect fetch or source scraping. Broader source ownership review is needed before public integration.

## 12. Market-aware research

Prompts include exact normalized fixture/date/market/selection/line/period/stat/entity coordinates and current time. Football result/DC/DNB focuses on win/loss/draw/context; BTTS on scoring/conceding; totals on distributions/tempo/exact line; handicap on margins/exact signed line. Tennis adds surface/hold-break/tournament/fitness; basketball offense/defense/injuries/rest; baseball pitchers/bullpen/ballpark; American football quarterback/units/rest/weather; MMA weight class/opponent quality/style/verified fitness. Unsupported sport/market combinations return controlled unavailable.

Facts and interpretations are separate. Prompt defenses forbid following webpage instructions, execution, secrets, invented current facts/probabilities and replacement bets. Search is Google's own grounding tool, never an existing sports client. Reliable source coverage and consistency remain application review concerns; grounded text is not independently verified statistics.

## 13. Research cache

**IMPLEMENTED**, memory only, detached structured evidence, maximum **1,024 entries**, LRU eviction. Keys include sport, canonical ID where present, normalized participants/competition, exact UTC kickoff/date, market family/selection/Decimal line/period/stat/entity, model and schema version. Same teams tomorrow and different market lines cannot collide. Conservatively, known-ID and unknown-ID requests may occupy separate entries rather than risk a wrong merge.

It stores facts/provenance/limitations/metadata/model/schema/timestamps; not raw slips or images. Cache evidence is copied on access so callers cannot mutate shared facts or local model outputs. Restart loses research cache and single-flight state, but **does not reset the durable daily usage budget**.

TTL policy (conservative proposals, tested; not empirically calibrated sports guarantees):

| Time to kickoff | Historical/form facts | Injury/fitness/lineup/availability/weather/starting-pitcher facts |
| --- | --- | --- |
| More than 24 hours | 60 minutes | 10 minutes |
| 2–24 hours | 15 minutes | 5 minutes |
| At most 2 hours | 2 minutes | 2 minutes |
| Started | No reuse/research | No reuse/research |

Mixed evidence uses the shorter dynamic TTL. Expiry never exceeds kickoff; reuse rechecks the current, potentially shorter TTL when moving nearer kickoff. Future-dated or expired entries are discarded. No persistent prediction-cache writes occur.

## 14. Single-flight

**IMPLEMENTED**, shared in-process cache/lock/Future map, maximum **32 different in-flight keys**, waiting callers bounded to **50 seconds**. Equivalent requests reuse the owner result. Owner failure clears the flight; controlled failures receive a **30-second cooldown** to avoid an immediate equivalent-request storm. Cache/failure maps are bounded. HTTP uses 10-second connect and 45-second read timeouts; these are transport inactivity limits, not a guaranteed total wall-clock deadline for a trickling response.

Twenty concurrent equivalent searches are tested to make one Gemini request. Single-flight is **not distributed across application processes**; durable quota reservations are. Future multi-worker public deployment would need shared deduplication and an absolute request deadline. A custom loader must also honor bounded execution; waiting callers cannot wait indefinitely.

## 15. Application quota and ticket budget

**IMPLEMENTED**: `UsageGuard` uses its own SQLite ledger/table (`audit_usage`) with atomic `BEGIN IMMEDIATE` reservations shared across processes/restarts. It refuses an existing unrelated database and accepts an optional explicit prediction-DB path exclusion. No production schema/table is added. Use one persistent dedicated ledger path in deployment; diagnostic CLI supports `--usage-file`, default `data/ticket_audit_usage.sqlite3` when its directory exists.

Counters: attempted requests, grounded requests, failures, rate-limit responses, cache hits/misses. Reserve **before** HTTP, count failed requests too, Pacific-date reset, default **20 requests/day**, administrative ceiling **100/day**; zero/invalid/exhausted budget fails closed. Models calls through the diagnostic consume the application budget too. This is intentionally below the published Search allowance and is not a substitute for Google's project quota or disabled billing. Other clients using the project are outside these counters.

All local legs resolve first, then cached research, then at most **three uncached research attempts per ticket**, including failures; maximum 20 legs. No automatic multi-market batching: reliable grounded fixture/market attribution and quota efficiency could not be validated live. Duplicate successful legs naturally reuse cache. This phase does not use Google's paid Batch API.

Settings added only to `.env.example`: `GEMINI_API_KEY`, `GEMINI_AUDIT_MODEL`, `AUDIT_GEMINI_ENABLED` (default false), `AUDIT_GEMINI_FREE_TIER_CONFIRMED` (default false), `AUDIT_GEMINI_MAX_REQUESTS_PER_DAY` (20). Enablement, key, free-project attestation and reviewed model gates must all pass. Diagnostics require `--live`; `--confirm-free-tier` explicitly records the operator's attestation for that command. No real `.env` change was made.

## 16. NormalizedTicket

Frozen detached schema: `input_type`, nullable `source_bookmaker`, future nullable `source_code`, nullable `submitted_combined_odds`, `selections`, bounded HIGH/MEDIUM/LOW extraction confidence and warnings. Selection fields: nullable sport, competition, home, away, aware kickoff, market family, selection, numeric line, submitted odds, source event ID and raw text; optional canonical event ID and period/stat/entity support exact matching. Missing fields are null. Source code is a future adapter slot, not a decoder.

## 17. FixtureEvidence / ResearchEvidence

Fixture evidence contains identity, sport/kickoff, LOCAL/RESEARCH/MIXED source vocabulary, local match/research evidence, freshness/completeness, limitations and provenance. Current orchestration chooses local exact evidence or research, not a fabricated merge. Local match has EXACT/MISSING/AMBIGUOUS/UNAVAILABLE status, original persisted record, available metrics, table provenance and evaluation time.

Research evidence contains exact fixture and selection coordinates; facts with category/claim/value/source title/URL/observation time/application quality; **separate interpretations**; limitations; research confidence (UNASSESSED rather than model-selected completeness); searched time/model/schema; bounded qualitative verdict; and copied grounding metadata. Missing credible injury data receives an explicit UNKNOWN limitation. No Gemini-derived model-probability field exists.

## 18. SelectionAudit

Original submitted selection, bounded verdict (STRONG/REASONABLE/RISKY/VERY_RISKY/INSUFFICIENT_EVIDENCE), deterministic evidence level, actual local and market probability if present, strengths/concerns, researched key facts, limitations, nullable safer alternative, provenance and fixture evidence. Gemini is never asked for a win percentage. Extra probability/alternative fields are rejected. Research-only numerical local/market probabilities are always null. External fact values remain source claims, not our model output.

## 19. TicketAudit

Contains audited selections, strongest/weakest selection indices, overall qualitative verdict, evidence-level counts, limitations and generation time. No accumulator probability, staking recommendation, settlement record or booking code is computed. Provisional strongest/weakest grouping reflects evidence completeness; it does not rank incomparable probability models.

## 20. Evidence-level policy

Deterministic proposed thresholds:

- **STRONG_LOCAL**: exact fresh match; valid stored model/market probabilities; stored independent probability with `p_model_basis=independent`; at least three recorded bookmakers; reference fair books and recorded reference EV.
- **GOOD_LOCAL**: exact match with valid recorded model/market probabilities, evaluation timestamp at most six hours old, not future-dated/expired/started. This includes clearly labeled market-derived estimates.
- **RESEARCHED**: fresh supported research, at least three credible A/B/C facts across two hostnames, at least one A fact, no explicit unresolved contradiction. Fact observation times must exist, not be future, and be at most seven days old for historical/form categories or 24 hours for dynamic categories.
- **LIMITED**: exact local quotes/metrics with missing freshness/core fields, or fresh credible research below the above completeness requirements.
- **INSUFFICIENT**: no exact/usable evidence, unsupported/ambiguous identity, no fresh credible research, or controlled failure.

Evidence completeness is not confidence a bet wins. Default local verdict is conservative REASONABLE with recorded nonnegative EV, otherwise RISKY when evidence qualifies. LIMITED/INSUFFICIENT evidence yields INSUFFICIENT_EVIDENCE. A Gemini qualitative verdict is retained only if RESEARCHED criteria pass. These policies need reviewer approval/calibration before public use; no production scoring was changed.

## 21. Safer alternatives

**Deferred recommendation policy**; the schema is implemented, but current `safer_alternative` is always null. The archive demonstrably contains genuine previously evaluated same-fixture alternatives. A later deterministic comparison can offer only those exact evaluated coordinates with timestamp/provenance/appropriate settlement context. Gemini alternatives and unverified replacements are rejected now. No new market is generated.

## 22. Privacy/security

Research sends only minimal sports coordinates/current time/market context. No Telegram identity, subscriber status, community membership, submitted raw text or account identifiers are sent for research. Extraction uses explicit ephemeral slip text/image; future user-facing intake must redact private account data and disclose free-tier handling before submission. No public intake exists in this phase.

Keys are absent from dataclass repr, URLs, printed response errors and logs; sent only in `x-goog-api-key` to the fixed Google HTTPS origin. Redirects are disabled. No raw HTTP error body/headers/exception text is printed. The diagnostic only uses synthetic data, does nothing network-related without `--live`, and cannot run during production startup. Unknown configured models print `unsupported`, not arbitrary environment text.

Google's unpaid-service terms permit product improvement and human review of inputs/outputs; sensitive personal information should not be submitted. Search grounding has additional prompt/context/output retention terms (30 days) and attribution/display obligations. Metadata is retained for future compliant display, but no public renderer is implemented. [Official Gemini terms](https://ai.google.dev/gemini-api/terms).

## 23. Failure behavior

Controlled reasons cover disabled/missing key/free attestation, unsupported/unavailable model, rejected key/capability, local-budget exhaustion/unavailable ledger, 429, timeout/transport failure, redirects/other rejected HTTP, 5xx, safety refusal/truncation, malformed/empty outputs, extraction mismatch, missing grounding/support/queries, unsupported facts, coordinate mismatch, ambiguity, unsupported/started fixtures and per-ticket limits. No fallback, automatic retry, billing enablement, sports-provider lookup or alternative paid service exists. Contradictions downgrade completeness; incomplete injuries remain UNKNOWN rather than a claim of healthy squads.

## 24. Tests

New regressions: **123**, all mocked/offline. Guarded sports-provider/generation isolation applies to every test. Coverage includes archive/picks/snapshot/raw-cache reads; whole-DB immutability; canonical and ambiguous fixtures; age/women/reserve qualifiers; exact/signed lines/period/stat/entity; local metrics; all gates/errors; malformed/injected outputs; coordinate echo; grounding provenance; weak/contradictory/stale facts; UNKNOWN injuries; sport/market prompts; cache keys/TTL/eviction/detached data; 20-request single-flight/cooldown/bounded waits; 20-leg budget; atomic durable quota/restart/reset; nullable text/image extraction; synthetic PNG; and offline diagnostic behavior. New root diagnostic is also covered by the existing all-root-script read-only regression.

Targeted result: **123 passed**. Full suite: **1,915 passed in 151.85 seconds (2:31)**; zero failures. This is the 1,791-test pre-phase suite plus 123 new audit cases and one existing dynamically parameterized root-script check for the new diagnostic. Command:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/phase2c1-full-20261007a
```

No live Gemini calls are made by pytest. Existing production behavior is additionally covered by the unchanged full suite.

## 25. Blockers and boundaries

The configured project's **generation POST rejects the preferred model (404)**. Free grounded research, structured extraction and image input cannot be claimed working. Google's current notice makes legacy access restrictions plausible, but actual project eligibility/error reason beyond HTTP status was not independently established. Current live source metadata is **not available**.

Research cache and single-flight are process-local; a multi-worker future UX needs shared deduplication and absolute HTTP deadline enforcement. Source-domain ownership, proxy-source handling, attribution rendering and the provisional evidence/TTL/verdict policy require review. No public UI, subscription gating, tracking or booking-code conversion is implemented. No claim is made that grounded search alone independently verifies every source's truth or fixture attribution.

## 26. Recommendation and changed files

**NO-GO for the next public Ticket Audit integration phase on this model/project.** The isolated local foundation and sports-provider boundary pass. Next scope: establish generation access on an eligible **billing-disabled** project/model; verify current free Search eligibility first; run one text/structured request, one harmless grounded request and one synthetic-image request, retaining real source/support schemas and observing quota failures without exhausting quota. Do not migrate automatically to a model whose grounding requires billing. If no free grounded model is available, keep the feature local-only and research disabled.

After validation passes: review actual grounding provenance/attribution, source ownership, deterministic completeness and alternative policy; harden shared deployment limits/deadlines; then design private audit intake in a separately authorized phase. Keep the zero-sports-provider invariant and no-tracking rule throughout.

Files added: `predbot/ticket_audit/__init__.py`, `schemas.py`, `local.py`, `gemini.py`, `research.py`, `service.py`; `diagnose_gemini_audit.py`; `tests/test_ticket_audit_foundation.py`; this report. File modified: **`.env.example` only**, adding disabled/blank example audit settings. Earlier untracked Betloy diagnostic/report/tests were preserved unchanged. Real `.env`, `main.py`, production modules and existing database schema were not changed.

## 27. Phase 2C.1B — actual project model access (2026-10-07)

### Outcome

**A current stable free text model works: `gemini-3.5-flash-lite`. The API is usable. NO-GO for the current Ticket Audit research architecture:** this model's Developer API Google Search grounding is paid-only, and the single structured-output attempt timed out while both normalized-ticket extraction requests returned 400. No billing was enabled and no paid capability was tested. No Telegram/architecture/prediction changes were made.

Recommended `GEMINI_AUDIT_MODEL` is now **`gemini-3.5-flash-lite`**, centralized in `model_access.py` and reflected in `.env.example`. The user's real `.env` remains untouched, including its existing model setting. Diagnostics used an explicit `--model` override. All ordinary Gemini operations still use one configured model; they never switch models automatically. `probe --discover` is an explicit, isolated diagnostic discovery/probe workflow, not runtime fallback.

### What the original 404 establishes

The original 2.5 Flash response body was not retained, so its exact message cannot be recovered without another request; it was **not re-probed**. Its cause is consistent with Google's documented legacy-access policy, but cannot be definitively attributed from its old status alone.

The **one new 2.5 Flash-Lite probe did establish a new-user restriction**, returning HTTP 404 / NOT_FOUND with this exact sanitized message:

```text
This model models/gemini-2.5-flash-lite is no longer available to new users. Please update your code to use models/gemini-3.5-flash-lite for the latest features and improvements. We recommend you to use the Interactions API (https://ai.google.dev/gemini-api/docs/get-started).
```

The improved classifier categorizes that message as PROJECT_NOT_ENTITLED, preserving the original NOT_FOUND status rather than inventing a different Google status. Google says the 2.5 models remain served for prior active users, not generally available to new users. [Official notice](https://ai.google.dev/gemini-api/docs/deprecations).

### Actual model catalog

One guarded `models.list` GET returned HTTP 200, **62 records**, no next page. **45** advertise `generateContent`. The successful listing alone proves neither entitlement nor free capability. Every record's name/display/version/methods/limits and useful sampling/thinking metadata was captured safely in the validation tool output; the table below preserves the inventory. No additional catalog request was made to reconstruct it.

Method abbreviations below are **exact sets**: G = `generateContent`, C = `countTokens`, T = `createCachedContent`, B = `batchGenerateContent`; E = `embedContent,countTextTokens,countTokens,asyncBatchEmbedContent`; A = `generateAnswer`; P = `predictLongRunning`; L = `bidiGenerateContent`; M = `bidiGenerateMusic`. Model names omit the common `models/` prefix. “—” means not exposed, not false/zero.

| Model | Display name | Version | Methods | Input/output token limits | Thinking exposed |
| --- | --- | --- | --- | --- | --- |
| gemini-2.5-flash | Gemini 2.5 Flash | 001 | GCTB | 1048576 / 65536 | true |
| gemini-2.5-pro | Gemini 2.5 Pro | 2.5 | GCTB | 1048576 / 65536 | true |
| gemini-2.5-flash-preview-tts | Gemini 2.5 Flash Preview TTS | gemini-2.5-flash-exp-tts-2025-05-19 | GC | 8192 / 16384 | — |
| gemini-2.5-pro-preview-tts | Gemini 2.5 Pro Preview TTS | gemini-2.5-pro-preview-tts-2025-05-19 | GCB | 8192 / 16384 | — |
| gemma-4-26b-a4b-it | Gemma 4 26B A4B IT | 001 | GC | 262144 / 32768 | true |
| gemma-4-31b-it | Gemma 4 31B IT | 001 | GC | 262144 / 32768 | true |
| gemini-flash-latest | Gemini Flash Latest | Gemini Flash Latest | GCTB | 1048576 / 65536 | true |
| gemini-flash-lite-latest | Gemini Flash-Lite Latest | Gemini Flash-Lite Latest | GCTB | 1048576 / 65536 | true |
| gemini-pro-latest | Gemini Pro Latest | Gemini Pro Latest | GCTB | 1048576 / 65536 | true |
| gemini-2.5-flash-lite | Gemini 2.5 Flash-Lite | 001 | GCTB | 1048576 / 65536 | true |
| gemini-2.5-flash-image | Nano Banana | 2.0 | GCB | 32768 / 32768 | — |
| gemini-3-flash-preview | Gemini 3 Flash Preview | 3-flash-preview-12-2025 | GCTB | 1048576 / 65536 | true |
| gemini-3.1-pro-preview | Gemini 3.1 Pro Preview | 3.1-pro-preview-01-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3.1-pro-preview-customtools | Gemini 3.1 Pro Preview Custom Tools | 3.1-pro-preview-01-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3.1-flash-lite-preview | Gemini 3.1 Flash Lite Preview | 3.1-flash-lite-preview-03-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3.1-flash-lite | Gemini 3.1 Flash Lite | 3.1-flash-lite-05-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3-pro-image-preview | Nano Banana Pro | 3.0 | GCB | 131072 / 32768 | true |
| gemini-3-pro-image | Nano Banana Pro | 3.0 | GCB | 131072 / 32768 | true |
| nano-banana-pro-preview | Nano Banana Pro | 3.0 | GCB | 131072 / 32768 | true |
| gemini-3.1-flash-image-preview | Nano Banana 2 | 3.0 | GCB | 65536 / 65536 | true |
| gemini-3.1-flash-image | Nano Banana 2 | 3.0 | GCB | 65536 / 65536 | true |
| gemini-3.1-flash-lite-image | Nano Banana 2 Lite | 3.0 | GCB | 65536 / 65536 | true |
| gemini-nano-banana-2.1 | Nano Banana 2.1 | 3.0 | GCB | 65536 / 65536 | true |
| gemini-3.5-flash | Gemini 3.5 Flash | 3.5-flash-05-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3.5-flash-lite | Gemini 3.5 Flash Lite | 3.5-flash-lite-07-2026 | GCTB | 1048576 / 65536 | true |
| gemini-omni-flash-preview | Gemini Omni Flash Preview | 001 | GC | 131072 / 65536 | true |
| gemini-omni-1.1-flash | Gemini Omni 1.1 Flash | 001 | GC | 131072 / 65536 | true |
| gemini-3.5-transcribe | Gemini 3.5 Transcribe | 3.5-transcribe-08-2026 | GC | 98304 / 32768 | true |
| gemini-3.6-flash | Gemini 3.6 Flash | 3.6-flash-07-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3.7-flash | Gemini 3.7 Flash | 3.7-flash-08-2026 | GCTB | 1048576 / 65536 | true |
| gemini-3.8-flash | Gemini 3.8 Flash | 3.0 | GCTB | 1048576 / 65536 | true |
| lyria-3-clip-preview | Lyria 3 Clip Preview | lyria-3-clip-preview | GC | 1048576 / 65536 | — |
| lyria-3-pro-preview | Lyria 3 Pro Preview | lyria-3-pro-preview | GC | 1048576 / 65536 | — |
| lyria-3.5 | Lyria 3.5 | 3.5 | GC | 1048576 / 65536 | — |
| gemini-3.1-flash-tts-preview | Gemini 3.1 Flash TTS Preview | 3.1-flash-tts-preview | GCB | 8192 / 16384 | — |
| gemini-3.8-flash-tts | Gemini 3.8 Flash TTS | 3.8-flash-tts | GCB | 8192 / 16384 | — |
| gemini-3.8-flash-lite-tts | Gemini 3.8 Flash Lite TTS | 3.8-flash-lite-tts | GCB | 8192 / 16384 | — |
| gemini-robotics-er-2-preview | Gemini Robotics-ER 2 Preview | 2-preview | GCTB | 131072 / 65536 | true |
| gemini-2.5-computer-use-preview-10-2025 | Gemini 2.5 Computer Use Preview 10-2025 | Gemini 2.5 Computer Use Preview 10-2025 | GC | 131072 / 65536 | true |
| antigravity-preview-05-2026 | Antigravity Agent Preview | 0.1 | GC | 1048576 / 65536 | — |
| antigravity-preview-09-2026 | Antigravity Agent Preview | 0.1 | GC | 1048576 / 65536 | — |
| antigravity-preview-latest | Antigravity Agent Preview Latest | Antigravity Agent Preview Latest | GC | 1048576 / 65536 | — |
| deep-research-max-preview-04-2026 | Deep Research Max Preview (Apr-21-2026) | deepthink-exp-05-20 | GC | 131072 / 65536 | true |
| deep-research-preview-04-2026 | Deep Research Preview (Apr-21-2026) | deepthink-exp-05-20 | GC | 131072 / 65536 | true |
| deep-research-pro-preview-12-2025 | Deep Research Pro Preview (Dec-12-2025) | deepthink-exp-05-20 | GC | 131072 / 65536 | true |
| gemini-embedding-001 | Gemini Embedding 001 | 001 | E | 2048 / 1 | — |
| gemini-embedding-2-preview | Gemini Embedding 2 Preview | 2 | E | 8192 / 1 | — |
| gemini-embedding-2 | Gemini Embedding 2 | 2 | E | 8192 / 1 | — |
| aqa | Model that performs Attributed Question Answering. | 001 | A | 7168 / 1024 | — |
| veo-3.1-generate-preview | Veo 3.1 | 3.1 | P | 480 / 8192 | — |
| veo-3.1-fast-generate-preview | Veo 3.1 fast | 3.1 | P | 480 / 8192 | — |
| veo-3.1-lite-generate-preview | Veo 3.1 lite | 3.1 | P | 480 / 8192 | — |
| gemini-3.5-transcribe-live | Gemini 3.5 Transcribe Live | 3.5-transcribe-live-08-2026 | L | 131072 / 65536 | — |
| gemini-2.5-flash-native-audio-latest | Gemini 2.5 Flash Native Audio Latest | Gemini 2.5 Flash Native Audio Latest | CL | 131072 / 8192 | true |
| gemini-2.5-flash-native-audio-preview-09-2025 | Gemini 2.5 Flash Native Audio Preview 09-2025 | gemini-2.5-flash-preview-native-audio-dialog-2025-05-19 | CL | 131072 / 8192 | true |
| gemini-2.5-flash-native-audio-preview-12-2025 | Gemini 2.5 Flash Native Audio Preview 12-2025 | 12-2025 | CL | 131072 / 8192 | true |
| gemini-3.1-flash-live-preview | Gemini 3.1 Flash Live Preview | 3.1-flash-live-03-2026 | L | 131072 / 65536 | — |
| gemini-3.8-live | Gemini 3.8 Live | 3.1-flash-live-03-2026 | L | 131072 / 65536 | — |
| gemini-3.8-live-extended-thinking | Gemini 3.8 Live Extended Thinking | 3.1-flash-live-03-2026 | L | 131072 / 65536 | true |
| gemini-robotics-er-2-streaming-preview | Gemini Robotics-ER 2 Streaming Preview | 2-streaming-preview | L | 131072 / 65536 | true |
| gemini-3.5-live-translate-preview | Gemini 3.5 Live Translate Preview | 3.5-live-translate-06-2026 | L | 16384 / 65536 | — |
| lyria-realtime-exp | Lyria Realtime Experimental | lyria-realtime-exp | M | 1048576 / 65536 | — |

Additional exposed sampling metadata: generative records normally report temperature 1, topP .95, topK 64 and maxTemperature 2. Image-generation-family records instead report maxTemperature 1; the three Antigravity records expose none of these values. Embedding/Veo/Lyria-realtime records expose none. AQA reports temperature .2, topP 1, topK 40 and no maxTemperature. The listed native/live speech records expose temperature 1/topP .95/topK 64/maxTemperature 2. These are catalog defaults, not model availability or billing evidence.

### Candidate selection and official free-capability evidence

The intersection of actual catalog records, `generateContent`, stable status and reviewed standard-tier free capability gives **six candidates**: `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`. The already failed 2.5 Flash was skipped. Flash-Lite was tested first for its documented free Search possibility; the newer stable Lite followed. **No other models were probed after success.**

| Model | Text free | Structured free | Image input free | Developer API Search free | Actual generation |
| --- | --- | --- | --- | --- | --- |
| 2.5 Flash | Yes, legacy eligibility | Yes, legacy eligibility | Yes, legacy eligibility | Yes, legacy eligibility | Prior-phase 404; not retried |
| 2.5 Flash-Lite | Yes, legacy eligibility | Yes, legacy eligibility | Yes, legacy eligibility | Yes, legacy eligibility | 404, new-user restriction |
| 3.5 Flash-Lite | Yes | Yes | Yes | **No** | **200, OK** |
| 3.6 Flash | Yes | Yes | Yes | **No** | Not probed |
| 3.7 Flash | Yes | Yes | Yes | **No** | Not probed |
| 3.8 Flash | Yes | Yes | Yes | **No** | Not probed |

Cost evidence: [current official pricing](https://ai.google.dev/gemini-api/docs/pricing). Capability evidence: [3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite), [structured outputs](https://ai.google.dev/gemini-api/docs/structured-output), [thinking configuration](https://ai.google.dev/gemini-api/docs/thinking), [REST configuration](https://ai.google.dev/api/generate-content).

Excluded without requests: model aliases can drift; embedding/audio/music/image-generation/agent models do not match this feature; experimental/preview or unreviewed capabilities fail closed. 3.1 Flash-Lite has an announced shutdown schedule and was not tested. 3.5 Flash's **standard** text tier is not documented free; no priority-tier workaround was attempted. Live/bidirectional models require a different API architecture and were not pursued. Nothing was tested simply because it appeared in the list.

### Sequential live results and exact errors

| Phase 2C.1B operation | HTTP / provider status | Result |
| --- | --- | --- |
| models.list, one page | 200 | PASS: 62 actual model records |
| 2.5 Flash-Lite, text-only probe, 16-output-token limit | 404 / NOT_FOUND | FAIL: exact new-user message above |
| 3.5 Flash-Lite, text-only probe, 16-output-token limit | 200 | **PASS**, exactly `OK`; serviceTier `standard`; 60 prompt tokens + 1 candidate token = 61 observed total tokens |
| 3.5 Flash-Lite, one tiny structured-output request, 128-output-token limit | No response observed | **FAIL / UNVALIDATED**, fixed reason `gemini_timeout`; no provider HTTP/status/message received |
| 3.5 Flash-Lite, Search | **No request made** | **SEARCH_GROUNDING_PAID_ONLY**, blocked before HTTP/quota reservation |
| 3.5 Flash-Lite, one synthetic inline PNG ticket extraction | 400 / INVALID_ARGUMENT | **FAIL**, exact message `Request contains an invalid argument.` |
| 3.5 Flash-Lite, one synthetic pasted-ticket extraction | 400 / INVALID_ARGUMENT | **FAIL**, exact message `Request contains an invalid argument.` |

The synthetic two-leg input is Arsenal–Chelsea / result3 home / odds 1.75 and Barcelona–Sevilla / total over 2.5 / odds 1.62, combined odds visibly 2.84. Sport/competition/kickoff/bookmaker/event IDs/period/stat/entity are deliberately invisible and must remain null. The PNG is generated in memory with no OCR service, upload or image persistence. Text extraction uses no Search.

The tiny schema used `status` and `confidence`, both required strings with no extra properties. The ticket adapters used the existing `NormalizedTicket` JSON contract. HTTP 400 is **not proof that the model lacks image understanding or free extraction**: both text and image payloads were rejected generically, without a field-specific explanation. It suggests a request/structured-schema compatibility issue, but the exact cause was **not established**. No replacement schema/API, extra retry or undocumented paid capability was tried. The timeout cannot establish structured-schema compatibility either.

There is **no actual grounded source metadata** from this phase. Metadata/chunk/title/URL/support/query parsing is regression-tested with mocks, not represented as successful live Search. Google AI Studio testing is not evidence of a free Developer API request; the paid-tier free Search allowance does not make a billing-disabled project eligible.

### Diagnostic improvements and preserved cost protection

`models` now captures useful metadata for all actual records with bounded pagination (maximum three pages, each reserved against the durable budget). `probe` sends one tiny text-only request; explicit `probe --discover` intersects actual catalog with the reviewed policy, skips explicitly known failures, limits probes to three, stops on the first success, and stops immediately for auth/billing/quota/malformed/transport failures. No retry exists in an ordinary Gemini client.

`GeminiHTTPError` keeps fixed safe service reason codes while exposing sanitized HTTP status, Google's status/message and diagnostic classification. The classifier distinguishes new-user entitlement, billing, free-tier unavailability, authentication, quota/rate limits, malformed request, unavailable/invalid model and generic NOT_FOUND; Google's ambiguous API-version/model-support wording remains explicitly ambiguous. Configured/URL-encoded keys, recognizable Google keys, Authorization/Bearer and key/token assignments are redacted. Raw bodies/headers are not logged. Structured parser failures remain fail closed.

The selected model uses Gemini 3 `thinkingLevel=MINIMAL`; legacy 2.5 uses `thinkingBudget=0`. These protocol choices are centralized with free-capability policy. Search's paid-only gate applies before a request is reserved, including the lower-level request helper. The cache, single-flight, durable daily quota, local reader and audit orchestration were **not changed**. Normal generation uses the configured model only. No existing sports provider is instantiated or invoked by this phase.

Six live Google HTTP attempts were made **in Phase 2C.1B**: one GET + five generation attempts (two probes, structured, image, text). Exactly one returned a successful generation. Zero Search HTTP requests, zero sports API requests. Across both phases: **eight Google HTTP attempts**; the dedicated ledger contains seven because the original Phase 2C.1 one-off catalog GET preceded it. Current ledger: requests **7**, grounded **0**, failures **5**, rate-limits **0**. Only the successful tiny probe exposes token usage; consumption for rejected/timed-out attempts is not known.

Billing remains **disabled per the user's explicit project confirmation**; no code changes it, and no billing API was called. Google does not expose the billing tier through models.list. Free text is supported by the reviewed pricing, that confirmation and the successful standard-tier request, not by model listing alone.

### Regression tests and full suite

Added `tests/test_gemini_model_access.py` with **76 new mocked cases** for catalog metadata/pagination/filtering, visible-but-unavailable legacy models, candidate selection/stop/budgets, new-model success, exact sanitized messages and redaction, billing/free-tier/quota gates, structured parsing, grounded provenance/missing metadata, two-leg image/text extraction and nulls, configured-model usage, no runtime fallback, durable quota restart and offline diagnostics. Every case inherits explicit forbidden sports-provider/generation/network guards.

The prior 123 foundation tests now explicitly use the legacy free-Search model for mocked research so they continue exercising the research path independently of the new selected model's paid-only Search gate. This is a test fixture, not production fallback. Existing production behavior remains unchanged. **Full suite: 1,991 passed in 105.75 seconds (1:45), zero failures.** This is the previous 1,915 cases plus 76 new model-access regressions:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/phase2c1b-full-20261007a
```

### Changed files and next scope

Phase 2C.1B adds `predbot/ticket_audit/model_access.py` and `tests/test_gemini_model_access.py`; updates `predbot/ticket_audit/gemini.py`, `diagnose_gemini_audit.py`, `.env.example`, the model fixture import in `tests/test_ticket_audit_foundation.py`, and this report. Real `.env`, prediction/provider/Telegram code, storage schema, research cache, single-flight and the existing quota-ledger implementation remain unchanged.

**NO-GO for the requested free grounded Ticket Audit architecture.** This phase successfully resolves basic free generation access, not the Search requirement. Recommended next scope: a narrowly authorized request/schema compatibility investigation for structured and ticket extraction with a fresh explicit call budget; separately decide whether an eligible legacy free-Search project/model exists. If none does, keep research disabled and consider a separately authorized local-only feature. Do not enable billing, add paid Search, split models, switch to Live API or redesign the architecture without a new scope. Do not build Telegram intake/menu/subscriptions yet.


## 28. Phase 2C.2 - free research and extraction compatibility

This section records the current implementation and supersedes the blockers/next steps in section 27. Earlier sections remain historical evidence. No Telegram Ticket Audit feature was added.

### A. Established HTTP 400 cause and REST comparison

The controlled comparison established a request-level culprit: **`selections.maxItems: 20` in the complete 15-field nullable selection schema**. The full schema returned 400; removing ONLY that field from the same endpoint/model/prompt/config returned 200 and the correct two-leg ticket. The 20-leg limit remains enforced by `ticket_from_json`. Small bounded arrays work: a nested one-item probe with `maxItems: 2`, enums, nullable strings and `additionalProperties: false` returned 200. The keyword is not universally unsupported.

Google documents schema complexity as a rejection condition; decoder complexity is the likely mechanism, **not a field-specific explanation supplied by Google**. The earlier 400 responses were generic. This isolates the offending full-schema constraint without claiming access to Google's internal decoder. [Structured output contract and limitations](https://ai.google.dev/gemini-api/docs/structured-output).

The client also adopts the current documented `generationConfig.responseFormat.text = {mimeType: "APPLICATION_JSON", schema: ...}`. Deprecated `responseMimeType`/`responseJsonSchema` fields are omitted. A controlled tiny request using the old fields timed out; this does NOT prove those fields caused the 400. Unsupported/malformed schemas now fail locally before quota/HTTP. [Current REST response format](https://ai.google.dev/api/generate-content#TextResponseFormat).

| Field | Comparison / outcome |
| --- | --- |
| Endpoint/version/model | Same v1beta `models/gemini-3.5-flash-lite:generateContent`; no SDK/version/model switching |
| Contents/role/parts | Same user role, text parts; image adds one inlineData part |
| Response format | Documented enum MIME/schema structure above; tiny schema passed |
| Full ticket schema | Objects, required fields, null type arrays, enum input/confidence, nested selections and warning array preserved; only selections.maxItems omitted |
| Output budget | 128 for minimal, 256 for shape probe, 4096 for ticket/analysis; bounded locally |
| Thinking | Same MINIMAL; no paid/priority tier override |
| Image | PNG, locally checked CRC/chunks/decompression, base64 ASCII, inlineData.mimeType/data; same ticket schema |
| Local limits | Strict root/selection fields, types, nulls, aware dates, finite numbers and 1-20 legs; PNG/JPEG signatures and 5 MB limit before HTTP |

### B-D. Sequential live validation

| Operation, all on confirmed billing-disabled Free tier | Result |
| --- | --- |
| One-field status schema, expected status OK | **PASS, 200**, exact expected object; 70 observed tokens |
| Same tiny request, old schema fields (diagnostic A/B only) | Timeout; no status observed, no retry |
| Arrays + nullable + nested objects + enum probe | **PASS, 200**, expected item with null; 97 observed tokens |
| Natural pasted ticket, original full bound | 400 INVALID_ARGUMENT; generic invalid-argument message |
| Identical pasted request, only maxItems removed | **PASS, 200**, two legs, submitted odds 1.75/1.62 and total 2.84; 585 observed tokens |
| ONE image request, same natural test ticket and fixed schema | **PASS, 200**, two correct legs/odds; all invisible fields null |
| Supplied public-evidence analysis, no tools | **PASS, 200**, ten supplied facts; appropriately INSUFFICIENT_EVIDENCE |

The requested natural pasted input was used verbatim. Sport, competition, kickoff, bookmaker, booking code, event IDs and period/stat/entity stay null. The ambiguous team-to-win market can stay null: absent sport/rules, a two-way versus three-way market is not established. Participant/side and Over 2.5 normalization preserve visible meaning. Extraction itself made **no fixture-research request**. Image support is validated on this synthetic PNG; real camera photos, multi-page/poor-quality slips and broad OCR accuracy are not established. No paid OCR or image generation service was used. [Official image input documentation](https://ai.google.dev/gemini-api/docs/image-understanding).

**This phase made seven Gemini HTTP attempts**, zero Search attempts. The shared historical Google ledger moved from 7/0/5 to **14 requests / 0 grounded / 7 failures**, rate-limits 0. No budget was reset or increased. Historical all-phase total is 15 Google attempts because one original catalog GET preceded the ledger. Unknown token consumption for rejected/timed-out calls is not presented as zero.

### E-G. Source investigation and feasibility matrix

Y = documented available; D = derivable from published records (not necessarily implemented); P = partial/no freshness guarantee; H = historical only; U = not verified; N = unavailable in the reviewed source. Availability is not permission. The two tables together describe each researched source; U cases are disabled.

| Source | Sports | Reviewed data | Current results | Form | Standings | News | Injuries | H2H | Scoring | Home/away | Structured |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OpenFootball football.json | Football | Fixture dates, participants, scores | P | D | D | N | N | D | Y | Y | JSON |
| nflverse-data | American football | Published games/PBP/team stats; per-asset scope varies | P | D | D | N | U | D | Y | Y | CSV/columnar releases |
| Retrosheet | Baseball | Event/box-score archives through 2025; schedules | H | H | D/H | N | N | H | H | H | Downloadable event/log files |
| NBA.com statistics | Basketball | Statistics covered by site's terms | U | U | U | U | U | U | Y | U | U |
| UFC official site | MMA | Terms reviewed; stats retrieval not attempted | U | U | U | U | U | U | U | U | U |
| ATP official site | Tennis | Access/terms inspection blocked | U | U | U | U | U | U | U | U | U |
| JeffSackmann/tennis_atp candidate | Tennis candidate | Repository/license URLs returned unavailable/404 | U | U | U | U | U | U | U | U | U |
| BBC News RSS candidate | Multiple sports candidate | Feed documentation; current permission not established | U | U | U | P | U | U | U | U | RSS documented |

| Source | Automated access / reuse | Login / key | Free? | Rate limit | Robots / terms concerns | Reliability | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenFootball | Owner explicitly documents raw curl downloads; CC0 reuse | No / no | Yes | No numeric raw-host quota verified; our 3/hour, 20/day | GitHub prohibits excessive bulk access; raw robots file not retrievable; no website scraping/bypass | Community/manual upstream; no current-data SLA | **Accepted, limited EPL adapter** |
| nflverse-data | Automated downloads documented; CC-BY-4.0 repository license | Public downloads no login/key | Yes for reviewed published releases | Asset hosting limit not verified; no calls enabled | Per-asset/upstream rights and attribution need review before enabling; repository software MIT license alone is not data permission | Update schedule documented; current asset availability varies | Candidate for later review; **not enabled** |
| Retrosheet | Data reuse, including commercial, explicitly permitted with attribution | No / no for public archives | Yes | Not published/verified | Programmatic access/robots not established here; no scraping attempted | Curated history; corrections; not current-season news/availability | Historical candidate; **not enabled** |
| NBA.com | Intended use prohibited | U / U | Page access does not establish permitted use | U | Betting use excluded | Official | **Rejected** |
| UFC | Automated acquisition prohibited in reviewed terms | U / U | Reading pages is not permission | No approved rate | Terms exclude robots/page scraping; page open returned 403, not bypassed | Official, access unsuitable | **Rejected** |
| ATP | Permission not established | U / U | U | U | Terms access 403; alternate path unavailable; no bypass | Not validated for this use | **Unsupported** |
| tennis_atp candidate | Current license/access not established | U / U | U | U | Do not rely on remembered license; inspected URLs unavailable | Not validated | **Unsupported** |
| BBC News RSS | Current relevant permission not established | U / U | U | U | Relevant terms blocked/inaccessible; Studios terms cannot establish News permission | Feed/current-data suitability not validated | **Unsupported** |

Source evidence and decision detail:

- **OpenFootball:** its [README](https://raw.githubusercontent.com/openfootball/football.json/master/README.md) explicitly invites raw JSON downloads. [CC0 license](https://raw.githubusercontent.com/openfootball/football.json/master/LICENSE.md) permits reuse. Daily JSON generation does not imply daily upstream updates: the README says the upstream Football.TXT data is not automatically updated. Other leagues in the repository were not enabled. [GitHub acceptable-use policy](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) prohibits excessive automated bulk activity. This adapter downloads an owner-designated public dataset, with no login, CAPTCHA, rotating proxy or evasion.
- **nflverse:** [data repository](https://github.com/nflverse/nflverse-data) explicitly documents programmatic downloads; its [data license](https://raw.githubusercontent.com/nflverse/nflverse-data/main/LICENSE.md) is CC-BY-4.0. The [availability schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html) describes game updates every five minutes and nightly PBP/stat updates, with corrections. This is a credible future dataset candidate, not permission to scrape its upstream vendors or assume every release has identical availability/rights. No NFL adapter was enabled.
- **Retrosheet:** [reuse notice](https://www.retrosheet.org/notice.txt) permits commercial reuse with a prominent prescribed attribution. [Event files](https://www.retrosheet.org/game.htm) list historical seasons through 2025. Historical batting/pitching records cannot establish today's starter, weather, injury or form. No archive was downloaded.
- **NBA:** [terms](https://www.nba.com/termsofuse) exclude statistics use for gambling. No adapter.
- **UFC:** [official terms](https://www.ufc.com/terms) expressly restrict automated acquisition and commercial reproduction; the indexed terms were inspected. No stats access/circumvention.
- **ATP:** [terms path](https://www.atptour.com/en/terms-and-conditions) returned 403. **tennis_atp:** [repository](https://github.com/JeffSackmann/tennis_atp) / license paths were unavailable. No automated-use or current-license permission was inferred.
- **BBC:** [feed documentation](https://support.bbc.co.uk/platform/feeds/NewsFeeds.htm) points to feed terms, but [current relevant terms](https://www.bbc.com/pages/terms-of-use) were inaccessible/robots-blocked. No feed/news adapter or reuse permission was inferred.

No approved current injury/news feed was found in this bounded investigation. No claim is made that the rejected/deferred sources are the only possible sources for those sports.

### H. Implemented adapters and live source check

`ResearchSourceAdapter` separates routing, fixture retrieval and market facts. `FreeResearchResolver` uses explicitly injected adapters and never imports prediction/provider/cache-generation modules. Its default adapter list is empty. V1 enables only `OpenFootballAdapter` when explicitly constructed with its own budget. Selection must supply football, an explicitly English/England Premier League competition, exact source participant names, aware kickoff and full-match goals/points/all coordinates. Generic "Premier League", aliases, other competitions, youth/women/reserves, player props and unsupported periods/stats are not silently matched.

The date-only source must have exactly one directed participant pair within the submitted UTC date +/-1 day. Its lack of timezone/exact kickoff is retained as a limitation, not asserted as exact identity. Multiple candidate fixtures, duplicate conflicting scores, malformed/oversized data and unsupported routes fail closed. Dataset scores accept both current direct arrays and older ft objects. Source quality is C. A single community source **cannot reach RESEARCHED** under the existing official/independent corroboration policy; it retains LIMITED evidence and an insufficient verdict.

Three actual free raw downloads were used, within the durable hourly cap. First, live normalization rejected the previously undocumented mixed score shape. Second, a shape diagnostic failed while formatting its result. Third saved the bytes before formatting and established the mixed array/object shape. After the fix, normalization and analysis reused that saved public response: **zero further source calls**. There were 380 fixture records. The audit used Sunderland AFC versus Brighton & Hove Albion FC, with a deliberately supplied test kickoff for its listed date; the source timezone was NOT inferred or validated. Ten score-derived facts retained RECENT/HISTORICAL labels and unknown upstream freshness. The user's synthetic Arsenal/Chelsea and Barcelona/Sevilla extraction fixtures were never researched.

### I. Cache/reuse, order and budgets

One shared **existing `ResearchCache` implementation** serves namespaced dataset, fixture and market entries; no second cache implementation or prose cache. Market keys retain sport/competition/participants/UTC kickoff, canonical ID, market/side/line/period/stat/entity, source policy/version and schema version. Fixture keys omit market/line so multiple markets share retrieval. Dataset keys identify the season/file. Returned values are detached. The existing single-flight, bounded entries/flights and 30-second negative cooldown apply at each layer.

Existing kickoff-aware TTLs remain: historical facts 3600s over 24h away, 900s within 24h, 120s within 2h, zero once started; dynamic-fact TTLs remain shorter. Dataset cache TTL follows its first requested fixture, never a fabricated source timestamp. Source observation time is actual retrieval; event_date/freshness separately describe records. Cached fixture evidence can create a new market entry without HTTP or another external-research slot.

Audit order: resolve **all** local legs -> check market/fixture cache -> up to **three uncached research slots** (failures consume slots) -> optional Gemini analysis, **one generation per audit**. Twenty markets for one cached fixture are tested to reuse one request/slot. Existing successful exact local matches and ambiguous identities do not invoke research.

`PublicSourceBudget` uses a separate owned SQLite ledger, atomic reservation, **3/source/UTC-hour and 20/source/UTC-day**. Restarts do not reset counts; unrelated prediction/Google databases are refused. Failed requests count. HTTP has 5s connect/10s read timeouts, a 20s streaming elapsed cutoff, 1 MB cap, no redirects/retries/auth/proxies. That elapsed cutoff is checked per chunk, so a stalled read may add up to the read timeout before returning. HTTP errors/429 stop; no alternate source/provider/LLM fallback. The separate Google ledger retains its 20/day default and fail-closed capability/confirmation gates.

### J. Market facts and deterministic grading

The adapter fetches one permitted season file, then emits only relevant score-derived categories: totals plus exact submitted line; both-score observations; named participant goal margins plus submitted signed handicap; or win/draw/loss and home/away context for 1X2/DC/DNB. It does not fetch news, scrape standings, rate opponent strength or compute win probabilities. Last five eligible results per participant, within 90 days, are bounded; source-date records from yesterday, today or the future are conservatively excluded because timezone is unknown. Future/unscored records never become facts. Duplicate H2H observations are deduplicated when identical.

Each fact preserves category/value/claim (the structured text field), source/source_title/source_url, observed_at, event_date, freshness and source_quality. The source payload SHA-256 is retained; raw dataset rows do not escape as market evidence. Dates older than 30 days are HISTORICAL. Unknown injuries, missing timezone, update uncertainty and lack of corroboration remain explicit. Limited or unsupported evidence cannot produce a positive verdict.

Local numeric probabilities remain exactly archived values; research-only probabilities and alternative bets remain null. Mixed LOCAL/RESEARCH/INSUFFICIENT legs retain their individual assessments and evidence counts. A partially usable ticket carries RISKY plus ticket-level limitations; it no longer downgrades every valid leg or labels the whole ticket wholly unsupported because of one missing leg. An entirely unsupported ticket remains INSUFFICIENT_EVIDENCE. No accumulator probability is created.

### K. Gemini analysis boundary

`EvidenceAnalyzer` is optional and explicitly injected. It receives only normalized sports coordinates, whitelisted actual local metrics, structured supplied facts, provenance, limitations and as-of time. Its prompt says: **"Use only the evidence supplied below for current factual claims."** No browsing/tools are sent. It makes zero calls for an entirely insufficient ticket.

Output is strictly verdicts + same-leg supplied fact IDs + predefined concern codes. Unknown/cross-leg IDs, new factual prose, probability/injury/alternative fields, extra/missing/duplicate legs, unsupported conflict/negative-EV claims and positive verdicts on LIMITED/INSUFFICIENT evidence are rejected. Returned factual content is the unchanged input; concerns are locally rendered templates. This is deliberately narrower than trusting model-authored prose to avoid hallucinations. Google/parse failure preserves the deterministic audit and adds an analysis limitation; it cannot cause another lookup or guess.

The live analysis of ten downloaded public facts returned the appropriate insufficient verdict with limited-source/unknown-availability concerns and null probability/alternative. Successful local/independent researched mixed output is also covered with mocks; those mocks are not represented as live corroborated research.

### L. Cost and production firewall

**GEMINI_SEARCH_GROUNDING = PAID_ONLY_FOR_SELECTED_MODEL. No Search HTTP request was made.** No paid tier, billing API, OpenAI/search/scraping/OCR payment, Betloy, booking-code decoder, provider request or fallback was used. Free eligibility is based on [Google pricing](https://ai.google.dev/gemini-api/docs/pricing), reviewed model policy and the user's persistent confirmation of Free tier with Cloud Billing disabled; response serviceTier=standard alone does not establish billing eligibility. No code enables billing.

The audit package imports no production provider/engine/jobs/Telegram/cache-generation/storage modules. Local resolution remains read-only. ProviderChain.collect, ResultsResolver.resolve, model scoring/generation, daily build/store, snapshot refresh and provider event/result methods are forbidden by the inherited tests; actual HTTP is blocked during pytest. No API-Football client entry point exists in the inspected provider modules; the blanket network firewall also prohibits any attempted external call. Production generation, Top/Daily selection, scoring, odds, settlement, cache generation, scheduling, stats, community menu, Custom Picks and delivery were not edited.

### M. Tests and files changed

**92 new cases** in `tests/test_ticket_audit_free_research.py`: minimal/nested REST schema, natural text/image/nulls, 20-leg local enforcement, valid PNG/base64, malformed extraction/schema/image, 400/timeout/failure handling, adapter routing, exact identity, market/provenance, mixed score representations, named participant margins, cache-before-network, fixture/dataset reuse, concurrent single-flight, local-before-research and analysis-last order, mixed leg preservation, 20-market reuse, source errors/redirects/429/cooldown, conflicting/ambiguous data, future/same-day/invalid scores, size/time limits including slow trickles, restart budgets, unreviewed URL firewall, supplied-only analysis and every prohibited field/fallback. Existing model/foundation tests were updated for the new minimal contract and real synthetic PNG.

Final full suite: **2,083 passed in 176.08 seconds (2:56), zero failures**. Prior suite 1,991 + 92 new cases. The earlier full run passed 2,082 cases before the final margin/slow-stream regression. No live calls during pytest.

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/phase2c2-full-20261007b
```

Phase 2C.2 modified/added files (earlier uncommitted phase artifacts were preserved):

| File | Phase 2C.2 change |
| --- | --- |
| predbot/ticket_audit/gemini.py | Current REST format, local schema/image checks, missing-market extraction instruction |
| predbot/ticket_audit/schemas.py | Remove wire-only selections.maxItems; add optional dated/source/freshness provenance and retrieval metadata |
| predbot/ticket_audit/free_research.py | NEW: resolver/adapter protocol, reviewed EPL adapter, durable public-source budget |
| predbot/ticket_audit/analysis.py | NEW: supplied-evidence-only qualitative analyzer |
| predbot/ticket_audit/research.py | Cache-hit hook for existing Researcher interface; existing cache implementation retained |
| predbot/ticket_audit/service.py | Free resolver compatibility, reuse between legs, mixed assessment, optional analysis last |
| diagnose_gemini_audit.py | Tiny/shape probes, requested natural ticket and matching synthetic image |
| tests/test_ticket_audit_free_research.py | NEW: 92 regressions with inherited provider/generation/network guards |
| tests/test_gemini_model_access.py | Update tiny-schema expected output |
| tests/test_ticket_audit_foundation.py | Current response-format assertion and valid generated PNG |
| .env.example | Clarify disabled-until-private-integration comment; existing selected-model/default flags retained |
| TICKET_AUDIT_FOUNDATION_REPORT.md | Latest decision and this A-N section |

Diagnostic ledgers/public samples under ignored .venv are validation artifacts, not production data. The real .env is unchanged; it currently still names gemini-2.5-flash and has audit/confirmation flags false. Key presence was checked without printing it. Live validations explicitly overrode only diagnostic settings. Before an authorized future integration, configure GEMINI_AUDIT_MODEL=gemini-3.5-flash-lite, retain billing disabled, explicitly opt into audit/free-tier confirmation and construct a shared, separate source-usage ledger. Do not enable public/startup functionality now. No new environment variable was necessary for this isolated constructor-injected foundation.

### N. Decision, limitations and exact Phase 2C.3 scope

**CONDITIONAL GO** for private Telegram Ticket Audit V1. Structured/text/image/supplied-analysis PASS; research **PARTIAL**, cache/single-flight PASS, sports-provider isolation PASS, paid API usage **ZERO**. Limited public sources are allowed to return insufficient evidence, which the live analysis did correctly. Current public adapter covers only fully specified supported EPL coordinates and exact dataset names. Other sports/competitions, source freshness/news/injuries and exact public kickoff matching remain unsupported. Data/cache are process-local, but both request budgets are durable. Large evidence payloads can hit the existing 30,000-character Gemini input cap; that fails safely and retains deterministic analysis. Synthetic image success is not broad OCR certification.

Recommended Phase 2C.3 scope: add **private-chat Ticket Audit intake/review only**, pasted text first, optional validated PNG/JPEG; explicit clarification of missing sport/competition/date/aware kickoff, participant identity and market/period/stat/entity; user review before lookup; wire the existing read-only resolver -> shared free resolver/cache/budgets -> optional supplied-evidence analyzer -> independent per-leg rendering with sources/limitations. Preserve local numeric values and explicit insufficient outcomes. Add user throttling/input-size limits and integration regressions proving no provider calls, production writes or public-group processing. Do not add booking codes, Betloy, payment/subscription logic, provider refresh, new research sports, public community audit UX or automatic delivery in that phase. No such Telegram work was implemented here.

## 29. Phase 2C.3 — Private Telegram Ticket Audit V1

### Entry and supported inputs

The Predictions community menu retains its five existing view callbacks and Custom Picks URL, and adds a fifth row: **🔍 Ticket Audit**, using `https://t.me/<same bot username>?start=ticket_audit`. Admin `/menu` posts this new markup through the existing transport, returns/logs its message ID and does not pin it. Existing pinned messages do not update themselves. No menu was posted during implementation.

Private `/start ticket_audit` activates an independent Ticket Audit session and exits Custom Picks mode, without subscribing the user or reading/generating predictions. Ordinary `/start` clears the feature mode and retains the existing welcome/subscription behavior. `/start custom_picks` clears Audit mode and retains the existing Custom Picks behavior; `/stop` clears both modes before its existing unsubscribe action. Existing `/top`, `/daily`, `/stats`, view buttons and admin job routes are unchanged. Old Custom Picks recovery buttons cannot execute while that private user/chat is in Audit mode; existing recovery semantics outside Audit mode are retained.

Group/supergroup/channel `/start ticket_audit` replies with a private deep link when the bot username is available. Ordinary group/channel text or images never enter Audit intake. Polling already accepts message and callback updates, including private photos/documents, so no polling/webhook changes are needed. Channel redirection applies only if a channel update reaches this handler; the current poller's allowed update list still excludes channel posts.

V1 accepts pasted text (8,000-character maximum), Telegram photos and PNG/JPEG image documents. Photos use the largest pixel representation whose reported size is within 5,000,000 bytes. Downloaded contents are independently capped at that size and checked for PNG/JPEG signature; malformed contents are rejected by intake/extraction. `getFile` and download use fixed Telegram hosts, validated relative file paths, no redirects and 5-second connect/10-second read timeouts. The streamed download also enforces a 20-second elapsed limit, checked on each byte; a blocked read can finish later at its read timeout. Data stays in memory; no screenshot files are created.

PDFs, arbitrary documents, voice, video and code-only inputs are rejected with concise supported-input guidance. No booking-code lookup, Betloy, Converter, subscriptions or payments are implemented. Telegram user/chat IDs, filenames and file IDs are never added to Gemini payloads. The submitted text/image itself is sent for extraction; users should omit personal/account details from that content.

### State, clarification and review

Sessions are ephemeral, keyed by `(Telegram user ID, private chat ID)`, limited to 500 concurrent sessions and expire 15 minutes after entry/latest extraction attempt. States are `WAITING → EXTRACTING → CLARIFYING/REVIEWING → PENDING_REVIEW → AUDITING → COMPLETE`, with cancellation/expiry removing state. Restart invalidates all sessions. No audited ticket or screenshot is persisted to prediction tables. The durable request ledgers store usage counts, not submitted tickets or identities.

All extraction uses the existing `GeminiClient.extract`, then `ticket_from_json` validates its normalized output again. Local validation enforces 1–20 legs and distinguishes oversized tickets for friendly copy. **The wire schema still has no `selections.maxItems=20`.** The extraction prompt now describes supplied tickets rather than synthetic tickets; visible-only/no-inference rules remain.

Before confirmation, missing fixture participants, sport, competition, aware kickoff, market/side, required numeric line, period, stat and entity require clarification. These extra coordinates are necessary for this foundation's exact market match, including distinguishing full-match score markets from props. An ambiguous `win` market or identical participant names also requires clarification. Labels are deterministic, for example `Away: Chelsea`, `Market: total`, `Line: 2.5`, `Kick-off: 2030-06-01T15:00:00Z`, `Period: full match`, `Counts: score`, `Applies to: both teams`. Replies only fill fields currently requiring clarification; they do not silently rewrite other fields. Aliases translate explicit user statements into existing coordinates. No fixture discovery or evidence request runs during clarification.

The first unresolved leg is clarified at a time. Two consecutive replies that make no valid progress discard the extraction and ask for a fuller ticket. Successful partial clarification advances the missing-field set; it cannot endlessly reset retries without fixing a field. Unsupported messages during clarification ask for labelled details or Send Again. Fully specified but ambiguous local database matches remain insufficient under the existing resolver; the UI does not guess or learn aliases.

The review shows actual extracted fixtures, picks, market context and odds only. Combined odds appear only when present in the submitted ticket; no multiplication or inferred prices. Review messages use existing splitting with a 3,500-character budget; only the final review carries **✅ Audit Ticket / ✏️ Send Again / ❌ Cancel**. No local evidence lookup, public research or evidence analysis starts before confirmation. Resend discards the pending extraction and rotates its token; Cancel exits the mode.

### Execution, result rendering and deterministic composition

The Telegram controller orchestrates the existing `AuditService`; it does not duplicate matching/research/model logic. Runtime is lazy: menu, entry and normal commands do not construct the backend or create audit ledgers. It requires the selected `gemini-3.5-flash-lite` model and explicit enabled/free-tier/key settings; no automatic model selection or fallback.

The existing service checks **all local legs first**, **research cache second**, then up to **three uncached eligible research legs**, and finally optional supplied-evidence Gemini analysis. A fresh sufficient exact local match makes no public-source lookup. Even an exact limited match or ambiguous local match cannot trigger external fixture guessing. Analysis receives supplied coordinates/metrics/facts only, with the explicit instruction: **“Use only the supplied evidence for current factual claims.”** Gemini cannot browse, add factual prose, probabilities, URLs or alternatives; its output remains bounded enums/fact IDs/risk codes, validated by the existing analyzer.

Results begin with ticket size and `Evidence: N/M selections checked`, followed by groups of at most five leg blocks, split at 3,500 characters, then the weakest assessed leg/overall footer. Each leg uses Strong / Reasonable / Risky / Very Risky / Insufficient Evidence and a plain source label: Today's data / Public data / Limited data. Unknown legs are not counted as checked. A useful local leg remains useful when other legs are unsupported or a source fails.

Numerical probability is shown only when the leg is an exact local match with GOOD_LOCAL/STRONG_LOCAL evidence and its finite 0–1 probability equals the actual stored local model metric. Research-only legs never display a percentage. No accumulator probability or combined odds is computed. Submitted fields are escaped for HTML and bounded; technical error strings and generated arbitrary prose never enter results. V1 does not render safer alternatives because the foundation currently supplies no independently verified alternative; injected/speculative alternatives are omitted.

Application-owned overall rule, evaluated in this order:

1. All legs insufficient → **Insufficient Evidence**.
2. Any Very Risky leg → **Very Risky**.
3. Any Risky or insufficient leg, or at least six selections → **Risky**.
4. A single Strong leg → **Strong**.
5. Otherwise → **Reasonable**.

This is a conservative composition label, not a win/loss prediction for unknown legs or an accumulator probability. The six-leg threshold expresses combined uncertainty without fabricating numeric risk.

Weakest assessed leg: Very Risky before Risky before Reasonable before Strong. Ties use weaker evidence first (`RESEARCHED < GOOD_LOCAL < STRONG_LOCAL`), then lower legitimate local probability when available, then original input order. A missing probability sorts after a supplied local probability within the same verdict/evidence strength; no research percentage is invented. Insufficient legs are excluded from this ranking and remain explicit unknowns. If all legs are insufficient, no weakest leg is forced.

### Security, duplicate protection and failure behavior

Callback payloads contain a 24-character opaque random session token, action and prefix, within Telegram's 64-byte limit. Handlers require the original private user/chat, current token and an unexpired session; confirmation additionally requires the actual current final review message ID. New extraction, resend and mode changes invalidate old callbacks. Tokens/normalized tickets are never shared across users.

An atomic state change to AUDITING makes repeated/concurrent taps acknowledge the existing run instead of starting new work. COMPLETE consumes the ticket before result delivery, so a failed Telegram send cannot rerun research. There are at most two active extraction/audit workers, no unbounded work queue, and at most five extraction attempts per user/chat per rolling hour. Rate records are also bounded to 500 scopes. The default workers are daemon threads, leaving the existing command loop free while external extraction/audit requests run.

Jobs capture their generation token. Cancellation, resend, mode change or expiry prevents a queued job from running and suppresses results from stale in-flight jobs, including replacement tickets already being audited. Cancellation cannot retract a Telegram message or HTTP request already in flight; existing source/Gemini timeouts and per-ticket limits bound that work. A canceled image download is rechecked before Gemini extraction.

Confirmation gets one callback acknowledgement and one progress message. Unsupported content/oversized input uses friendly intake guidance; malformed extraction uses clearer-screenshot/text guidance; quota/model/key/free-tier failure returns temporary unavailability. Sanitized logs contain exception class names, not raw transport errors, credentials or tickets. Failed public research keeps usable local assessments; affected legs remain insufficient. Malformed/unavailable supplied-evidence analysis keeps the existing deterministic assessment. All-insufficient evidence skips Gemini analysis entirely.

### Zero-cost boundaries and persistent counters

Every Gemini operation uses the existing guarded client; no direct Google requests from handlers. A single shared backend per command loop preserves research cache and single-flight across users/tickets. Owned SQLite sidecars sit beside the configured prediction database:

- `ticket_audit_usage.sqlite3`: existing atomic Gemini quota guard, Pacific daily boundary, failures consume reservations, restart-persistent.
- `ticket_audit_sources.sqlite3`: existing public-source budget, at most 3 requests/source/UTC hour and 20/source/UTC day, failures count, restart-persistent.

Keep the prediction database's parent directory writable and persistent so both ledgers survive redeploys. Research data/cache remain process-local. Cache reset on restart never resets these durable counters. No prediction-provider refresh, settlement result lookup, live lookup, paid Search, Gemini Search grounding, OpenAI, paid scraping or paid fallback is reachable through runtime construction or Audit handling. Flags express the user's free-tier attestation; they do not independently inspect Cloud Billing. Keep billing disabled.

Required production values (configure manually; real `.env` was not changed):

```dotenv
GEMINI_API_KEY=<existing secret, never pasted into chat>
GEMINI_AUDIT_MODEL=gemini-3.5-flash-lite
AUDIT_GEMINI_ENABLED=true
AUDIT_GEMINI_FREE_TIER_CONFIRMED=true
AUDIT_GEMINI_MAX_REQUESTS_PER_DAY=20
```

No new environment variables. `DB_PATH`, `TELEGRAM_CHAT_ID`, bot token/admin settings and polling ownership retain their existing meanings. Each extraction and, when applicable, supplied-evidence analysis consumes a reservation; 20 requests/day does not mean 20 complete tickets/day. Configuration is read when the shared backend is first built; restart after changing it. `.env.example` comments now describe the private integration while keeping its safe disabled defaults.

### Phase 2C.3 regression coverage and files

**102 new regression cases** in `tests/test_ticket_audit_telegram.py`, plus the existing menu layout expectations updated for the additional row and the analysis prompt assertion aligned with the requested supplied-evidence instruction. The tests exercise real normalized extraction/validation, service/local matching, free resolver/cache and guarded Gemini contracts with mocked transports. They cover private/deep-link entry, public rejection, mode switches/help, text/image/code/oversize intake, missing/ambiguous coordinates, bounded clarification, review/odds/nulls, screenshot memory/privacy, callback identity/expiry/restart/wrong-message safety, concurrent duplicate taps, stale jobs during replacement/cancel, worker/rate bounds, old recovery isolation, normal view responsiveness, mixed/partial evidence and actual source timeouts, malformed analysis, durable quota exhaustion, numeric/alternative restrictions, deterministic composition, 20-leg output and Telegram download host/size/time/error protection.

The inherited firewall forbids provider collection/result methods, model scoring/generation, daily build/store and snapshot refresh. Additional guards forbid settlement/live entry points and socket connections; real requests are blocked. Database SQL dumps before/after private Audit handling prove no schema, subscriber, membership, pick, settlement, watch, cache, stats, calibration or recap writes. Existing full-suite regressions protect Custom Picks, private view navigation and automatic community delivery. **Real external calls in Phase 2C.3 tests: zero; paid API calls: zero; Search grounding: zero.**

**Final full suite: 2,185 passed in 156.15 seconds (2:36), zero failures.** This includes the previous 2,083 cases plus 102 new private-flow cases. The earlier full run passed 2,177 cases before the final cancellation/rate/odds regressions; the final run includes all final code and test changes.

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/phase2c3-full-20261008b
```

`git diff --check` also passed. No production Telegram requests or live Google/public-source probes were performed in Phase 2C.3; test transports were mocked, with actual external connections forbidden in the new Audit regressions. These are regression results, not completion of the manual acceptance plan.

| Requested result | Phase 2C.3 status |
| --- | --- |
| Community menu updated | YES in code/admin `/menu`; existing pinned menu needs manual replacement |
| Text intake | PASS |
| Image intake | PASS (mocked Telegram/Gemini transport, real image contract) |
| Review/confirmation | PASS |
| Local evidence | PASS |
| Free research | PARTIAL (existing reviewed EPL coverage; no new sources) |
| Mixed evidence | PASS |
| Result rendering | PASS |
| Weakest-leg logic | PASS |
| Overall verdict logic | PASS |
| Callback security | PASS |
| Duplicate protection | PASS |
| Gemini quota protection | PASS |
| Sports-provider isolation | PASS |
| Paid API usage in tests | ZERO |
| Deployment decision | CONDITIONAL GO after configuration and manual acceptance checks |

| File | Phase 2C.3 change |
| --- | --- |
| `predbot/bot/callbacks.py` | Additional Ticket Audit private menu URL/parameter; existing view payloads unchanged |
| `predbot/bot/commands.py` | Private entry/intake/callback routing, feature-mode clearing, recovery isolation and private help |
| `predbot/bot/telegram.py` | Sanitized bounded in-memory PNG/JPEG download only |
| `predbot/bot/ticket_audit.py` | NEW: ephemeral private controller, clarification, confirmation and bounded background execution |
| `predbot/bot/audit_format.py` | NEW: escaped review/results with bounded grouping and source labels |
| `predbot/ticket_audit/runtime.py` | NEW: lazy selected-model-only backend with shared service/cache and dedicated ledgers |
| `predbot/ticket_audit/composition.py` | NEW: application-owned overall/weakest rules and legitimate-local probability gate |
| `predbot/ticket_audit/schemas.py` | Distinguishable local >20-leg exception; wire bound remains omitted |
| `predbot/ticket_audit/gemini.py` | Extraction describes supplied tickets, retaining visible-only constraints |
| `predbot/ticket_audit/analysis.py` | Exact supplied-evidence factual instruction; existing bounded analysis retained |
| `tests/test_ticket_audit_telegram.py` | NEW: 102 private-flow/firewall regressions |
| `tests/test_community_menu.py` | New menu row; Custom Picks identity assertion keeps its original row |
| `tests/test_ticket_audit_free_research.py` | Existing prompt assertion aligned with the requested instruction |
| `.env.example` | Comments for the integrated private feature; safe disabled values retained |
| `README.md` | Private flow, production settings and manual checks |
| `TICKET_AUDIT_FOUNDATION_REPORT.md` | Latest decision and this implementation report |

Earlier phase files/uncommitted artifacts were preserved. No `main.py`, prediction providers, generation/cache jobs, scoring, settlement, stats or Custom Picks engine changes in Phase 2C.3.

### Limitations, manual Telegram plan and deployment decision

**CONDITIONAL GO**, subject to explicit free-tier configuration, writable persistent ledgers and these manual acceptance checks. No production bot was started or contacted during implementation. Prior synthetic-image validation is not broad real-betslip OCR certification; a human review is mandatory. Public research remains **PARTIAL**: exact supported EPL/OpenFootball coordinates only, lower-trust C-quality evidence, usually Insufficient Evidence; other sports may work with exact sufficient local data, otherwise insufficient. Research cannot establish current injuries/lineups or guarantee freshness/exact public kickoff. Missing dates/period/stat/entity require explicit clarification. Strict exact matching deliberately rejects uncertain aliases/markets. Sessions do not survive restart. No booking codes, converter, payments, permanent ticket tracking or newly generated alternatives.

Manual acceptance plan, without placing bets:

1. Privately send `/start ticket_audit`; check the intro and no subscription side effect.
2. Paste a 3-leg test ticket with fixture/sport/competition/date/kickoff/market/period/counts/entity; also try omitting a field and answer the labelled clarification.
3. Review all extracted picks and submitted odds; verify absent odds/combined odds stay absent.
4. Tap Audit Ticket twice; expect one audit and one progress message.
5. Inspect the result, source labels, checked count, weakest assessed leg and overall label; missing evidence must be explicit.
6. Re-enter Audit and send a clear PNG/JPEG screenshot; review extraction before confirming.
7. Try Send Again, then tap the old confirmation; it must expire. Try Cancel, including while work is running; stale results must not appear.
8. Send a booking-code-only string; expect unsupported-code guidance without a lookup.
9. Try a fully specified unsupported sport/fixture absent from today's local data; expect Insufficient Evidence, without a fabricated percentage.
10. As admin, run `/menu`, identify its returned message ID, test the community Ticket Audit URL, then manually replace/pin the menu. Group tickets must not be processed.
11. Open Custom Picks and use a normal request/recovery button; switch both directions and verify stale feature buttons are rejected appropriately.
12. Use private `/top`, `/daily`, `/stats` and the five community view buttons; verify existing behavior and scheduled community cards continue unchanged.

## 30. Screenshot extraction failure investigation — 2026-10-08

### Root cause and evidence

The user clarified that acceptance testing used a local terminal, not JustRun, and that the `.env` values were used unchanged with no terminal environment overrides. The project `.env` resolves to:

| Check | Observed |
| --- | --- |
| Gemini key | Present; value never printed |
| Model | `gemini-3.5-flash-lite` |
| `AUDIT_GEMINI_ENABLED` | `false` |
| `AUDIT_GEMINI_FREE_TIER_CONFIRMED` | `false` |
| Daily request limit | 20 |
| Runtime usage sidecar | Absent; zero recorded requests, not exhausted |

**Exact cause: `build_backend()` raises `AuditUnavailable('private_audit_disabled')` because both required opt-in flags are false.** It runs before Telegram image download, Gemini image validation, quota reservation and HTTP. The controller previously logged only the class name, hiding this fixed reason, and retained the generic unavailable Telegram response.

Reproduction first ran against the unchanged local configuration with all HTTP forbidden, before implementation changes. It returned `private_audit_disabled`, with no transport call. The new regression initially failed on the absence of diagnostic metadata while reproducing the exact original class-only log. A second read-only reproduction used the same `load_config()` path as `main.py`, then the instrumented backend; it confirmed `api_key_in_environment=true`, `api_key_present=true`, both flags false, `stage=backend_config`, `quota_used=0`, `quota_limit=20`, `quota_state=ledger_missing`, `http_attempted=false`, `quota_reserved=false`, `http_status=null`.

The parent diagnostic shell itself did not export the audit variables. This does **not** mean the bot cannot see its key: `main.py` calls `load_config()`, which copies missing variables from `.env` into its Python process environment; actual environment variables take precedence. The startup-path reproduction verified that distinction without printing credentials.

For the confirmed unchanged-file/no-overrides run, **no Gemini HTTP request was made and no daily request was consumed**. Screenshot intake reaching `_extract()` does not imply download or Gemini happened: backend initialization is its first step. No Gemini 400/403/404/429/timeout or image parsing error is needed to explain this failure. No screenshot-extraction implementation defect was reproduced. The class-only historical log alone would not establish any of these conclusions without the configuration inspection and the user's terminal clarification. No matching historical failure entry was found in the available local log files; no remote worker was inspected.

### Complete `AuditUnavailable` trace

`CommandLoop` private intake → `TicketAuditController._extract()` → lazy `build_backend()` → `Settings.from_environment()` / guards → Telegram in-memory image download → `GeminiClient.extract(image=(mime, bytes))` → `generate()` validation → `_request()` quota reservation and HTTP → response envelope → `ticket_from_json()` → controller revalidation and review. Evidence research/analyzer/provider calls are not part of screenshot extraction.

| Boundary | Fixed reasons / conditions | HTTP and quota |
| --- | --- | --- |
| Backend configuration | `unsupported_private_audit_model` if model is not the selected 3.5 Flash-Lite; `private_audit_disabled` if disabled, key absent or free-tier attestation false | Neither attempted nor reserved |
| Backend ledger construction | `usage_path_must_be_separate` for prediction-path reuse or unrelated Google-ledger tables; `unrelated_source_usage_database` for unrelated source-ledger tables | Neither attempted nor reserved; ordinary constructor SQLite/I/O errors retain their own exception class |
| Client configuration gate | `gemini_disabled`, `missing_key`, `free_tier_unconfirmed`, `unsupported_free_model` | Neither attempted nor reserved |
| Client capability/input/schema validation | `structured_free_capability_unavailable`, `image_free_capability_unavailable`, `input_too_large`, `unsupported_structured_schema`, `invalid_image_input`, `invalid_output_budget` | Neither attempted nor reserved |
| Search combinations | `SEARCH_GROUNDING_PAID_ONLY`, `search_schema_combination_not_validated` | Neither attempted nor reserved; unreachable in this extraction flow, which never enables grounding |
| Durable reservation | `invalid_local_budget` for <=0 or >100 configured requests; `local_quota_exhausted` at configured daily cap; `usage_guard_unavailable` for SQLite errors | Before transport; unsuccessful reservation does not increment the request count |
| Non-200 Google response | `GeminiHTTPError`, a subclass: 400/other rejection → `gemini_request_rejected`; 401/403 → `key_or_capability_rejected`; 404 → `model_unavailable`; 429 → `gemini_rate_limited`; >=500 → `gemini_service_unavailable` | Transport attempted; one request reserved, including failures |
| Transport/JSON decoding | `gemini_timeout`, `gemini_transport_or_json_failure`; non-object top-level JSON → `malformed_response` | One request reserved; HTTP status known only if a response was received |
| Successful HTTP but invalid envelope/content | Candidate missing/empty, malformed required structures or caught KeyError/IndexError/TypeError → `malformed_response`; block/non-STOP completion → `safety_refusal_or_incomplete_output`; no text → `empty_response` | One request reserved, HTTP 200 |
| Ticket validation | JSON/schema/field/type/non-finite numeric/invalid aware kickoff/root odds/warnings failures → `malformed_structured_output`; >20 legs → `TicketTooLarge` subclass with the same fixed reason; wrong extraction input type → `extraction_type_mismatch` | Normally after HTTP 200, one request reserved; controller revalidation itself adds no reservation |
| Failure-counter update | `usage_guard_unavailable` can mask an earlier HTTP/parsing error if updating counters fails | Existing reservation remains; diagnostics retain HTTP status/provider cause where observed |

Some generic `generate()` guards cannot fail on the normal owned screenshot call unless configuration/policy or inputs change: the selected model has reviewed structured/image support, output budget is fixed at 4096, and the fixed image prompt is below the input cap. Catalog/model-discovery errors are not reachable: runtime never calls `models()`. Telegram download failures use `TelegramImageError`, not `AuditUnavailable`, and are logged at `telegram_image_download`.

Existing response behavior is intentionally preserved. In particular, a blocked response with no `candidates` currently becomes `malformed_response` because candidate access occurs before the block check; it now records `prompt_blocked=true`, rather than hiding that fact. Other unexpected exception types are not relabelled as `AuditUnavailable`. Malformed ticket/output and explicit refusal cases keep their existing clearer-ticket copy; configuration/quota/transport/HTTP failures keep temporary-unavailability copy.

### Diagnostic implementation and safety

Failures now include a single JSON `diagnostics` object alongside `error_type`, with: `stage`, fixed `reason`, reviewed `model`, `api_key_present`, `api_key_in_environment`, enabled/attestation flags, `mime_type`, `image_size_bytes`, Pacific `quota_day`, `quota_used`, `quota_limit`, `quota_state`, `quota_reserved`, `http_attempted`, `http_status`, and provider category/fixed safe message when applicable. Response diagnostics additionally expose only a boolean `prompt_blocked` and an allowlisted `finish_reason`.

Stage names distinguish backend config/usage/source guard, Telegram image download, client gate/validation, quota reservation, transport, response JSON/envelope, ticket parsing/revalidation and review. Early failures correctly show unknown MIME/size/status rather than pretending an image reached Gemini. `http_attempted=true` means the HTTP transport was invoked; a timeout or connection error does not prove Google received the request. `quota_reserved=true` tracks this operation's successful durable reservation; `quota_used` is a contemporaneous shared-ledger snapshot and may include other concurrent requests.

Diagnostics use a `ContextVar` per extraction, so the existing shared two-worker client does not mix metadata across images/users. Nested client/runtime failures preserve stage metadata on the exception for direct local inspection; the controller logs the same safe metadata. New sessions reset attempt/status/reservation fields. Quota snapshots open SQLite with `mode=ro`, `query_only`, a short timeout and no writes, creates or resets; diagnostic read failures report unknown quota without replacing the original failure or altering enforcement.

Reason codes and model names are allowlisted. No arbitrary exception text, Google response message, headers, URLs, paths, user/chat/file IDs, filename, original ticket, extracted ticket, image bytes/base64 or key value enters the new logs. Provider categories map to fixed summaries because even credential-redacted provider messages might echo a full submitted ticket or image. An unknown model/code is labelled unreviewed/unknown without printing its value. Nothing from this diagnostic object is sent to Telegram users.

### Retest configuration

Change only these two existing values manually in the project `.env`:

```dotenv
AUDIT_GEMINI_ENABLED=true
AUDIT_GEMINI_FREE_TIER_CONFIRMED=true
```

The existing key, `GEMINI_AUDIT_MODEL=gemini-3.5-flash-lite` and `AUDIT_GEMINI_MAX_REQUESTS_PER_DAY=20` already resolve correctly and do not need replacement. The free-tier confirmation remains based on the user's earlier explicit free-tier/billing-disabled confirmation. Keep Cloud Billing disabled. Stop and restart the terminal bot after changes: startup copies `.env` into process environment and the backend caches its settings. No JustRun change is needed for this terminal test; future JustRun deployment needs the same existing settings in that worker's environment. No `.env`, hosting variable, quota ledger or billing setting was edited automatically.

Then privately re-enter `/start ticket_audit` and submit the screenshot again. If a different failure appears, the new safe diagnostic line will identify its stage, reason, reservation and transport status. The fix is opt-in configuration, not a fallback or quota bypass.

### Regression tests and changed files

41 new cases in `tests/test_ticket_audit_diagnostics.py`: exact disabled-config screenshot reproduction; missing key/each flag/wrong model; real startup key visibility and environment precedence; exhausted 20-request guard without a 21st call; invalid limits; correct PNG/JPEG MIME/base64 across 400/403/404/429/500; timeout/connection failure; HTTP-200 JSON/envelope/refusal/truncation/empty/ticket/type errors; local image and 20-leg enforcement; reservation/counter failures; unknown exception/provider secret suppression; concurrent metadata isolation; clean context reset; read-only missing/corrupt ledger behavior and unchanged prediction database. Inherited HTTP/socket/provider/generation/settlement/live firewalls remain enabled. No external calls or paid services were used.

Focused verification: **434 passed in 42.69 seconds**, covering new diagnostics, Telegram Audit, foundation, free research and model-access suites.

**Final complete suite: 2,226 passed in 143.59 seconds (2:23), zero failures.** Previous 2,185 cases plus 41 new diagnostics regressions. `git diff --check` passed. No real Gemini/Telegram/public-source or paid API requests were made during this investigation or pytest.

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/audit-diag-full-20261008a
```

| File | This investigation's change |
| --- | --- |
| `predbot/bot/ticket_audit.py` | Safe failure logging; stage and image metadata only |
| `predbot/ticket_audit/runtime.py` | Observe resolved config/backend guards without changing gating |
| `predbot/ticket_audit/gemini.py` | Observe validation/reservation/transport/response/parsing boundaries; retain existing request/counter behavior |
| `predbot/ticket_audit/diagnostics.py` | NEW: request-local allowlisted diagnostics and read-only quota observation |
| `tests/test_ticket_audit_diagnostics.py` | NEW: 41 reproductions/privacy/isolation regressions |
| `TICKET_AUDIT_FOUNDATION_REPORT.md` | This root-cause trace and retest instructions |

No generation, Top/Daily selection, scoring, provider logic, settlement, scheduling, caches, sports-provider firewall, Custom Picks, community menu or existing Telegram response behavior changed. No new provider calls or tracking/stats/settlement side effects; no Search grounding, billing enablement, alternative model, quota reset, wire `maxItems=20` or paid fallback was introduced. Earlier uncommitted phase work was preserved.

