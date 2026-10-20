# Kovqi visual redesign — 20 October 2026

The redesign was approved for release on 20 October 2026. Production generation
loads this shared renderer and its bundled assets from `main` for new snapshots.
No schema migration is required. Existing published tickets are not replaced.
No Telegram messages were sent during release validation.

## Final images

- Today's existing ticket was previewed locally; its private invitation is not
  included in repository preview artifacts.
- [One selection, labeled sample](predictions-bot/assets/kovqi/previews/sample-1-selection.jpg)
- [Two selections, labeled sample](predictions-bot/assets/kovqi/previews/sample-2-selection.jpg)
- [Three selections, labeled sample](predictions-bot/assets/kovqi/previews/sample-3-selection.jpg)
- [Long names and markets, labeled sample](predictions-bot/assets/kovqi/previews/sample-long-names.jpg)
- [Other clubs on a future date, three-selection sample](predictions-bot/assets/kovqi/previews/future-ticket-3.jpg)

All images are 1080 pixels wide with content-sized height up to 1350 pixels,
removing excess space below shorter tickets. Each was visually inspected against the approved
reference. The current-ticket preview uses the existing 20 October D1 snapshot,
read without writes: Sabah FK vs Borussia Dortmund and Fenerbahce vs SK Slavia
Praha. Its underlying odds remain 1.3 and 1.5594; the authoritative calculation
formats the total as 2.03. Names and labels are preserved exactly as stored.

## Files changed

- `predictions-bot/predbot/bot/ticket_image.py`: replaces the flat card with a
  stadium poster, alpha-matted approved logo, crown with white/lime heading,
  handwritten slogan, verified team badges and calendar icon, cohesive ticket
  with subtle dividers, market/selection
  panels, consistently aligned odds, integrated total strip, Telegram CTA and
  visible disclaimer. Content wraps at measured font widths rather than being
  clipped. One selection uses a larger fixture treatment; two and three use
  progressively tighter spacing. All preserve the fixed canvas.
- `predictions-bot/predbot/bot/club_badges.py`: reusable local catalog loader,
  with explicit alias lookup, ambiguity checks and asset-path bounds.
- `predictions-bot/assets/kovqi/badges/`: 396 bundled clubs, per-file source hashes,
  catalog index, reviewed aliases and the four individually checked overrides.
  No four-team list remains in the renderer. Unknown/missing badges keep the same
  team-name/versus/market/odds grid; they no longer change the fixture layout.
- `predictions-bot/tools/build_club_catalog.py`: explicit maintenance-only tool
  for rebuilding the offline bundle from a pinned source tree; no network calls
  or catalog fetches are added to daily generation.
- `Anton-Regular.ttf` and `OFL-Anton.txt`: heavier reference heading. Fixture
  rows and market panels are compact, avoiding the previous stretched cards.
- `predictions-bot/assets/kovqi/KaushanScript-Regular.ttf` and `OFL-Kaushan.txt`:
  the reference's handwritten slogan, underlines and Good luck sign-off. The
  reference's optional stake field and coin icon are included without inventing
  a stake amount; the QR area also includes its scan instructions and divider.
- `predictions-bot/assets/kovqi/Barlow-SemiBold.ttf`: fixture/body emphasis using
  the existing Barlow family and bundled SIL license. The two existing fonts
  remain in use.
- `predictions-bot/assets/kovqi/stadium-background.png`: static decorative
  background with floodlights, dark stadium, turf and a cropped lower-right ball.
- `predictions-bot/assets/kovqi/stadium-background-prompt.txt`: exact background
  creation prompt and tool provenance.
- `predictions-bot/assets/kovqi/README.md`: updated asset provenance and usage.
- `predictions-bot/assets/kovqi/previews/` and `sample-preview.jpg`: reviewed
  output images; none are loaded by production publishing.
- `predictions-bot/tools/preview_bet_image.py`: creates all four sample layouts
  and optionally reads an existing structured ticket via `--snapshot`. Uses the
  Lagos date and configured link rather than fixed production fixtures or dates.
- `predictions-bot/tests/test_ticket_image.py`: fixed-size canvas assertions,
  alpha transparency, missing/corrupt assets, Unicode, optional competition,
  three long fixtures, disclaimer placement and existing transport/fallback checks.

## Rendering and assets

Pillow remains sufficient; no browser or Playwright installation is needed.
The existing logo's original pixels are alpha-matted deterministically. Its
letterforms and proportions are preserved; no logo was generated or redesigned.

The built-in imagegen tool created only the decorative stadium backdrop once
during implementation. Its full prompt is in the bundled prompt file above.
Daily rendering loads that local asset; it makes no AI calls, network image
requests or paid API requests. A missing/corrupt background uses a local dark
gradient. Missing required fonts/logo still use the existing text-ticket failure
fallback. No new production Python dependencies were added.

The final JPEG uses bounded progressive compression to stay within the existing
120,000-byte / 160,000-base64-character media ceiling. Neither the Worker limit
nor the D1 transfer path was changed. QR codes use integer-sized modules and a
four-module quiet zone, without interpolation. Every final preview QR was decoded
locally from the compressed JPEG and matched the configured community URL.
OpenCV was installed only in ignored local test dependencies for this manual
verification; it is not a production dependency.

## Preserved behavior

The renderer's public inputs and byte output are unchanged. It uses actual
structured selections and the existing `combined_odds` function. Markets,
outcomes, prices, IDs and kickoff times remain dynamic. The configured link
drives both the visible invitation and QR code; no placeholder address is shown.

No prediction engine, selection thresholds, Daily Picks formatting, schedules,
Cloudflare Worker code, database schema, publication receipts, match monitoring,
settlement, owner routing, admin permissions or Won/Lost GIF behavior was edited.
The existing snapshot → D1 → Worker → Telegram architecture is retained.
Production Python changes are restricted to the renderer and its local badge
loader. The existing `_store` → `attach_image` hook automatically uses this
shared template for future snapshots; preview scripts are not part of publishing.

The catalog covers 396 European clubs, not every team worldwide. Explicit
aliases and a safe missing-crest presentation remain necessary. New catalog
assets can be added through the maintenance tool without editing the renderer.

## Verification

The latest reference comparison also restores a separate outlined summary card
inside the ticket, places the QR code before its scan instructions, and centers
the shield/disclaimer between balanced rules. These changes are in the shared
renderer and apply to future generated snapshots after deployment.
Reference spacing now also controls the smaller logo, header separation,
150-pixel minimum match rows, footer gap and content-sized bottom edge.
The latest spacing revision passed all 28 renderer tests and QR decoding for
all eight regenerated previews.

- 176 Python checks passed across rendering, badge fallback, text tickets,
  publication, lifecycle, monitoring, settlement, dependency guard and workflows.
- All 408 existing Worker checks passed.
- All eight final JPEG QR codes decoded to the configured community link.
- Future snapshots dated 2030-06-01 and 2031-11-20 used the real production
  `_store` path, retaining their date, pick identity and odds, with network
  access prohibited. Both produced the same catalog-backed image presentation.
- The extra three-long-fixture stress test passes within the existing media cap.
- Wrangler 4.151.1 production packaging dry-run passed; nothing was deployed.
- `git diff --check` passed.
- Final release gate: 45 renderer, dependency-guard and workflow checks passed.
- All five requested preview variants were visually inspected. No unsupported
  team crests or guessed club imagery was introduced. The user-requested club
  marks are sourced and verified, rather than generated. Missing badge or script
  assets are optional and do not suppress the underlying ticket.

The existing remote schema was only read to obtain today's preview selections
and configured URL. The existing record, publication and settlement state were
not changed. Previously deleted local reports and unrelated workspace changes
were left untouched.
