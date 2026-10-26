# Stage 1 Staging Runbook

Run this yourself. Nothing here touches the live bot token, the live webhook,
or the production Worker. Everywhere a value is secret or identifying, this
runbook uses a placeholder (`<...>`) — fill those in locally, never paste the
real value into this file, into chat, or into a commit.

**Boundaries enforced by this runbook:**
- The bot token used throughout is the **test** bot's token, from a bot you
  create below. The live bot's token is never touched.
- The database is `sureodds_staging` (via `wrangler.staging.toml`), never
  `sureodds_production`.
- The Cloudflare API token created below is scoped to D1 edit on the staging
  database only — it cannot read or write production, and cannot do
  anything else on the account.
- The production Worker and its webhook are not modified by anything in
  this runbook. Stopping the staging bot (last section) fully undoes
  everything.

---

## 1. Create the test bot (@BotFather)

Do this in Telegram, from your own account:

1. Open a chat with **@BotFather**.
2. Send `/newbot`.
3. When asked for a name, send something clearly marked as a test, e.g.
   `Sureodds Staging Test`.
4. When asked for a username, send one ending in `bot` and not already
   taken, e.g. `SureoddsStagingTestBot` (Telegram will tell you if it's
   taken and let you try again).
5. BotFather replies with a token that looks like `123456789:AA...`. This
   is the **test bot token** — copy it somewhere private (a password
   manager, not a text file in this repo). You'll paste it into the
   server's `.env` in step 4, over SSH, never into chat.
6. Open a chat with your new test bot and send `/start` once, so it shows
   up in your chat list — you'll use this same chat to send test messages
   in step 5.

You do **not** need to set a webhook, set commands, or do anything else in
BotFather. The bot starts with no webhook configured, which is what the
polling bot needs (if a webhook were ever set on this test bot, delete it
first with `https://api.telegram.org/bot<test-token>/deleteWebhook`).

---

## 2. Create the Cloudflare API token (D1 edit, staging database only)

This token must only be able to touch `sureodds_staging` — not production,
not anything else on the account.

1. Go to the Cloudflare dashboard: <https://dash.cloudflare.com/>
2. Click your profile icon (top right) → **My Profile**.
3. Left sidebar → **API Tokens**.
4. Click **Create Token**.
5. Scroll to the bottom and click **Create Custom Token** (do not use a
   template — templates are broader than needed here).
6. **Token name:** something identifying, e.g. `sureodds-staging-bot-d1-edit`.
7. **Permissions** — add exactly one row:
   - Dropdown 1: `Account`
   - Dropdown 2: `D1`
   - Dropdown 3: `Edit`
8. **Account Resources:**
   - Dropdown 1: `Include`
   - Dropdown 2: your account (there should only be one to pick)
9. There is no per-database scope selector in the Cloudflare token UI as of
   this writing — `D1:Edit` grants edit access to every D1 database on the
   account, not just staging. This is a real gap between what was asked
   (scoped to staging only) and what Cloudflare's token UI currently
   offers. Two ways to close it, pick one before continuing:
   - **(a) Preferred, no extra setup:** proceed with the account-wide
     `D1:Edit` token, but treat it as sensitive as a production credential
     and rotate/delete it immediately after the staging test (step 6).
     The bot code itself only ever talks to the database ID in `.env`
     (`sureodds_staging`'s), so a bug in the bot can't reach production by
     accident — the exposure this closes is a *leaked token*, not a *bot
     bug*.
   - **(b) Tighter, more setup:** create a separate Cloudflare account (or
     a dedicated API-only Cloudflare user with access to nothing but a
     project containing just the staging D1 database) to host
     `sureodds_staging` going forward, so a token for that account
     structurally cannot see production. This is a bigger change — don't
     do it just for this staging test; only worth it if staging access
     will be a recurring need.
   - Tell me which you'd rather do before step 10 if you want to discuss
     it further; otherwise this runbook assumes (a).
10. Scroll down, click **Continue to summary**, then **Create Token**.
11. Cloudflare shows the token **once**. Copy it to your password manager
    immediately — same handling as the bot token. You'll paste it into
    the server's `.env` in step 4.
12. You'll also need your **Account ID** for `.env`: still on the
    dashboard, go to **Workers & Pages** → any page's right sidebar shows
    "Account ID", or check the URL after `dash.cloudflare.com/` (it's the
    hex string right after the domain). This is not secret by itself, but
    there's no reason to paste it into chat either — put it straight into
    `.env`.

---

## 3. Install Node 24 and the sparse checkout on the server (as `sureodds`)

SSH in with the `sureodds`-scoped key from the earlier SSH setup (not your
personal key):

```bash
ssh -i ~/.ssh/sureodds_oracle sureodds@<server-ip>
```

Everything below runs as `sureodds`, with no `sudo` anywhere — if a command
ever asks for a password or `sudo`, stop; that means something is wrong
with this runbook or the account, not something to work around.

### 3a. Install Node 24 via nvm (no sudo needed — nvm installs per-user)

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 24
nvm use 24
node --version   # expect v24.x.x
```

### 3b. Sparse-checkout the repo's workers/ directory

```bash
mkdir -p /opt/sureodds-bot 2>/dev/null || true
# if /opt/sureodds-bot doesn't exist or isn't owned by sureodds, stop here
# and ask for it to be created first (that part needs root, done once,
# separately from this runbook)
cd /opt/sureodds-bot
git clone --no-checkout https://github.com/preshious11/Sureodds.git .
git sparse-checkout init --cone
git sparse-checkout set workers
git fetch origin feature/stage1-telegram-polling
git checkout feature/stage1-telegram-polling
ls workers/bot/bot.ts   # confirm it's there
```

(Once Phase 7 is approved and tagged, replace the last two lines with
`git fetch origin refs/tags/<tag>` and `git checkout <tag>` instead of a
branch — branches move, tags don't. For this staging test, the branch is
fine.)

### 3c. Install the bot's one dependency

```bash
cd /opt/sureodds-bot/workers/bot
npm install
```

---

## 4. Create `.env` on the server with `nano` (secrets never touch chat or git)

```bash
cd /opt/sureodds-bot/workers/bot
cp .env.example .env
nano .env
```

Fill in exactly these four values (leave everything else as the
`.env.example` default, or blank where shown):

```
TELEGRAM_BOT_TOKEN=<test-bot-token-from-step-1>
TELEGRAM_ADMIN_IDS=<your-own-telegram-user-id>
TELEGRAM_BOT_USERNAME=<test-bot-username-from-step-1>
TELEGRAM_CHAT_ID=

CF_ACCOUNT_ID=<account-id-from-step-2>
CF_DB_ID=<staging-database-id>
CF_API_TOKEN=<cloudflare-api-token-from-step-2>
```

For `CF_DB_ID`: this is `sureodds_staging`'s database ID from
`workers/wrangler.staging.toml` in the checkout you just made —
`cat /opt/sureodds-bot/workers/wrangler.staging.toml | grep database_id`
will show it. Don't paste that value back to me; I already have it from
the repo, I just don't repeat IDs in chat.

Save and exit nano: `Ctrl+O`, Enter, `Ctrl+X`.

```bash
chmod 600 .env   # only sureodds can read it
```

Confirm nothing secret leaked into the shell's history:

```bash
history | grep -i "TELEGRAM_BOT_TOKEN\|CF_API_TOKEN"
# should show nothing - you only ever typed these inside nano, never on
# the command line
```

---

## 5. Install the systemd unit, start the service, send test messages

This one step needs root, once, to install the service file (copying a
file into `/etc/systemd/system/` is not something a non-sudo user can do).
Everything else here is back to `sureodds`.

**As root** (a separate SSH session, or whoever has the root/admin
credential for this box):

```bash
cp /opt/sureodds-bot/workers/bot/sureodds-telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
```

**Back as `sureodds`:**

```bash
systemctl start sureodds-telegram-bot
systemctl status sureodds-telegram-bot
# expect: active (running)
```

Watch the logs live:

```bash
journalctl -u sureodds-telegram-bot -f
```

In another window (or just leave the log tail running), open your chat
with the **test** bot in Telegram and send:

1. `/start` — expect a reply within a few seconds; expect the log to show
   `{"event":"polling_started",...}` already printed, then no errors
   after you send the message.
2. `/top` and `/daily` — expect normal replies.
3. A Custom Picks request in plain text, e.g. `3 basketball picks` —
   expect either a ticket or an honest "nothing qualifies" message, not an
   error.
4. Check the offset file is advancing:
   ```bash
   cat /var/lib/sureodds-bot/polling.offset
   # re-run after sending another message - the number should increase
   ```

Things that would mean **stop and report back, don't continue**:
- Any log line containing `"event":"quota_exhausted"` — would mean the
  staging D1 is somehow already near its write quota, worth knowing before
  any further testing.
- Any log line containing `"event":"fatal_polling_error"` — the process
  exited; `systemctl status` would show it as inactive/failed.
- A Telegram reply arrives, but editing/pagination buttons don't work
  (tap "◀️"/"▶️" on `/all` if there's more than one page) — would point at
  a D1 write issue through the REST adapter specifically.

Let it run for at least 30–60 minutes of normal use before moving to
step 6, so there's a real chance to see a quota or network blip, not just
the first successful reply.

---

## 6. Stop the staging test and clean up

```bash
systemctl stop sureodds-telegram-bot
systemctl status sureodds-telegram-bot   # expect: inactive (dead)
```

Then, back on the Cloudflare dashboard (from step 2):

1. **My Profile → API Tokens** → find the token you created → **Roll** or
   **Delete** it. Don't leave a live D1-edit token sitting around between
   tests.

And in Telegram, optionally:

2. Message @BotFather → `/mybots` → select the test bot → **Delete Bot**,
   if you don't expect to reuse it for a later staging round. Keeping it
   around for Stage 2's staging test is also fine — it's free and isolated
   either way.

Nothing about this stop sequence touches the live bot, the live webhook,
or production — they were never part of this runbook.

---

## 7. What to send back

Once you've run this, the useful things to report back are:

- Whether the service started cleanly (`systemctl status` output).
- Whether `/start`, `/top`, `/daily`, and a Custom Picks request all got
  replies, and roughly how fast (a few seconds, matching long-poll +
  processing time, or something slower worth investigating).
- Whether the offset file advanced as expected.
- Any log lines containing `"event":"quota_exhausted"`,
  `"event":"update_failed"`, or `"event":"fatal_polling_error"` — paste
  these (they contain no secrets by design) rather than summarizing them.
- How long you ran it and whether anything degraded over that time.

No live cutover step follows from this automatically — that's a separate,
explicit approval after this report.
