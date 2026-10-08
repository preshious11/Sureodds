# Betloy Ticket Converter — Phase 2B.1 validation

Report finalized: **2026-10-07 14:07:20 UTC / 15:07:20 Africa/Lagos**.
Validation date: **2026-10-07**, Africa/Lagos (UTC+01:00). Documentation review and
the HTTP observation occurred during this session; the individual health-call
timestamp was not captured. The diagnostic records UTC timestamps for future
explicit probes. Full-suite result is recorded below after completion.

**Recommendation: NO-GO for Phase 2B.2 at present.** The documented API health
route returned HTTP 404/HTML, and the official pages give conflicting contracts
and access/billing information. This is a blocked validation, not proof that
Betloy's conversion engine or this account's key is invalid. Follow the user's
stop conditions: no additional live requests, guessed routes, alternate-host
experiments, paid source guessing, purchases, scraping or bookmaker internals.

## 1. Official sources and contract

Consulted current official sources:

- [Developer API reference](https://betloy.com/api)
- [Homepage API example / source-detection description](https://betloy.com/)
- [Resources and API links](https://betloy.com/resources/)
- [Pricing and API eligibility](https://betloy.com/pricing)

The reference identifies **v1**, base `https://betloy.com/api/v1/`, and
`Authorization: Bearer <BETLOY_API_KEY>`. Its endpoint list is:

| Method | Relative route | Contract details obtained |
| --- | --- | --- |
| GET | `/health` | Liveness; documented without authentication requirement |
| GET | `/usage` | Account usage; JSON field schema unspecified |
| GET | `/bookmakers` | Catalogue; concrete JSON field schema unspecified |
| POST | `/decode` | Ticket inspection; request and selection schemas unspecified |
| POST | `/convert` | Example body: `booking_code`, `from_bookie_id`, `to_bookie_id` |
| POST | `/scan` | Odds comparison; not a documented code-only source detector |
| POST | `/analyze`, `/trim` | Outside this validation's operations |

The published conversion example contains `success`, `data`, `message`; inside
`data`: `conversion_id`, `original_code`, `converted_code`, `from_bookie`,
`to_bookie`, `total_odds`, `number_of_events`, `status`. This is documentation,
not an observed real response. No complete OpenAPI link or decode schema was
obtained from the consulted official pages. No undocumented route was requested.

## 2. Actual requests and stop decision

`BETLOY_API_KEY` was present in local environment/.env configuration. Its value
was never printed, written into code/tests/report, committed or sent in a URL.

| Attempt | Exact target / method | Actual outcome |
| --- | --- | --- |
| Initial restricted-network attempt | GET `https://betloy.com/api/v1/health` | Local `ProxyError`; no HTTP response obtained |
| Authorized network attempt | GET `https://betloy.com/api/v1/health` | **HTTP 404**, `Content-Type: text/html`, nginx not-found page, no redirect |

The authenticated HTTP request used the reference's Bearer header, HTTPS,
timeouts `(10, 20)` seconds, and disabled redirects. The health response exposed
no API JSON, account data, provider error code, plan or quota. A missing route
does not establish whether the key is accepted. **Authentication: unverified.**

Live validation stopped here. Usage, catalogue, sandbox, decode, conversion,
failure simulation and destination-code loading were **not run**. No fallback
request to the conflicting homepage host was made.

## 3. Account, usage, costs and rate limits

Actual plan, remaining/monthly/daily allowance, reset, account rate limits,
request-versus-operation accounting and failure/cache/decode/detection costs:
**UNKNOWN**. No usage-before/after responses were obtained.

Confirmed business operations executed: **zero decode and zero conversion**.
Total billed credits: **UNKNOWN**; do not label the health request free from
documentation alone. There was one confirmed HTTP response, and the failed
local proxy attempt has no confirmed provider response.

[Pricing](https://betloy.com/pricing) excludes API access on Free/Individual,
includes it on Business, and describes per-request API charging. This conflicts
with the [API reference](https://betloy.com/api), which describes free-tier keys
and free metadata lookups. The reference's paid-plan rate table and FAQ also
disagree. The actual account response must settle these questions; purchasing
an upgrade is not authorized or necessary for this report.

## 4. Sandbox and source detection

Sandbox status: **UNAVAILABLE FOR VALIDATION / NOT RUN**. The reference specifies
`?test=true`, claims deterministic free responses, but no sandbox request was
sent after the health stop. Key requirements, credit effects, mock catalogue
realism, decode/conversion support and error simulation are unverified. The
utility's optional sandbox command only probes health with the documented query;
it explicitly does not claim to prove a conversion sandbox.

Automatic source detection: **UNCLEAR — classification D**. The
[homepage](https://betloy.com/) describes automatic source recognition in the
website flow. Its example uses another hostname/body shape; the reference's
conversion example requires an explicit source ID. Neither supplies a verified
code-only detection request/response contract. Decode/scan parameters are absent,
so they cannot establish that detection is either supported or impossible. No
live code-only request, paid brute-force search or dedicated guessed detector
was attempted. The future source-selection fallback remains accepted.

## 5. Catalogue, Nigerian coverage and registry feasibility

Actual supported-bookmaker count and Nigerian count: **UNKNOWN**, not zero.
There is no API catalogue response, and a public marketing list cannot prove
account-specific availability, IDs, direction support or active status.

| Requested bookmaker | Live catalogue evidence | Provider ID / capabilities |
| --- | --- | --- |
| SportyBet | Not obtained | Unknown |
| Bet9ja | Not obtained | Unknown |
| MSport | Not obtained | Unknown |
| 1xBet | Not obtained | Unknown |
| BetKing | Not obtained | Unknown |
| NairaBet | Not obtained | Unknown |
| Betano | Not obtained | Unknown |
| 22Bet | Not obtained | Unknown |
| Paripesa | Not obtained | Unknown |

Additional Nigerian bookmakers were not discoverable in this run. No example
bookmaker IDs were treated as real catalogue IDs.

| Future registry field | Feasibility / required evidence |
| --- | --- |
| `provider_id` | Need actual catalogue key and confirmed mapping to conversion IDs |
| `canonical_name` | Need an actual API field and canonical naming guarantee |
| `display_name`, `aliases` | May be maintained locally after canonical IDs are established |
| `country` | Only explicit, documented region/country data; global implies no Nigerian guarantee |
| `can_decode`, `can_convert_from`, `can_convert_to` | Need provider capabilities or controlled proof; never default to true |
| `supported_markets` | Need actual catalogue/pair/sport compatibility fields |
| `active` | Need explicit status or verified availability; not inferred from a listed name |

Popular buttons, More and typed names are architecturally feasible, but the
catalogue's suitability remains unverified. Name aliases can be local, pure
normalization; ambiguous countries/brands must require clarification. No registry
or Telegram UI was implemented.

## 6. Decode and NormalizedTicket feasibility

Real decode: **NOT RUN**. No real booking code or selections were submitted.
Real decoded-ticket fields: **none observed**. The parser prototype preserves
opaque structured data and fabricates no selection schema.

NormalizedTicket feasibility: **PARTIAL at the documentation/design level;
unverified for real tickets**. Documented conversion summary fields cannot
establish full source-ticket semantics or populate selection detail.

| Normalized field | Actual provider availability | Safe future source, conditional on evidence |
| --- | --- | --- |
| `source_bookmaker` | Unverified | Verified detector/decode field or explicit user choice; conversion example has `from_bookie` |
| `source_code` | Unverified | Runtime user input; retain only as needed, not diagnostic persistence |
| `country` | Unverified | Confirmed source bookmaker region |
| `combined_odds` | Unverified | Explicit source-ticket odds; do not treat conversion `total_odds` as source odds without confirmation |
| `selections[]` | Unverified | Actual decode or conversion selection array with published semantics |
| `sport` | Unverified | Explicit selection field; not inferred from team names |
| `competition` | Unverified | Explicit field |
| `home`, `away` | Unverified | Explicit participants; no unreliable split of display text |
| `kickoff` | Unverified | Explicit timestamp plus known timezone/format |
| `market`, `selection`, `line` | Unverified | Explicit stable identifiers/specifiers; never substitute guessed outcomes |
| `source_odds` | Unverified | Explicit source decimal odds |
| `source_event_id` | Unverified | Source bookmaker event ID |
| `canonical_event_id` | Unverified | Actual shared identifier; a provider/source ID must not masquerade as canonical |

No field is labelled actually available solely because a synthetic parser test
can read it. Any derivation, including accumulator multiplication, needs verified
ticket type, odds convention and complete leg data first.

## 7. Conversion, destination verification and partial matching

Real conversion: **NOT RUN**. No destination code, share URL, actual source leg
count, converted leg count, odds, warnings or usage delta was obtained.
Destination-code verification: **NOT RUN**. The future proof must show that a
returned code retrieves the destination slip, through an officially supported
mechanism or manual bookmaker-site loading, without placing a wager.

Partial-conversion visibility: **UNKNOWN**. The reference describes a `flagged`
array and no silent substitution, but its JSON conversion example omits flagged
data. Its presence, item schema, dropped/substituted-leg semantics, confidence,
and source-to-destination leg reconciliation were not observed. The diagnostic
parser keeps absent flags as `None`/UNKNOWN instead of falsely treating absence
as an empty list or a fully matched ticket. This is not proof of safe conversion.

Market/pair compatibility is **UNKNOWN** for 1X2, Moneyline, Double Chance, Draw
No Bet, BTTS, Totals, handicap/spread, Correct Score, corners, cards and player
props. No bookmaker/source→destination/sport/market coverage matrix was obtained.
Unsupported-market and no-matching-fixture behavior remain unverified. Phase 2B.2
must not silently accept partial or substituted results without reliable evidence.

## 8. Failure taxonomy

Only the health 404/HTML and local proxy failure are real observations here.
Mock error identifiers in tests are explicitly synthetic, not Betloy codes.

| Situation | Observed or documented status | Provider code/message and billing |
| --- | --- | --- |
| Documented health route missing | Observed 404, HTML nginx page | No API error code; billing unknown |
| Local proxy failure | No HTTP response | No provider code/message or confirmed billing |
| Invalid / expired / unknown booking code | Not tested; exact status unknown | Unknown |
| Unsupported source / destination | Not tested; reference gives generic 400 for unsupported bookmaker | Unknown |
| Same source/destination | Not tested | Unknown |
| Unsupported market / partial match / no matching fixtures | Not tested | Unknown |
| Provider unavailable | Not tested | Unknown |
| Authentication failure | Reference lists 401; not observed | Unknown |
| Exhausted quota | Reference lists 402; not observed | Unknown |
| Rate limiting | Reference lists 429; not observed | Retry-After claim not verified; billing unknown |
| Malformed request | Reference lists 400; not observed | Unknown |
| Timeout | Not observed at provider | No idempotency/charging guarantee obtained |
| Server error | Reference lists 500; not observed | Unknown |

No credits were intentionally spent to trigger failures, exhaust quota or test
rate limiting. Actual request rate limits and failed-request credit effects
cannot be assigned from these mocks.

## 9. Retry and cache recommendations

These are proposed local policies, not provider guarantees:

- **Paid decode/conversion:** no automatic retries. A timeout/5xx may follow a
  successful, chargeable operation. Require documented idempotency/reconciliation
  before retrying. Never retry by enumerating source bookmakers.
- **Metadata:** only consider bounded retries after billing is resolved and the
  route works. Respect a validated Retry-After; stop on authentication, access,
  quota, schema or route failures. Current diagnostic performs no retries.
- **Catalogue:** propose one hour, keyed by provider/version/country, with explicit
  last-fetch time and invalidation on schema/capability errors.
- **Source detection:** propose five minutes for a successful authoritative match;
  no negative/ambiguous-result caching initially.
- **Decoded ticket:** propose one minute, also bounded by earliest kickoff/code
  expiry, with a fetched timestamp and archived-odds label.
- **Conversion:** propose one minute with exact source/code/destination/options
  key; never present cached odds as current. Confirm booking-code lifetime first.
- **Usage:** use a fresh read before/after an approved paid operation; any display
  cache should last at most five seconds. Concurrent operations require budget
  accounting/idempotency to be specified before implementation.

## 10. Security and isolated diagnostic

Added `diagnose_betloy.py`, following existing root diagnostic conventions. It
is not imported by `main.py`, `cli.py` or production modules. It has no database,
subscriber, Telegram, scheduling, prediction or provider-generation integration.

Safety properties:

- Reads only `BETLOY_API_KEY` from process environment or the existing .env parser;
  loads no unrelated .env settings into the process and writes nothing.
- Fixed HTTPS Betloy origin and metadata endpoint allowlist; no credential URL
  parameters, alternate base URL option, redirects, HTTP fallback or retries.
- No mode prints help and makes no request. Missing key stops all network access.
- Because pricing conflicts with metadata/test-free claims, **all CLI network
  probes require `--live`**. It means explicit possible-credit intent, not a claim
  that the operation is known free. Do not rerun live probes until the blocker is
  resolved. Decode/conversion remain blocked even with `--live`.
- An optional sandbox metadata probe uses only documented `test=true`, without
  treating a healthy response as proof of deterministic/free sandbox conversion.
- Body, header, encoded and nested active-key reflections are redacted. Transport
  exceptions are never printed. Source-code/credential/email fields are redacted;
  public catalogue keys remain inspectable when explicitly accompanied by region.
- Real source codes are never embedded, stored or submitted in this run. Tests
  generate ephemeral synthetic credential/code strings and mock every HTTP call.
- Parser prototypes preserve fields and UNKNOWN states. Unverified usage,
  catalogue and decode shapes are clearly labelled synthetic/provisional.

Examples for local, network-free inspection:

```powershell
python diagnose_betloy.py
python diagnose_betloy.py health
python diagnose_betloy.py convert --live
```

The second command refuses network access without `--live`; the third reports the
validation blocker and never converts. After vendor clarification, metadata
commands with `--live` can be deliberately revisited. Enabling real POSTs requires
finishing this validation, not removing guards blindly.

Future logs should contain operation name, status, duration, safe request ID,
quota delta and counts of unmatched legs. Do not log keys, auth headers, complete
request/response bodies, user account details, or booking codes by default.
No successful real response was available to assess unexpected personal fields.

## 11. Documentation inconsistencies and missing evidence

1. The reference's documented health URL returned 404/HTML instead of API health.
2. The [homepage](https://betloy.com/) / [resources](https://betloy.com/resources/)
   examples use `api.betloy.com/v1` and `code/from/to`; the reference uses
   `betloy.com/api/v1` and different numeric-ID parameters. Compatibility/aliases
   were not established. Neither contract was guessed or mixed.
3. Pricing excludes Free/Individual API access, while the reference describes
   free API keys/limits. Account access could not resolve this.
4. The reference's paid rates disagree internally: Individual 60 versus 120 per
   minute; Business 120 versus 1,200. Free monthly request and credit numbers are
   different units; **1,000 requests is not proof of 1,000 free conversions**.
5. Pricing's general API charging and the reference's free metadata / variable
   decode/scan charging claims are inconsistent. Actual costs remain unknown.
6. Homepage/resources claim included webhooks/specification; the reference says
   webhooks are planned. No downloadable specification link was obtained.
7. Flagged-leg and automatic-detection claims lack sufficient REST schemas. These
   are evidence gaps; they are not proof of substitutions or missing capability.

## 12. Recommended architecture and release decision

**NO-GO now:** API accessibility/authentication, quota, actual Nigerian coverage,
decoded selections, working conversion, retrievable destination codes and safe
unmatched-leg handling were not demonstrated. A code-only source-detection
fallback alone would permit a CONDITIONAL GO, but the present blockers are broader.

Before resuming, obtain from Betloy the account-specific working API base/routes,
access eligibility, usage JSON and cost rules, sandbox contract, catalogue IDs
and direction capabilities, decode/detection schemas, conversion leg reconciliation,
code verification and idempotency guarantees. Do not buy a plan automatically.
Then validate sandbox/quota/catalogue and one runtime-supplied real source→target
ticket with before/after usage and destination loading evidence.

If those checks earn GO/CONDITIONAL GO, Phase 2B.2 should add an isolated Betloy
adapter, dynamic capability-aware bookmaker registry with local aliases, and
provider-neutral NormalizedTicket/NormalizedSelection models. The separate private
Ticket Converter entry should accept code first, use detection only if verified,
then source fallback and popular/More/typed destination selection. Testing should
remain ungated by payment. Explicitly expose unmatched legs without substitution,
keep conversion outside prediction/result tracking, and leave Ticket Audit out.
**No Phase 2B.2 feature was implemented.**

## 13. Tests and unchanged production scope

Added **62 mocked tests** in `tests/test_betloy_validation.py`. Coverage includes
missing key, env precedence, secret/header safety, healthy/usage/catalogue
inspection, explicit Nigeria filtering, malformed/non-JSON responses, generic
decode inspection, documented conversion example and partial/absent flags,
invalid-code-like synthetic errors, auth/quota/rate limit/timeout/5xx, redaction,
fixed endpoint allowlist, no retries, CLI credit guards and production isolation.
No source-detection parser test pretends an unverified endpoint exists.

Full suite: **1,791 passed in 107.75 seconds (1:47)**. All 62 new mocked cases
passed; the existing dynamic root-diagnostic regression also adds one case for
the new script. Previous production coverage remains passing.

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/phase2b1-full1
```

Files added only: `diagnose_betloy.py`, `tests/test_betloy_validation.py`, this report.
No production code, .env, menu, Custom Picks, archive, providers, prediction
generation, All/Top/Daily, settlement/VOID, live tracking, stats/calibration,
recaps, subscriber delivery, scheduler or database schema was changed.
