# Kovqi Bet of the Day implementation

Implemented locally for review. No commit, push, migration, live Telegram post,
or deployment was performed.

## Files and responsibilities

- `predictions-bot/predbot/bot/ticket_image.py`: deterministic Pillow JPEG
  renderer, supplied-logo crop, 1–3 adaptive selection rows, authoritative
  `bot.format.combined_odds`, Lagos kickoff times, link validation, QR code,
  disclaimer, bounded optional media and safe rendering fallback.
- `predictions-bot/predbot/cache/models.py`: backward-compatible optional
  `bet_image` presentation field, kept separate from generation diagnostics.
- `predictions-bot/predbot/cache/fetcher.py`: attaches presentation when the
  already-selected structured snapshot is stored. No selection logic changed.
- `predictions-bot/assets/kovqi/`: supplied logo, Barlow fonts and their SIL
  licenses, labeled fictional preview and asset provenance.
- `predictions-bot/tools/preview_bet_image.py`: reproducible offline preview.
- `predictions-bot/requirements.txt`: free Pillow and qrcode dependencies.
- `workers/src/ticket_image.ts`: validates snapshot date, selected pick IDs,
  canonical fixture IDs, membership, markets, outcomes, labels, prices and
  kickoff times against D1 before accepting optional media.
- `workers/src/cron.ts`: multipart `sendPhoto`, photo receipts, group and owner
  copies, and independently deduplicated Won/Lost replies to each destination.
- Python `bot/{callbacks,format,pagination,telegram}.py` and Worker
  `{format,telegram,views}.ts`: public Bet of the Day copy and compatible ticket
  splitting. Existing `/top`, `top` memberships and receipt identifiers remain.
- Python and Worker `custom_picks` formatters: required text-ticket disclaimer.
- `.github/workflows/stage5-acceptance-production.yml`: passes the optional
  real invitation URL from a repository secret to generation.
- Tests and shared presentation golden fixtures: required branding/disclaimer
  changes, rendering, transfer and delivery regressions. The prediction slate
  golden fixture was not regenerated.
- `.gitignore`: local dependency and test scratch directories only.

## Rendering and transport

The existing production Actions generation job renders the image while storing
its completed snapshot in scratch SQLite. Image bytes are base64-encoded in that
snapshot, then retained in D1 by the existing `d1_sync.py` pipeline. The Worker
never accesses the Actions filesystem and no public image host is needed.

The existing exporter sends rows over 90KB through parameterized D1 REST requests
(`stage5-acceptance-production.yml` already runs `rest-apply`). Media is capped
at 160,000 base64 characters and the entire snapshot at 1.5MB, below the existing
2MB row limit. A regression test verifies that the REST parameter payload
preserves the media. This uses existing storage, schema, retention, generation
leases and readiness gates; no migration is necessary.

At the unchanged delivery time, the Worker validates optional media and uploads
the actual JPEG bytes directly to Telegram. The caption includes a clickable
community link when configured. Daily Picks remains `sendMessage`. Already
cached snapshots without media remain compatible and publish as text.

## Publication, monitoring and settlement

`top` and `top_owner` keep their existing independent atomic claims and receipts.
The returned photo message ID is written to the same `posts.message_id` used by
text publications. Picks, fixture keys, memberships and settlement rows remain
structured and unchanged. No settlement reads the image or caption.

Result GIF queries now include owner receipts as independent destinations,
resolving their memberships back to `top`/`daily`. Each destination replies to
its own original message ID with exactly `Won` or `Lost`. Existing pending,
half-result, push, void and match-end ordering rules remain in effect. Replies
require the original message to exist rather than silently becoming standalone.
Stats/Yesterday access checks and Top/Daily selection deduplication are unchanged.

## Failures and retries

Missing libraries/assets, unreadable fonts/logos, excessive text or media size
log a warning and omit the image. The Worker also falls back to the original
text card when media is absent or inconsistent with D1. Neither path regenerates
predictions or creates new tickets.

A rejected photo upload follows the existing retryable claim-release path.
An accepted photo must have a valid Telegram response/message ID. Ambiguous
upload responses and receipt-write exceptions propagate; they never trigger a
second text post. Group and owner receipts prevent resending a confirmed group
publication when only the owner needs retrying.

Remaining inherited limitation: Telegram sends and D1 receipt writes cannot be
one atomic transaction. A crash or lost response after Telegram acceptance but
before receipt confirmation can leave an ambiguous claim; existing stale-claim
recovery can resend it. The implementation preserves repository semantics and
does not claim an impossible cross-service exactly-once guarantee. No live
Telegram verification was performed without deployment approval.

## Configuration and preview

Set the GitHub Actions repository secret `TELEGRAM_GROUP_INVITE_URL` to the real
community URL (`https://t.me/<public_name>`, `https://t.me/+<invite>`, or legacy
`https://t.me/joinchat/<invite>`). For local generation use the same environment
variable. No additional Worker variable is required: the validated URL travels
with the image. Missing or malformed values warn and omit the promotion.
Validation checks URL syntax; it cannot establish that a private invitation has
not expired or been revoked. Maintain that link in configuration.

Install the updated Python requirements. Keep all bundled logo/font files in
`predictions-bot/assets/kovqi/`. They require no runtime downloads or AI calls.

Run `python predictions-bot/tools/preview_bet_image.py` to regenerate
`predictions-bot/assets/kovqi/sample-preview.jpg`. It uses clearly marked
fictional sample data and never posts or reads production data.

## Verification

- Full Python suite: **3,280 passed**, 322.42 seconds.
- Final renderer/asset/transport suite, including the additional corrupt-asset
  cases and long market-name layout: **16 passed**, 2.67 seconds.
- Full Worker suite: **408 passed**. This includes photo publication IDs,
  group/owner retries, stale media, upload rejection, receipt-write failure,
  multipart encoding, reply IDs, monitoring, settlement and permissions.
- Repository workflow hygiene tests passed in the Python suite.
- `npx --yes wrangler@4.151.1 deploy --dry-run --config workers/wrangler.toml`:
  **passed**, local bundle only. The package's `npm run check` could not find
  its uninstalled local Wrangler; the explicit repository-pinned version above
  completed the equivalent dry-run successfully.
- `git diff --check`: **passed**.
- Final fictional three-selection preview was regenerated and visually checked.

Test runs used workspace-local downloaded libraries and disposable test paths.
Production dependencies remain declared in `requirements.txt`. Existing user
deletions and `workers/backups/` were left untouched.
