# ingest: bank data into a self-hosted Sure

Pulls statements and transactions from E\*TRADE, Wealthsimple, Simplii, Amex and
Sun Life into Sure, with no third-party aggregator anywhere in the path. Every
institution is driven by a deterministic Playwright script on our own hardware;
a local model is only ever used offline, by a human, to propose a fix for a
broken script.

This folder is self-contained. It doesn't touch the Rails app, so syncing the
fork with `we-promise/sure` stays conflict-free.

```
ingest/
  src/ingest/      runner (scraper), steps (script format), loader, parsers/, repair, scrub, sure (API client), cli
  banks/           one JSON script per institution
  tests/           unit + end-to-end tests against a local fake bank
  deploy/          Sure compose, ingest compose, Dockerfile entrypoint, systemd, VM and host setup scripts
  Dockerfile       two targets: scraper, loader
```

## Architecture

```
               finance VM (one VM: Sure + scraper + loader)
 ┌────────────────────────────────────────────────────────────────────────┐
 │  systemd timer (03:30) ── nightly.sh ── one bank at a time, then load  │
 │                                                                        │
 │  scraper container (scraper_net)          loader container (sure_net)  │
 │  headed Chrome + Xvfb, persistent         parse -> check -> JSONL ->   │
 │  profile per bank, bw at run time         POST /api/v1/transactions    │
 │        │ writes                              │ reads ro      │         │
 │        ▼                                     ▼               ▼         │
 │  /srv/finance/raw/<bank>/<year>/  ──────────►  /srv/finance/normalized │
 │                                                                        │
 │  Sure (web, worker, db, redis, backup) on sure_net ── web bound to the │
 │  tailnet IP only (never the LAN); backups to sure-backups every 6 h    │
 └────────────────────────────────────────────────────────────────────────┘
   /srv/finance = naspool/finance via virtiofs     Vaultwarden LXC (bank logins)
   push (optional, generic text) ◄── failures      Ollama on this VM, 127.0.0.1 (repair only)
```

| Path on the VM | What | Owner |
|---|---|---|
| `/opt/sure/` | Sure `compose.yml` + `.env` (600) | root |
| `/opt/ingest/` | sparse checkout of this repo (`ingest/` only) | root |
| `/etc/ingest/ingest.json`, `banks.list` | config | root:finance 640 |
| `/srv/ingest-secrets/` | browser profiles, `bw/` credentials, `sensitive.json`, push token | ingest-scraper 700 |
| `/srv/ollama` | repair model weights | root 700 |
| `/srv/loader-secrets/sure_api_key` | Sure API key | ingest-loader 400 |
| `/srv/ingest-state/bundles` | failure bundles | ingest-scraper 700 |
| `/srv/ingest-state/review` | files that failed a check | ingest-loader |
| `/srv/ingest-state/repair-queue` | repairs waiting for the model | root 700 |
| `/srv/finance/raw/<bank>/<year>/` | original downloads, mode 444, never changed | ingest-scraper:finance 2750 |
| `/srv/finance/normalized/` | canonical JSONL + `.ledger.json` | ingest-loader |
| `/srv/finance/sure-backups/` | Sure's backup output | root |

Nothing under `/srv/ingest-secrets`, `/srv/loader-secrets` or `/srv/ingest-state` is on the share, and none of it is backed up off the VM.

### Canonical record

`account_ref, date, amount, currency, description, source, external_id, raw_file`.
`amount` is signed from the holder's view: positive = money in, negative = money out.
The loader sends Sure a positive `amount` with `nature: income|expense`.

`external_id = sha256(source|account|date|amount|description|n)`, where `n` counts
earlier identical rows **in the same file**. Two identical coffees on one day get
different ids; tomorrow's overlapping file reproduces the same ids. Sure returns
201 for a new `source` + `external_id` and 200 with the existing transaction for
a repeat, so overlap is harmless. Caveat: if a bank rewrites a description
(pending -> posted), the row counts as new. Keep the window to posted transactions
where the bank allows it.

Raw files are named `<account_ref>__<anything>.<ext>`; the loader gets the
account from the name and maps `<bank>:<account_ref>` to a Sure account name
via `accounts` in `ingest.json`.

## Security model

- **No middlemen.** Banks are reached only from Chrome on this VM. No Plaid,
  SimpleFIN, SnapTrade, Flinks or any hosted LLM; Sure's provider connectors are unused.
- **No model in a live session.** Scripts are JSON data, run step by step. A
  model is used only by `ingest repair`, offline, on a scrubbed snapshot, with two
  human confirmations.
- **One login attempt per run.** No retries anywhere. Any failure, unexpected page or
  new window stops the run and sends a generic push.
- **Read-only allowlist.** Every request passes `page.route`: top-level pages must
  match the script's `navigation` patterns, non-GET requests its `post` patterns,
  subresources its `hosts`. Money-moving words (transfer, e-transfer, Interac,
  payee, bill pay, payment, wire, withdraw, trade, order, and French forms) are
  always aborted, whatever the script says (`steps.DEFAULT_BLOCK`). An aborted
  navigation stops the run. In single-page apps, route changes after `#` never
  reach the network, so the guard that matters is the `post` list: any
  state-changing request not on it is aborted. Keep `post` to the exact sign-on
  and export endpoints.
- **Secrets** come from a dedicated Vaultwarden account with only the bank logins,
  through `bw` with a temporary appdata dir. They're unlocked at run time, go
  straight into `locator.fill`, and are locked after. They never appear in the
  container env, CLI args, logs, traces or bundles; error text is redacted against
  every value used.
- **No tracing or video, ever.** Screenshots only on failure *after* login. They
  stay in the bundle folder on the VM and are never read by the repair command.
- **Staying signed in (fewer "new device" flags):** each bank has its own Chrome
  profile under `/srv/ingest-secrets/profiles/<bank>`, kept between runs. Cookies,
  local storage and the bank's "remember this device" token survive, so night after
  night the bank sees the same browser. The first sign-in is done by hand
  (`scrape <bank> --interactive`, where you type the code in the terminal). After
  that, codes should be rare. Tested: a second run with the same profile skips the code.
- **MFA at night:** when a bank asks for a code during the unattended run, the run
  stops at once (`mfa.unattended: "stop"`) and, if a push target is set, you get
  "Finance sync: <bank> wants a verification code. Run it by hand when you can."
  It doesn't sit waiting at 3:30 a.m. To have a scheduled run wait for a code
  instead, set `"form"`: it serves a one-time page (random URL token) on `MFA_BIND`
  and sends its link in the push.
- **Not announcing automation:** real Chrome, headed under Xvfb (so no "HeadlessChrome"
  user agent), real window size, Canadian locale and timezone, and Playwright's
  `--enable-automation` removed, so `navigator.webdriver` is false (tested). This
  avoids the obvious tells. It doesn't defeat serious fingerprinting; the persistent
  profile is what does most of the work.
- **Containers:** non-root UIDs, `cap_drop: ALL`, `no-new-privileges`, read-only
  root, tmpfs `/tmp`, no Docker socket, CPU/memory/pid caps. The scraper is on
  `scraper_net` and can't reach Sure; the loader holds only the Sure key.
- **Chrome sandbox: on.** This is Chrome's own process isolation: web pages run in
  renderer processes that can't touch files or the network directly. It has nothing
  to do with bot detection. It matters because, without it, a hostile script on a
  page (a compromised ad or analytics include on a bank page) that exploits the
  renderer could read everything the scraper UID can, including the vault
  credentials. Chrome builds the sandbox from user, pid and net namespaces, which
  Docker's default seccomp profile only allows with `CAP_SYS_ADMIN`. So the scraper
  runs with `seccomp=unconfined` and an AppArmor profile (`deploy/ingest/apparmor-ingest-scraper`:
  docker-default plus `userns`). Chrome then applies its own, stricter seccomp-BPF
  filter to every renderer. The container keeps no capabilities, no-new-privileges,
  a read-only root and a non-root user. If Chrome can't build its sandbox it refuses
  to start ("No usable sandbox"); it never silently runs without one.

### Repair scrubbing

The snapshot in a failure bundle is scrubbed when it's taken. It keeps only
button, link, textbox, combobox, checkbox, heading, dialog, menuitem and tab, with
role and accessible name only. Values, text and attributes are dropped, each run
of digits becomes `#`, words outside `src/ingest/vocab.txt` (English/French UI
words) become `‹x›`, and link URLs become path patterns (`/accounts/:id/history`).

Before anything is shown or sent, a **blocking check** runs over the whole prompt
against usernames and custom fields from the vault, `sensitive.json` (your name,
address, phone, email) and every amount from the last 120 days of normalized data.
Any hit aborts.

The model answers in a JSON schema (Ollama structured outputs, `keep_alive: 0`)
that allows only two kinds of fix:

- `change_locator` on one step;
- `add_step` with a click, wait or dismiss, or a fill that reuses a login secret.

Locators are role, label, text, placeholder or testid; no CSS and no code. The
reply is validated again: no URLs off the allowlist, nothing that names a
money-moving action, and the resulting script must parse. You see the diff and
type `yes` before it's written and committed.

## Setup

Run all VM steps in a root shell.

1. **Host (cassio).** Create the `finance-data` virtiofs directory mapping for
   `/naspool/finance` and attach it to the VM. Then set ownership to match the
   VM's UIDs (virtiofs passes them through):
   `deploy/host/cassio-finance-perms.sh`
2. **Checkout on the VM:**
   ```sh
   git clone --filter=blob:none --sparse https://github.com/The-Agency-Labs/sure /opt/ingest
   git -C /opt/ingest sparse-checkout set ingest
   /opt/ingest/ingest/deploy/vm-setup.sh
   ```
   This creates the users (`finance` 2000, `ingest-scraper` 2001, `ingest-loader` 2002),
   the directories and permissions in the table above, the `/srv/finance` fstab
   entry, the `sure_net` and `scraper_net` networks, the systemd timer, and starter
   copies of the configs.
3. **Sure:** fill `/opt/sure/.env` (`SECRET_KEY_BASE`, `POSTGRES_PASSWORD`,
   `SURE_BIND_ADDR` = the VM's tailnet IP from `tailscale ip -4`, never the LAN IP).
   Sure is reachable only over the tailnet. `vm-setup.sh` makes Docker wait for
   Tailscale at boot, because Docker can't publish a port on an address that doesn't
   exist yet and won't retry a container that failed to bind. Then
   `cd /opt/sure && docker compose up -d && docker compose --profile backup up -d backup`.
   Create your user and accounts in the UI. Then create an API key with read/write
   scope under Settings and write it to `/srv/loader-secrets/sure_api_key`.
4. **Vault:** in Vaultwarden, create a dedicated account (or org collection) that
   holds only the bank logins. Name the items after the bank (`simplii`, `amex`, …).
   Put the API key client id/secret, the server URL and the master password in
   `/srv/ingest-secrets/bw/{bw_clientid,bw_clientsecret,bw_server,bw_password}`.
5. **Config:** edit `/etc/ingest/ingest.json` (push target in `notify`, `accounts`
   mapping, and timezone if not Toronto). Copy `deploy/ingest/sensitive.example.json`
   to `/srv/ingest-secrets/sensitive.json` and fill it in. List the banks in
   `/etc/ingest/banks.list`.
6. **Build:** `cd /opt/ingest/ingest/deploy/ingest && docker compose build --build-arg BW_VERSION=<x> --build-arg BW_SHA256=<sha>`
   (take both from the Bitwarden CLI release page).
7. **Repair model:** `docker compose --profile repair up -d ollama && docker compose exec ollama ollama pull qwen2.5:3b`
8. **First sign-in by hand** for each bank, so you can type the code and tick
   "remember this device": `docker compose run --rm scraper scrape simplii --interactive`,
   then `docker compose run --rm loader load`.
9. `systemctl start ingest-nightly.timer`
10. **Repair tooling (VM, outside the containers).** It needs git and the vault:
   ```sh
   apt install python3-venv
   python3 -m venv /opt/ingest-venv && /opt/ingest-venv/bin/pip install /opt/ingest/ingest
   ```
   Also install the same pinned `bw` binary to `/usr/local/bin` (unzip, `sha256sum -c`).

Sun Life: drop the quarterly PDF into `/srv/finance/raw/sunlife/<year>/rrsp__<quarter>.pdf`
(any name starting with the account ref and `__`). The next load posts the closing
balance as a valuation with `upsert: true`.

## Recording a bank script (Simplii first)

`banks/simplii.json` is a **placeholder**: the URL, selectors and steps are guesses,
and the scraper refuses to run it while `"placeholder": true`. To record it, on
your PC (not the VM):

```sh
cd ingest && uv sync && uv run playwright install chrome
uv run playwright codegen --channel chrome --target python-pytest \
  "https://online.simplii.com/ebm-resources/public/client/web/index.html#/signon"
```

Sign in, go to the chequing account and download the CSV for the last 30 days.
Don't touch anything else. Then translate the recording into steps:

| codegen line | step |
|---|---|
| `page.goto(url)` | `{"action": "goto", "intent": "...", "url": url}` (login only) |
| `page.get_by_label("Card number").fill("…")` | `{"action": "fill", "intent": "...", "locator": {"label": "Card number"}, "secret": "simplii/username"}` |
| `page.get_by_role("button", name="Sign on").click()` | `{"action": "click", "intent": "...", "locator": {"role": "button", "name": "Sign on"}}` |
| a pop-up you closed | `{"action": "dismiss", ...}` (skipped if it doesn't appear) |
| the verification page | one `mfa` step: `detect`, `input`, `submit` locators |
| `with page.expect_download(): ….click()` | `{"action": "download", ..., "save_as": "chequing__{today}.{ext}"}` |

Never copy a typed value from codegen into the script. Literal values are rejected;
use `"secret": "<vault item>/<field>"`.

Then:

- tighten `allowlist.navigation` to the pages you visited, and `post` to the sign-on
  and export endpoints. Read the "aborted request" lines from a first run to see
  what else is needed;
- add an `expect` step after login;
- run `uv run ingest check banks/simplii.json`;
- remove `"placeholder": true` and commit;
- check `parsers/simplii.py` against a real CSV (column order, date format, header).

Repeat for the other banks. If a bank's CSV isn't Simplii's shape, add a
`mapped_csv` entry in `formats`, or a new parser in `parsers/` registered in
`loader.PARSERS`.

## When a script breaks: repair

A failed step leaves `/srv/ingest-state/bundles/<bank>-<time>/bundle.json` (plus
`screenshot.local.png` if it failed after login, for your eyes only). On the VM, as root:

```sh
/opt/ingest-venv/bin/ingest repair /srv/ingest-state/bundles/simplii-20261117T033012
```

1. The blocking check runs. If anything sensitive is found, nothing is shown or sent.
2. The **exact** prompt is printed. Type `yes` to send it to the configured
   endpoint (`repair.url`, default the Ollama container on this VM at
   `127.0.0.1:11434`). `--endpoint`/`--model` override it.
3. The reply is validated, then shown as a diff. Type `yes` to write
   `banks/<bank>.json` and commit it.
4. Push the commit and re-run the bank by hand.

If the endpoint is offline, the bundle (without the screenshot) is queued and you
get a push. Run `/opt/ingest-venv/bin/ingest repair --queue` later.

**Where the model runs.** By default, on the finance VM, CPU-only:
`qwen2.5:3b` (about 2 GB) in an Ollama container listening on 127.0.0.1 only. Nothing
leaves the VM and no firewall rules are needed. With 3 vCPUs, expect an answer in
roughly a minute or two (an estimate, not measured). That's fine for a rare,
human-attended fix. The job is narrow (match an intent like "download transactions"
to one of the listed elements, in a fixed schema), and every answer is validated and
reviewed. If the 3B model's suggestions turn out poor, point `--endpoint` at the
main PC (GTX 1060 6 GB) running `qwen2.5:7b-instruct-q4_K_M`. That needs Ollama
there with the firewall rules below.

## Tests

```sh
cd ingest
uv sync && uv run playwright install chromium
uv run pytest                    # everything that runs offline
uv run pytest -m browser         # just the fake-bank end-to-end tests
xvfb-run -a uv run pytest -m browser   # same, but you can flip RunConfig.headless off to watch
```

What's covered: the runner end to end against a local fake bank (login, MFA,
promo pop-up, CSV saved read-only, transfer link blocked, failure bundle on a
renamed button, no secrets in bundles, one login attempt, MFA timeout, an
unattended run stopping at a code request, the "remembered device" surviving
between runs, `navigator.webdriver` false with the sandbox on, tracing never started); the parsers (made-up CSVs, a generated PDF, French statements);
`external_id` stability and the occurrence index; scrubbing and the blocking
check (fake secrets caught); repair schema validation (code, CSS, URLs and
money-moving targets rejected) and the interactive flow with a stub model; and
the loader against a fake Sure API that mimics 201/200 idempotency.

### Against a real Sure (opt-in)

Create a throwaway manual account in a test Sure, then:

```sh
SURE_TEST_URL=http://127.0.0.1:3000 SURE_TEST_API_KEY_FILE=/path/key SURE_TEST_ACCOUNT="Ingest Test" \
  uv run pytest -m sure
```

It expects 3 created, then 0 created and 3 already present on a re-run.

### Things CI can't test: check these on the real machines

| What | Command (VM, root unless noted) | Expect |
|---|---|---|
| virtiofs mount | `findmnt -t virtiofs /srv/finance` | the `finance-data` source |
| UID passthrough | `su -s /bin/sh ingest-scraper -c 'touch /srv/finance/raw/.t && rm /srv/finance/raw/.t'`, then on cassio `ls -ln /naspool/finance` | raw owned by 2001:2000 |
| secrets not on share | `ls /srv/finance` | only `raw normalized sure-backups` |
| bw login | `docker compose run --rm --entrypoint python scraper -c "from ingest.secrets import BitwardenStore as B; from pathlib import Path; s=B(Path('/srv/ingest-secrets/bw')); print(len(s.get('simplii/username'))>0); s.close()"` | `True` (never print the value) |
| Chrome sandbox works in the container | `docker compose run --rm --entrypoint sh scraper -c 'xvfb-run -a google-chrome --headless=new --dump-dom about:blank'` | `<html>…` and no "No usable sandbox" |
| AppArmor profile loaded | `aa-status \| grep ingest-scraper` | listed in enforce mode |
| push | trigger a test via the configured `notify` target | push arrives |
| Sure bound to the tailnet only | `ss -ltnp \| grep 3000` | only the `100.x` tailnet IP |
| Sure not on the LAN | `curl -m 3 http://<VM LAN IP>:3000` from another LAN machine | timeout/refused |
| Sure on the tailnet | `curl -sI http://<tailnet IP>:3000` from a tailnet device | `HTTP/1.1 200` or a redirect |
| Sure comes back after reboot | `reboot`, then `docker compose -f /opt/sure/compose.yml ps` | `web` running |
| scraper can't reach Sure | `docker compose run --rm --entrypoint python scraper -c "import socket; socket.create_connection(('web',3000),3)"` | fails (name not resolved) |
| Ollama up on the VM | `curl -s http://127.0.0.1:11434/api/tags` | JSON list with `qwen2.5:3b` |
| Ollama not reachable from the LAN | `curl -m 3 http://<VM_IP>:11434/api/tags` from another machine | timeout/refused |
| Ollama structured output | `curl -s http://127.0.0.1:11434/api/chat -d '{"model":"<m>","stream":false,"keep_alive":0,"format":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]},"messages":[{"role":"user","content":"say ok"}]}'` | `{"ok":true}` in `message.content` |
| timer | `systemctl list-timers ingest-nightly.timer` | next run ~03:30 |
| backups | `ls /srv/finance/sure-backups/*/` after 6 h | `*-scheduled` folders |

Only if you use the main PC as the repair endpoint: firewall it so only the finance VM reaches 11434.

- Linux:
  ```sh
  ufw allow from <VM_IP> to any port 11434 proto tcp
  ufw deny 11434/tcp
  ```
  Set `OLLAMA_HOST=0.0.0.0` in the Ollama service.
- Windows (admin PowerShell):
  ```powershell
  New-NetFirewallRule -DisplayName "Ollama from finance VM" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress <VM_IP> -Action Allow
  ```
  Also set `OLLAMA_HOST=0.0.0.0`, and make sure no broader allow rule covers 11434.

## Still unverified

- Simplii: sign-on URL, every selector, CSV columns and date format (`banks/simplii.json` is a placeholder).
- Amex (Canadian card): Amex Canada offers CSV and Excel downloads but publishes no column layout, so the `amex` mapping in `ingest.example.json` is a guess until checked against a real export.
- Chrome's sandbox inside the container (AppArmor profile plus `seccomp=unconfined`) is untested until the first build on the VM. Run the sandbox check in the table above.
- E\*TRADE, Wealthsimple, Sun Life flows are not recorded yet; the Sun Life PDF wording is guessed (tests use made-up statements).
- Container images have not been built in this repo's CI. Build once on the VM and run the checks above.
