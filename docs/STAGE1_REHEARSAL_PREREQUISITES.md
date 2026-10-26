# Rehearsal Prerequisites (Stop Point 3)

Everything below is something only you can do - server/account access,
secrets, and account-level IAM setup. Nothing here is executed by me;
this is the exact list, in order, with placeholders only.

**Boundaries unchanged:** nothing here touches the live bot token, the
live webhook, the production Worker, or production D1. The test bot
token is a separate bot you create below. The server's SQLite file is a
copy of production data for the rehearsal, not production D1 itself -
production D1 keeps running exactly as it does today throughout.

---

## 1. Create the test bot (@BotFather)

Same as the earlier bot-only runbook, repeated here for completeness:

1. Open a chat with **@BotFather** in Telegram.
2. Send `/newbot`.
3. Name: something clearly marked as a test, e.g. `Sureodds Rehearsal`.
4. Username: ending in `bot`, not already taken, e.g. `SureoddsRehearsalBot`.
5. Copy the token BotFather replies with to your password manager - never
   into chat, never into a file in this repo.
6. Open a chat with the new bot and send `/start` once.
7. Confirm no webhook is set on it: `https://api.telegram.org/bot<test-token>/deleteWebhook` (safe to call even if none was ever set).

---

## 1b. Create test chats (every chat the system can post to, during rehearsal)

The production data copy (step 8) contains the real community group id and
real subscriber ids. **None of those are ever read for where to post** -
the bot only ever posts to whatever `TELEGRAM_CHAT_ID` and
`TELEGRAM_ADMIN_IDS` say in `.env` (step 7), and subscriber broadcasting
is disabled in the code itself (confirmed by a regression test - see
`workers/bot/tests/rehearsal-guard.test.mjs`). This section is about
creating chats to put in `.env`, not about anything reading the copied
data for addresses.

1. **Test group**: create a new Telegram group (any name, e.g. "Sureodds
   Rehearsal"), add the test bot (step 1) to it, and make it an admin of
   the group (same permissions the live bot has in the real community
   group - needed to post and pin). Get its chat id: send any message in
   the group, then check
   `https://api.telegram.org/bot<test-token>/getUpdates` - the group's
   `chat.id` is a negative number, shown in the JSON response.

2. **Test admin identity**: this needs to be a Telegram account that is
   **not** the same account configured as the production admin today -
   otherwise the rehearsal guard (step 7) can't tell the difference, and
   more importantly, you wouldn't actually be testing isolation. If you
   have a second Telegram account (many people do, for exactly this kind
   of testing), use its user id. If not, either:
   - create a second Telegram account on a different phone number (a
     Google Voice number or similar works fine for this), or
   - use a throwaway/test account you don't mind associating with this
     repo's testing.

   Get that account's user id the same way: message the test bot from it,
   check `getUpdates`, read `message.from.id`.

3. Keep both ids (test group, test admin) somewhere you can paste into
   `.env` in step 7 - they are not secret like the bot token, but there is
   no reason to paste them into chat either.

---

## 2. Server: create the `sureodds` user and directories (root, one-time)

SSH in as whatever admin account you already have on the Oracle VM, then:

```bash
sudo useradd -m -s /bin/bash sureodds
sudo mkdir -p /opt/sureodds /var/lib/sureodds /var/lib/sureodds/incoming /var/lib/sureodds/backups /var/lock
sudo chown -R sureodds:sureodds /opt/sureodds /var/lib/sureodds
```

Confirm `sureodds` has **no sudo** access (this is the whole point - it
should not be in `sudo`/`wheel`):

```bash
sudo -u sureodds sudo -n true 2>&1
# expected: "sudo: a password is required" or "sureodds is not in the sudoers file" - NOT a successful sudo
```

---

## 3. Your own SSH key (unrestricted, for your own admin access)

```bash
# On your own machine:
ssh-keygen -t ed25519 -f ~/.ssh/sureodds_oracle -C "sureodds-admin-$(date +%Y%m%d)" -N ""
```

Send me the contents of `~/.ssh/sureodds_oracle.pub` (the public key only)
- I'll give you the exact line to add. As root on the server:

```bash
sudo mkdir -p /home/sureodds/.ssh
sudo tee -a /home/sureodds/.ssh/authorized_keys <<'EOF'
<paste-your-public-key-line-here>
EOF
sudo chmod 700 /home/sureodds/.ssh
sudo chmod 600 /home/sureodds/.ssh/authorized_keys
sudo chown -R sureodds:sureodds /home/sureodds/.ssh
```

Test:
```bash
ssh -i ~/.ssh/sureodds_oracle sureodds@<server-ip>
# should land in /home/sureodds with no password prompt
```

---

## 4. Node 24 via nvm, as `sureodds` (no sudo)

```bash
ssh -i ~/.ssh/sureodds_oracle sureodds@<server-ip>
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 24
nvm use 24
node --version   # expect v24.x.x
```

Also confirm `sqlite3` (the CLI, used by `backup.sh`'s `VACUUM INTO`) and
`python3` (used by the closing job and `build_plan_manifest.py` on the
Actions side, not the server - but check anyway in case a future piece
needs it server-side) are available:

```bash
sqlite3 --version
python3 --version
```

If either is missing, that needs a `sudo apt install` by an admin account
(not `sureodds`) - tell me and I'll give you the exact package names.

---

## 5. Sparse checkout the repo onto the server

As `sureodds`:

```bash
cd /opt/sureodds
git clone --no-checkout https://github.com/preshious11/Sureodds.git .
git sparse-checkout init --cone
git sparse-checkout set workers predictions-bot
git checkout feature/stage1-server-migration
ls workers/bot/bot.ts   # confirm it's there
```

(For the rehearsal we check out this branch directly, by name - once this
is merged and tagged, future deploys use a tag instead, as the plan doc
describes.)

```bash
cd /opt/sureodds/workers/bot
npm install
cd /opt/sureodds/predictions-bot
pip install -r requirements.txt
```

---

## 6. Install the systemd units (root, one-time)

```bash
sudo cp /opt/sureodds/workers/bot/systemd/*.service /opt/sureodds/workers/bot/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Do **not** `systemctl enable`/`start` any of them yet - that happens
during the rehearsal itself, once `.env` (next step) exists.

---

## 7. Create `.env` on the server with `nano`

```bash
cd /opt/sureodds/workers/bot
cp .env.example .env
nano .env
```

Fill in (leave anything not listed here as the `.env.example` default).
**`TELEGRAM_ADMIN_IDS` and `TELEGRAM_CHAT_ID` are the TEST identities from
step 1b, never your production admin id or the real community group id**
- the rehearsal guard below exists specifically to catch the mistake of
pasting the wrong one in:

```
TELEGRAM_BOT_TOKEN=<test-bot-token-from-step-1>
TELEGRAM_ADMIN_IDS=<test-admin-user-id-from-step-1b>
TELEGRAM_BOT_USERNAME=<test-bot-username-from-step-1>
TELEGRAM_CHAT_ID=<test-group-id-from-step-1b>

SQLITE_DB_PATH=/var/lib/sureodds/sureodds.db
POLLING_OFFSET_FILE=/var/lib/sureodds/polling.offset

STAGE3_CRON_ENABLED=1

GITHUB_REPO=preshious11/Sureodds
GITHUB_REF=feature/stage1-server-migration
GITHUB_DISPATCH_TOKEN=<a-fine-grained-PAT-scoped-to-this-repo-Actions-read-write-optional-for-the-rehearsal>

OCI_BACKUP_BUCKET=<your-bucket-name-from-step-9>
OCI_BACKUP_NAMESPACE=<your-object-storage-namespace-from-step-9>

REHEARSAL_MODE=true
PRODUCTION_BOT_TOKEN_HASH=<see below>
PRODUCTION_CHAT_IDS=<see below>
```

**`PRODUCTION_BOT_TOKEN_HASH`**: on your own machine (never on the
server, never in this file in chat), hash the real production bot token -
the hash is all that's needed for the guard to detect a mistake, the real
token itself never has to exist in the rehearsal `.env` at all:

```bash
echo -n "<the real production bot token>" | sha256sum
```

Paste the resulting hex string (not the token) as `PRODUCTION_BOT_TOKEN_HASH`.

**`PRODUCTION_CHAT_IDS`**: a comma-separated list of every real chat id
the live bot ever posts to - the real community group id and the real
admin id(s) from `workers/wrangler.toml`'s `TELEGRAM_ADMIN_IDS`. These
are not secret the same way a token is, but still don't need to be pasted
into chat - read them yourself from `wrangler.toml` and the group's own
settings.

With these three set, `bot.ts` and every `cron/*.ts` entry point refuse
to start (loudly, before any Telegram call) if `TELEGRAM_BOT_TOKEN`
hashes to `PRODUCTION_BOT_TOKEN_HASH`, or if `TELEGRAM_CHAT_ID`/any entry
of `TELEGRAM_ADMIN_IDS` appears in `PRODUCTION_CHAT_IDS` - see
`workers/bot/rehearsal-guard.ts` and its tests.

Save: `Ctrl+O`, Enter, `Ctrl+X`. Then:

```bash
chmod 600 .env
history | grep -i "TELEGRAM_BOT_TOKEN\|GITHUB_DISPATCH_TOKEN\|PRODUCTION_BOT_TOKEN_HASH"
# should show nothing - these were only ever typed inside nano
```

Confirm the guard actually works before relying on it - deliberately set
`TELEGRAM_CHAT_ID` to one of the `PRODUCTION_CHAT_IDS` values temporarily
and start the bot (`node bot.ts` directly, not via systemd, so you see
the output):

```bash
node bot.ts
# expect: "REHEARSAL GUARD: configured chat id '...' matches a production
# chat id..." printed to stderr, then the process exits - NOT a normal
# "polling_started" line
```

Then put the real test group id back before continuing.

---

## 8. A copy of production data for the rehearsal

This is the one step that touches production D1 - **read-only**, an
export, never a write:

```bash
# From your own machine or any environment with the production Cloudflare
# credentials already configured for wrangler (NOT run as sureodds on the
# server - this runs wherever you already run other D1 admin commands):
cd workers
npx -y wrangler@4.151.1 d1 export sureodds_production --remote --output=/tmp/sureodds-rehearsal-export.sql --config wrangler.toml
```

Copy that export to the server and load it into a fresh SQLite file as
`sureodds`:

```bash
scp -i ~/.ssh/sureodds_oracle /tmp/sureodds-rehearsal-export.sql sureodds@<server-ip>:/tmp/
ssh -i ~/.ssh/sureodds_oracle sureodds@<server-ip>
sqlite3 /var/lib/sureodds/sureodds.db < /tmp/sureodds-rehearsal-export.sql
rm /tmp/sureodds-rehearsal-export.sql
```

This is a **copy** - the rehearsal writes to this file, never back to
production D1. Production D1 is unaffected by anything in the rehearsal.

---

## 9. Oracle Object Storage: bucket + instance principal setup

### 9a. Create the bucket (if it doesn't exist yet)

OCI Console → hamburger menu → **Storage** → **Buckets** → select your
compartment → **Create Bucket**. Name it something identifying, e.g.
`sureodds-backups`. Default settings (Standard tier) are fine. Note the
bucket name and your tenancy's **Object Storage Namespace** (shown on the
bucket's detail page, near the top) - both go into `.env` in step 7.

### 9b. Dynamic group (so the compute instance can authenticate as itself)

OCI Console → **Identity & Security** → **Domains** → your domain →
**Dynamic Groups** → **Create Dynamic Group**.
- Name: `sureodds-backup-instance`
- Matching rule: `ALL {instance.id = '<your-instance-OCID>'}` (find the
  instance OCID on the Compute instance's detail page, "Copy" next to the
  OCID field)

### 9c. Policy (what that dynamic group is allowed to do)

**Identity & Security** → **Policies** → **Create Policy**, in the same
compartment as the bucket.
- Name: `sureodds-backup-policy`
- Statements (replace the compartment name):
  ```
  Allow dynamic-group sureodds-backup-instance to manage objects in compartment <your-compartment-name> where target.bucket.name='sureodds-backups'
  ```

### 9d. Install the OCI CLI on the server (as `sureodds`, no sudo needed - it installs to the user's home directory)

```bash
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --install-dir ~/lib/oracle-cli --exec-dir ~/bin --accept-all-defaults
export PATH=$PATH:~/bin   # also add this line to ~/.bashrc
oci --version
```

### 9e. Confirm instance principal auth actually works

```bash
oci os ns get --auth instance_principal
# expect: your namespace printed back, no error
```

If this fails, tell me the exact error - it almost always means the
dynamic group's matching rule or the policy's compartment/bucket name
doesn't match. The API-key fallback in `backup.sh` means backups still
work while this gets fixed, just not via the preferred, no-credential path.

---

## 10. GitHub secrets for the SSH relay

Repo → **Settings** → **Secrets and variables** → **Actions** → **New
repository secret**, three secrets:

### 10a. `SUREODDS_SSH_HOST`
Value: the server's public IP or hostname.

### 10b. `SUREODDS_SSH_KEY` - the relay's OWN key, not your admin key

Generate a **separate** key pair just for GitHub Actions deliveries (on
your own machine, or anywhere - this key's private half goes into GitHub
and nowhere else, not even your own `~/.ssh/`):

```bash
ssh-keygen -t ed25519 -f ./sureodds-relay-key -C "github-actions-delivery" -N ""
```

Paste the **private** key's full contents (`cat ./sureodds-relay-key`,
including the `-----BEGIN...-----`/`-----END...-----` lines) as this
secret's value. Then delete the local copy of the private key - once it's
in GitHub, it doesn't need to exist anywhere else:

```bash
shred -u ./sureodds-relay-key   # or just rm, on a disk where that's acceptable
```

Now install the **public** key on the server, restricted to the forced
command (as root, since editing another user's `authorized_keys` this way
is simplest done once by root):

```bash
sudo tee -a /home/sureodds/.ssh/authorized_keys <<'EOF'
command="/opt/sureodds/workers/bot/ssh-relay/receive-batch.sh",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding ssh-ed25519 <paste-the-PUBLIC-key-here, from sureodds-relay-key.pub> github-actions-delivery
EOF
```

This key can **only** ever run `receive-batch.sh`, regardless of what
command is requested - verified by the `command=` prefix, same mechanism
your own key (step 3) deliberately does NOT have.

### 10c. `SUREODDS_SSH_KNOWN_HOSTS` - the pinned host key

From any machine (does not need to be yours specifically, just needs
network access to the server once, right now, to capture its real key):

```bash
ssh-keyscan -t ed25519 <server-ip>
```

Paste the **entire output line** (starts with the server's IP/hostname,
then `ssh-ed25519`, then the key) as this secret's value. This is
captured **once** - if the server's host key ever legitimately changes
(a reinstall, a new host key generated), delivery will start failing
closed (by design) until this secret is updated to match, rather than
silently trusting whatever key shows up.

---

## 11. Verify the relay end-to-end (before the full rehearsal)

A quick manual check that the pieces above actually connect, run from
your own machine with the relay's private key available locally just for
this one test (delete it again afterward - the real copy lives only in
the GitHub secret):

```bash
ssh -i ./sureodds-relay-key -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=<(ssh-keyscan -t ed25519 <server-ip>) \
    sureodds@<server-ip> "epoch-get"
# expect: {"epoch":0} (or whatever the loaded export's epoch is)
```

If this returns the epoch cleanly, the forced command, the allow-list,
and the key restriction are all working.

---

## 12. Summary checklist

- [ ] Test bot created, token saved to password manager
- [ ] Test group created, test bot added as admin, chat id captured
- [ ] Test admin identity (a DIFFERENT account from production's admin) ready, user id captured
- [ ] `sureodds` user exists, confirmed no sudo
- [ ] Your own SSH key added, tested
- [ ] Node 24 installed via nvm
- [ ] Repo sparse-checked-out to `/opt/sureodds`
- [ ] systemd units installed (not started)
- [ ] `.env` created via `nano`, mode 600, nothing in shell history, including `REHEARSAL_MODE=true`, `PRODUCTION_BOT_TOKEN_HASH`, `PRODUCTION_CHAT_IDS`
- [ ] Rehearsal guard tested directly (deliberately wrong chat id → refuses to start), then corrected
- [ ] Production D1 exported (read-only) and loaded into the server's SQLite file
- [ ] Object Storage bucket created
- [ ] Dynamic group + policy created; `oci os ns get --auth instance_principal` succeeds
- [ ] `SUREODDS_SSH_HOST`, `SUREODDS_SSH_KEY`, `SUREODDS_SSH_KNOWN_HOSTS` set as GitHub secrets
- [ ] Manual `epoch-get` check succeeds
- [ ] Read `docs/STAGE1_REHEARSAL_SIDE_EFFECTS.md` - when dispatching stage5 for the rehearsal, use `operation=fixture-replay` and `allow_telegram_posts=false` (the workflow now refuses `write_target=server` with either set wrong, but worth knowing why)

Once all of this is checked off, tell me and I'll run the rehearsal itself
against this setup and report results at stop point 4.
