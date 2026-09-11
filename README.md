# Budget App: personal bank aggregation over PSD2 (Enable Banking)

A local, single-user budgeting tool that pulls my own bank transactions through the
[Enable Banking](https://enablebanking.com) API (PSD2 account information, read-only),
stores them locally, categorises them with editable rules, and displays them in a
Streamlit interface bound to `127.0.0.1`.

Personal project, built and used with one BNP Paribas account.
The user interface, code comments and setup guide are in French; identifiers are in English.

## Features

- **Read-only bank access**: the consent only requests `balances` and `transactions`. The client exposes no payment endpoint.
- **Idempotent synchronisation**: every fetch re-reads an overlapping window and upserts on a stable identity key, so replaying a fetch never creates duplicates and a *pending* transaction is updated in place once *booked*. All result pages are fetched (`continuation_key`).
- **Rule-based categorisation at read time**: ordered keyword or regex rules with a debit/credit filter, plus per-transaction manual overrides. Editing a rule re-categorises the whole history.
- **Streamlit interface** (four pages):
  - *Overview*: monthly spending bars and category breakdown with drill-down.
  - *Transactions*: filtering, manual corrections, rule creation from a transaction label.
  - *Rules*: table editor for the rule set.
  - *Projects*: either hand-picked transactions, or a budgeted period with an envelope split per category, planned vs. actual tracking and exclusions.
- **CLI**: `list-banks`, `connect`, `fetch`, `show`, `summary`, `unknown`, `revoke`.

## Architecture

```
app.py                    Streamlit interface
run_app.py                Launcher forcing --server.address 127.0.0.1
budget_poc/
├── config.py             Fail-closed configuration from environment variables
├── eb_client.py          Enable Banking client (JWT auth, AIS endpoints, pagination)
├── callback_server.py    Ephemeral HTTPS loopback server capturing the OAuth code
├── tls.py                Self-signed certificate for the loopback callback
├── secure_store.py       Encrypted vault (AES-256-GCM, scrypt-derived key)
├── store.py              SQLite persistence, deduplication, projects
├── categorizer.py        Ordered categorisation rules
├── service.py            Fetch logic shared by the CLI and the interface
└── cli.py                Command-line entry point
tests/                    Unit tests (no network, no bank account needed)
legal/                    Privacy policy and terms templates required by Enable Banking
```

The bank connector knows nothing about budgeting, and the database layer knows nothing about the network: the aggregator could be replaced without touching categorisation or the interface.

## Security design

- **Secrets outside the repository**: configuration comes from environment variables (`.env`, git-ignored). The application refuses to start if a required value is missing.
- **Application authentication**: short-lived RS256 JWT (10 minutes) signed with the application's private key, which can itself be passphrase-protected.
- **OAuth callback**: HTTPS server bound to `127.0.0.1` only, `state` parameter compared in constant time, authorisation code never logged, 5-minute timeout.
- **API client**: TLS verification kept on, timeouts on every call, response bodies never logged, responses validated before use, identifiers URL-encoded before being placed in a path.
- **Encrypted vault** (`store.enc`): holds the PSD2 session identifier and full account identifiers. AES-256-GCM with a key derived from a master passphrase through scrypt (N = 2^15, r = 8, p = 1). A fresh random salt and nonce are used for every write, which is atomic.
- **Database**: parameterised SQL queries only. The `accounts` table keeps only the last four characters of the account IBAN.
- **Interface**: never exposed to the network (`run_app.py`), usage statistics disabled.
- **Dependencies**: pinned versions, checked with `pip-audit`.

## Threat model and known limitations

What the vault protects against: a leak of the data directory or of a backup of it, without the master passphrase.

What it does **not** protect against, by design or for now:

- **The transaction database is not encrypted.** `transactions.db` is plain SQLite, and each transaction's raw JSON may include counterparty names and account numbers. Confidentiality at rest relies on full-disk encryption (e.g. BitLocker).
- **Master passphrase location.** The convenience scripts `start.ps1` and `connect.ps1` load `BUDGET_MASTER_PASSPHRASE` from `.env`. In that mode, anyone able to read the user's files gets both the passphrase and the vault. Stricter option: remove that line from `.env` (the scripts then leave the variable untouched), type the passphrase in the interface when refreshing, and before `connect.ps1` set it for the current PowerShell session only, from `Read-Host -AsSecureString` rather than in a typed command, since PowerShell saves command history to disk.
- **Local malware or access to the running process** is out of scope: the derived key lives in memory during use.
- **File permissions** (`0600` / `0700`) are only enforced on POSIX systems. On Windows, protection relies on the default ACLs of the user profile.
- **Scrypt parameters are not stored in the vault file.** Changing them makes the existing vault unreadable, so `connect` must be run again.
- **Deduplication fallback.** When the bank provides no `entry_reference`, the key is a hash of date, amount, currency, direction and label. Two genuinely identical transactions on the same day would be merged.
- **Single account.** Designed and tested with one account; entry references of several accounts could collide (see `store.py`).
- **Consent lifetime.** PSD2 consent expires (at most 180 days, often less): `connect` must be run again.

## Getting started

Requirements: Python 3.11 or later, and an Enable Banking account with a registered **production** application:
- redirect URL `https://127.0.0.1:8765/callback`;
- publicly reachable privacy policy and terms pages, required at registration (templates in `legal/`).

The full registration procedure is in [`SETUP_ENABLE_BANKING.md`](SETUP_ENABLE_BANKING.md) (French).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # then fill it in
.\connect.ps1                    # bank consent (strong customer authentication in the browser)
.\start.ps1                      # interface on http://127.0.0.1:8501
```

On Linux or macOS, load the variables with `set -a && source .env && set +a`, then run `python -m budget_poc.cli connect` and `python run_app.py`.

## CLI

```bash
python -m budget_poc.cli list-banks                   # exact bank name for EB_ASPSP_NAME
python -m budget_poc.cli connect                      # consent and encrypted session
python -m budget_poc.cli fetch --from 2026-01-01      # the only command calling the API
python -m budget_poc.cli show --from 2026-01-01       # local database, no network
python -m budget_poc.cli summary --from 2026-01-01    # totals per category
python -m budget_poc.cli unknown                      # labels matched by no rule
python -m budget_poc.cli revoke                       # close the session, wipe the vault
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
```

The test suite runs without network access or credentials. It covers:
- **vault**: round trip, tamper and wrong-passphrase rejection, fail-closed behaviour, unique salt and nonce;
- **deduplication**: independence from the session account identifier, pending-to-booked update;
- **database connections**: every connection is closed;
- **API client**: pagination (including empty intermediate pages), repeated continuation keys, malformed responses, URL encoding of identifiers;
- **categorisation**: default rules and rule ordering.

## License

No license: all rights reserved.
