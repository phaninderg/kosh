# Roadmap

Kosh is a self-hosted personal finance tracker for Indian assets. This file is for people who want to work on it: what exists, why it is built the way it is, what real statements taught us, and **a long list of things nobody has done yet** (section 7). Read it with [README.md](README.md).

Status: MVP (branch `main`). The test suite is green and uses synthetic data only.

---

## 1. Goal and hard rules

One local place to see what you own: bank balances, fixed deposits, mutual funds, PPF, NPS and what you owe on credit cards, built from the statements your institutions already send you. Statements arrive by upload or by read-only email (IMAP); parsers turn them into a SQLite ledger; the web UI shows the result.

Rules that are never relaxed:
- **Secrets** (app passwords, date of birth, name, PDF passwords) live only in the OS keyring, environment variables or, in a container, a 0600 file. Never log them, commit them or store them in the database.
- **IMAP is read-only.** Never delete, move or mark mail.
- **Nothing leaves the machine.** No statement data is sent to any service.
- **Money is `Decimal`**, never float.
- **Numbers come from deterministic code.** A parser must refuse a statement whose arithmetic does not close (opening + movements = closing, and the statement's own totals) instead of guessing.
- **Never commit real statements or real personal data.** Fixtures and samples are synthetic.
- **Ambiguity goes to the Review queue**, never guessed.

## 2. How to contribute

1. Pick something from section 7 (or open an issue first for anything big).
2. **Parsers are plugins**: one module in `finance/parsers/`, one class with `name` and `parse(path, password)`, registered in `REGISTRY`. No orchestrator changes. Copy a nearby parser (`axis_card.py` for cards, `icici.py` for bank statements, `nps.py` for holdings statements).
3. **Never build from a real statement.** Build a synthetic one that reproduces the layout (names, numbers and amounts invented): add a builder beside the others in `tests/` and register a sample in `samples/generate.py` (see [samples/README.md](samples/README.md)). If a real statement needs a new layout, describe the layout (column positions, markers, totals) in the issue rather than attaching the file.
4. Add the integrity gates the money allows (balance chain, printed totals) and a test for each refusal. Mutation-check them: break the code on purpose and confirm a test fails.
5. `python -m pytest` must be green. Update the README table and this file.
6. A parser built from a synthetic sample is **unverified** until someone has run it on a real statement; say which in the PR.

---

## 3. What is built

**Sources:** upload (PDF), and read-only IMAP (Gmail, Yahoo, any IMAP host) with per-mailbox sender rules, DKIM/SPF/DMARC verdict checks, UID tracking, a Review queue for statements that cannot be unlocked or parsed, and password templates (`{name4}{ddmm}`, `{ddmmyyyy}{card4}`, ...).

| Area | Parser | Notes |
|---|---|---|
| Mutual funds | `cas` | CAMS / KFintech / MF Central consolidated statements via `casparser`; redeemed schemes kept; XIRR; category and fund-house mix |
| Bank | `icici` | savings, PPF and linked fixed deposits; both the emailed monthly statement and the statement requested for any period |
| Credit cards | `icici_card`, `hdfc_card`, `axis_card`, `tata_card` | card arithmetic checked per issuer; due dates, minimum due, credit limit; bill payments linked to the bank debit |
| NPS | `nps` | monthly, annual and portal statements; per-scheme units and NAV; account-level XIRR when the history is complete |

Also built: **hosted mode and PWA** (installable on a phone; password + authenticator-code sign-in, signed `__Host-` session cookie, throttling, activity log, no-store pages, a service worker that caches only static files; see `docs/HOSTING.md` and `SECURITY.md`); transfer matching between own accounts (shared reference, or card payment by amount and date); duplicate-folio and duplicate-transaction detection with backups before any merge; net worth overview (assets only; card bills shown beside it, not subtracted); card spending by month; local-time rendering; Docker image; sample statements for every layout.

---


## 4. Key design decisions (and why)

| Decision | Why |
|---|---|
| Only `finance/ledger.py` writes; others use `connect_ro` (which upgrades an old schema first) | Single writer, auditable. An old DB opened read-only once crashed a page: keep the upgrade in `connect_ro`. |
| Account identity = keyed hash of `(type, institution, folio)` + `account_aliases` | Raw numbers are never stored. Aliases make a merge permanent for future imports. |
| Transaction identity for unit-moving rows ignores the date (`strong_key`) | Statements disagree on dates by days to weeks; the running unit balance + NAV do not change. |
| Destructive actions (merge, cleanup) require a click and write a backup to `data/backups/` | Learned the hard way; real data was inflated by duplicates. |
| Ambiguity goes to Review, never guessed | Same for missing folios, duplicate folios, transfer matches, unparseable PDFs. |
| `ParsedStatement` / `Parser.parse(path, password)` plugin contract; parsers registered in `finance/parsers/base.py` `REGISTRY` | Sender rules name a parser; the sync runner dispatches by name. |
| Secrets: keyring first (service `kosh`), `KOSH_*` env fallback, or the 0600 file store in containers; write-only in the UI | Hard rule. Identity values are only used to build passwords in memory. |
| Typed password candidates live in keyring (`id:extra_passwords`), never in DB | Custom CAS passwords vary per request. |
| Bank/PPF/card rows reuse `transactions` (isin '' and units NULL); identity = keyed hash of (account, date, amount, running balance, occurrence) | Banks print a running balance, which makes a row unique and date-stable between overlapping statements. Cards print none: the occurrence index covers identical rows; expect to need a date tolerance there (lesson 2). |
| A parser must refuse, not guess: the money checks live in `ParsedAccount.integrity_errors()` and the Ledger re-checks | A wrong sign or a dropped row must never reach the ledger; mutation tests cover each gate. |
| Description lines are split between rows by centring each row's text block on its date line (`_distribute`) | "Nearest date line" fails on exact ties (same line pitch inside and between cells); this was found on real data and has a regression test. |
| Statement descriptions are stored with long digit runs masked; UPI/IMPS references are kept apart in `transactions.reference` | Full account numbers appear inside descriptions (transfer legs); references are needed for matching and are not account numbers. |

---

## 5. Codebase map and how to extend it

See the tree in the README. Conventions:

- **Add a parser:** new module in `finance/parsers/`, a class with `name` and `parse(path, password) -> ParsedStatement` (or a sibling statement type such as `ParsedAccountStatement`), `register(...)` it, import it in `finance/sync.py` and `finance/importer.py` (the unused-import `# noqa` lines exist to trigger registration). Add a sender rule (`parser` field) in a mailbox.
- **Add a source:** implement `finance/sources/base.py` `Source` (`fetch_new`, `commit`, `close`); factory is injected into `SyncRunner` (tests use a fake IMAP, see `tests/mailfix.py`).
- **Change the schema:** add `MIGRATIONS[n]` in `finance/db.py`, bump `SCHEMA_VERSION`, add an upgrade test (see `test_ledger.py`). `ALTER TABLE ... ADD COLUMN` only; SQLite statements run through `executescript`.
- **Add a review kind:** create items via `Ledger` (`add_review` or a dedicated method), render in `finance/web/templates/review.html` (title map + buttons), add a route in `finance/web/app.py`, mirror in `finance/cli.py`.
- **Add a UI page:** route in `app.py`, template extending `base.html`, nav entry in `base.html`. `render()` injects `mb` (mailbox labeller); times use the `ts` filter (UTC in the DB, converted to local in the browser).
- **Add a job action** (a long task started from the UI): `finance/actions.py` `ACTIONS` + branch, `finance/jobs.py` `ACTION_INFO`, a route and button that call `jobs.start`.
- **Tests:** fixtures in `tests/conftest.py` (`make_cas`, `make_summary`, in-memory keyring autouse) and `tests/mailfix.py` (encrypted PDFs via pypdf, RFC822 messages, strict fake IMAP that fails on any write command). When adding a guarantee, **mutation-check it** (temporarily break the code and confirm tests fail).

---

## 6. Lessons from real data (do not relearn these)

Real statements behaved differently from the assumptions in the first design. These facts shaped the code and will matter for banks and cards too:

1. **The same account can be spelled differently across statements/sources** (e.g. a folio with and without a trailing `/ 0`, different spacing). Identity from a raw string is fragile; keep aliases and detect lookalikes from shared transactions.
2. **The same transaction can carry different dates in different statements** (order date vs allotment/value date, up to weeks apart). Never put the date in a transaction's identity when a running balance is available. Expect the same for bank/card statements (transaction date vs posting date).
3. **Summary statements exist** and name no institution (`N/A`) and have no transactions.
4. **Consolidated statements list only non-zero folios**, so absence means "maybe sold", not "unknown". A folio can be fully sold between two statements.
5. **Statement passwords can be chosen per request** (not derivable from a pattern); several statements may share one.
6. **A unit-balance check is a strong integrity test**: transactions should sum to the current units. Use the analogous check for money (opening + sum of movements = closing) for banks/cards and as the acceptance gate for any LLM-assisted parse.
7. Mail arrives as PDF attachments; some senders may send links instead (not handled).
8. Financial data is sensitive: experiments run on a copy of the database, and any destructive step needs a backup and a click.
9. **ICICI statements are one PDF for several products**: savings + PPF transaction tables, a linked fixed-deposit table, and (for other customers) possibly recurring deposits. Sections repeat their header on every page; each page's table ends with a `Total:` row (deposits, withdrawals, closing balance), which is a free integrity check.
10. **Direction is not in the text**: only the column (x position) tells a deposit from a withdrawal. Cross-check it against the balance change.
11. **Descriptions wrap above and below the date line** and a date can be glued to the mode text (`19-10-2026CMS`); an account's own number can appear inside descriptions of transfer legs.
12. **A transfer between two own accounts carries the same `SR…` id on both legs** (savings -> PPF), a free, exact match. Card bills are paid through **CRED** (`UPI/CRED Club/…`), so the bank side of a card payment reads "CRED", not the bank's name, and one CRED payment can fund any card.
13. The statement's own summary page repeats each account's closing balance: a final cross-check (a mismatch is a warning, since a statement period can end before the summary date).
14. **A card statement carries its own arithmetic** (previous dues, payments, purchases, charges = total due), which is a second gate beside rows-explain-dues. The formula differs per issuer (ICICI: prev + purchases + cash − payments; HDFC: prev − payments + purchases + finance; Axis: prev − payments − credits + purchases + cash + other, every amount with Dr/Cr).
15. **Direction markers differ**: ICICI a `CR` word after credits, HDFC a `+` before the amount, Axis `Dr`/`Cr` on every amount. The rupee sign is a backtick (ICICI), the letter `C` (HDFC, glued or separate) or absent (Axis). Credit limits can lack paise (`C1,00,000`).
16. **Table columns move between pages and variants** (ICICI page 1 is pushed right by a rewards panel; HDFC RuPay adds a reward-points column; Axis headers are centred over their columns, so values start left of the header). Take column positions from each page's header row, never from constants; keep the table end explicit (footnotes, `End of Statement`).
17. **Transaction dates can precede the statement period** by a day (posting vs transaction date); do not reject rows outside the period. Payment due date on ICICI cannot be labelled from text (the left-panel labels are not readable), so it is the only date after the period; if several qualify it is left empty with a warning, not guessed.
18. **The ICICI statement you request from net banking** (a period such as a financial year) shares the tables of the emailed one but differs: the heading reads `Statement of Transactions in [Savings] Account Number: <id> in INR for the period …` (the type word can be missing; the account is then skipped with a warning), the table header repeats on every page, a section ends with one `TOTAL` row, columns sit at other x positions (read from the header row, 30 pt tolerance), and each page opens with its own lines (page counter, customer name) that must never reach a description. Row order across a page break is kept with a per-page offset on line positions. The Import page shows the parser's own error when a type has one parser.
19. **One bank account, several spellings**: a monthly email masks the number (`XXXXXXXX8892`) while a requested statement prints it whole, so the exact-text hash made two accounts and two balances. Bank and PPF accounts now match on institution + last 4 (`SAME_MASK_TYPES` in `ledger.py`); the new spelling is stored as an alias, and `merge_same_account_spellings()` (run after every import) folds any existing split, moving balances, summaries, linked FDs and re-keying moved rows so later overlapping statements still dedupe. Cards are not matched this way: two cards of one issuer can share a last 4.
20. **Net worth is the assets only.** Card bills are shown beside it (hero note and tile), never subtracted: without the next statement nobody knows whether a bill is already paid. A bill drops off once its due date passes.
21. **Times are stored in UTC and rendered by the browser** (`<time class="ts">` + the script in `base.html`, which uses the viewer's own locale and timezone). Never print a raw timestamp in a template: pipe it through `|ts`.

---

## 7. Open work

Everything below is unclaimed. Items are roughly ordered by how many people they would help. Tags: **[parser]** a new statement layout, **[feature]** product behaviour, **[infra]** engineering. "Easy" means a copy of an existing parser with a new synthetic fixture.

### 7.1 Credit cards

- **[parser] More issuers** (easy per issuer, one parser each): SBI Card, Kotak, IndusInd, Yes Bank, RBL, AU Small Finance, IDFC First, Standard Chartered, American Express, Federal / OneCard / Jupiter / Scapia / Slice (co-branded), Bank of Baroda (BoB Card), PNB, Canara, HSBC.
- **[parser] More layouts of the issuers we have**: premium and co-branded variants (HDFC Regalia / Diners / Millennia / RuPay, Axis Magnus / Flipkart, ICICI Amazon Pay / Coral), and the statement you request from net banking for a custom period, as already exists for bank statements.
- **[feature] EMI on cards.** Purchases converted to EMI, no-cost EMI, loan-on-card and balance transfers: parse the instalment rows into principal, interest, GST and processing fee; show the outstanding principal and remaining instalments; keep an EMI from being counted as a fresh purchase every month.
- **[feature] Several cards in one statement** and **add-on cards** (a primary and supplementary cards on one PDF), with per-card spend.
- **[feature] Foreign-currency transactions**: original currency and amount, forex mark-up, GST on it.
- **[feature] Reward points ledger** (earned, redeemed, expiring) and cashback.
- **[feature] Fees and interest as their own kinds**: annual fee, late fee, finance charge, GST, fuel surcharge waiver, cash-advance fee; show interest paid per year.
- **[feature] Refund and reversal matching** back to the original purchase.
- **[feature] Credit utilisation** (limit vs outstanding) over time, minimum-due vs total-due tracking, overdue and part-payment flags, autopay detection.
- **[feature] Due-date alerts** (see 7.7).

### 7.2 Bank accounts

- **[parser] Nationalised banks**: State Bank of India, Punjab National Bank, Bank of Baroda, Canara, Union Bank, Bank of India, Indian Bank, Central Bank of India, UCO, Indian Overseas Bank, Bank of Maharashtra, Punjab & Sind; and **India Post** (Post Office savings, IPPB).
- **[parser] Private and small-finance banks**: HDFC, Axis, Kotak, IndusInd, Yes, IDFC First, Federal, RBL, AU, Equitas, Ujjivan, Jana, and cooperative banks.
- **[parser] Other file formats**: net-banking downloads as CSV / Excel (XLS, XLSX), MT940, and OFX/QIF, which are far easier to parse reliably than PDF. A generic "map the columns" CSV import would cover many banks at once.
- **[parser] Scanned statements**: OCR fallback (Tesseract) for image-only PDFs, gated by the same balance checks.
- **[parser] Recurring deposits.** The ICICI statement can list an account that names no type (a recurring-deposit-shaped table: fixed monthly deposits, then interest). It is skipped with a warning today. Support RD accounts (and RD on other banks) as balance snapshots plus instalments.
- **[parser] Fixed deposits from other banks** (maturity date, rate, interest payout, TDS), auto-sweep and linked FDs, maturity reminders.
- **[feature] Transaction types beyond debit and credit.** Today a row is only `DEBIT` or `CREDIT` plus a free-text description. Derive and store structured fields: channel (UPI, NEFT, RTGS, IMPS, ACH / NACH mandate, cheque, ATM, POS, standing instruction, cash deposit), counterparty (UPI VPA, merchant, payee), and nature (salary, interest credit, charges and GST, TDS on interest, dividend, refund and reversal, EMI debit, SIP debit, rent, tax payment, forex remittance).
- **[feature] Categories.** Rule-based categorisation (merchant to category) with user-editable rules and manual overrides that are remembered; merchant-name normalisation; tags and notes; split a transaction across categories.
- **[feature] Recurring-payment detection**: salary, subscriptions, SIPs, EMIs, rent; "upcoming debits" and a monthly commitments view.
- **[feature] Account variety**: joint accounts and multiple holders, overdraft and negative balances, NRE / NRO / FCNR accounts in foreign currency, salary and zero-balance accounts, closed accounts.
- **[feature] Interest summary for tax** (savings and FD interest per financial year, TDS deducted).
- **[feature] Financial-year view** and custom periods across all pages.

### 7.3 Loans and liabilities

- **[parser/feature] Loan accounts**: home, car, personal and education loans; statement or amortisation schedule; principal / interest split, outstanding principal, prepayments, next EMI date; loans as liabilities (net worth stays assets-only today, so they would be shown beside it like card bills).
- Buy-now-pay-later and credit-line products.

### 7.4 Other assets

- **[parser/feature] EPF**: EPFO passbook (UAN) with employee, employer and pension splits and interest credits. The Import page lists EPF but says "not supported yet".
- **[parser] NPS Tier II and Atal Pension Yojana.** The NPS parser reads a Tier II header (and a synthetic test covers it) but has only ever been run on real Tier I statements, so Tier II is unverified.
- **[parser/feature] PPF at other banks and the Post Office**, Sukanya Samriddhi, SCSS, NSC / KVP, Atal Pension Yojana, RBI bonds, Sovereign Gold Bonds.
- **[parser] Stocks and demat**: NSDL / CDSL consolidated statements (currently rejected as unsupported), broker holdings and tradebooks (Zerodha, Groww, Upstox, ICICI Direct, HDFC Securities), contract notes; corporate actions (splits, bonus), dividends.
- **[feature] Capital gains**: FIFO lots for stocks and mutual funds, short- and long-term, the post-2018 grandfathering rules, equity vs debt fund rules, ELSS lock-in, indexation where it still applies. Pairs with the tax view in 7.7.
- **[feature] Mutual-fund extras**: SIP tracking and missed-SIP detection, IDCW and dividend rows, stamp duty, NAV refresh from AMFI so values are current rather than as of the statement, goal mapping.
- **[feature] Manual assets**: real estate, vehicles, gold (physical, ETF, digital), jewellery, private loans given, employer ESOP / RSU, foreign stocks and crypto, with a simple form and an as-of date.
- **[feature] Insurance**: term, health, ULIP and endowment policies, premium due dates and surrender value.
- **[feature] Corporate FDs, NCDs, bonds and P2P** holdings.
- **[feature] Multi-currency** assets with a stored conversion rate and date.

### 7.5 Ingestion

- **[infra] Account Aggregator** (the RBI framework, via an FIU partner) as a statement-free source for banks, deposits, mutual funds and NPS. Large, but removes the parsers' hardest problem.
- **[infra] Gmail API / Microsoft Graph with OAuth** (no app password), and a generic folder-watch source for downloaded files.
- **[infra] `.eml` / `.mbox` import** for statements saved from a mail client.
- **[feature] Link-only emails**: statements delivered as a download link are not fetched; add a Review entry or a fetcher.
- **[feature] Alert emails** (transaction SMS-style mails) as provisional rows reconciled against the statement row so they never duplicate. Treat email content as untrusted.
- **[feature] LLM fallback for unrecognised layouts**: local model only, JSON-schema validated, accepted only if it passes the integrity gate, shown in Review for confirmation the first times.
- **[feature] Manual transfer link / unlink** in the UI; an "unmatched transfers" view.
- **[feature] Scheduled sync** (`launchd`, cron, or a loop in the container) with a "last successful sync is old" warning; retries with backoff for transient network errors only, and a run history page.

### 7.6 Insight and reports

- **[feature] Net-worth history**: the overview is a point in time. Store a monthly snapshot and chart it, with allocation drift.
- **[feature] Cash flow and budgets** by category and month, savings rate, top merchants, month-on-month comparison.
- **[feature] Reports and analysis**: returns against benchmarks, fund scoring, rebalancing and withdrawal-plan tools, built on the ledger.
- **[feature] Goals**: map assets to goals (retirement, house, education) with a target and a projected date.
- **[feature] Ask**: natural-language questions answered from **read-only tools** over the ledger (holdings, transaction search, net worth, XIRR, upcoming dues, allocation), also exposed as an MCP server. Every number in an answer must come from a tool result; descriptions and email text are untrusted input (prompt injection).
- **[feature] Export**: CSV / Excel / JSON of any table; a full backup and restore from the UI.

### 7.7 Alerts and tax

- Notifications for card due dates, FD maturities, SIP failures, large debits and low balances (email, desktop, push).
- Tax helper: income from interest and dividends, capital gains statement, 80C / 80D tracker, advance-tax estimate, a schedule-FA style foreign-asset list.

### 7.8 UI and product

- Sorting, filtering and pagination on every table (Transactions is capped at 200 rows), column chooser, saved filters.
- Edit and annotate transactions; bulk actions.
- **Multiple people**: one identity per install today; support family members with separate ledgers and a combined view.
- Hindi, Telugu and other languages; accessibility audit; phone-width layouts; dark-mode review of every chart.
- First-run onboarding that walks through Identity, Mailboxes and a first import; a demo mode that loads `samples/`.
- Optional dev auto-reload for the UI.

### 7.9 Security and privacy hardening

Findings from the pre-release review that are not fixed yet:
- **Decrypt cost.** A `{card4}` template tries up to 10,000 passwords, each a full PDF parse. Parse the encryption dictionary once and test against it; add a timeout to sync jobs so one stuck job cannot block the next.
- **Authentication-Results provenance.** Only the topmost header is trusted today. Also check its authserv-id against the mail provider, and treat custom IMAP hosts that add no header explicitly.
- Remove the always-allowed `testserver` Host from production code (inject it from tests).
- Store the ledger hash key in the keyring instead of `data/.hash_key`.
- **Passkeys (WebAuthn)** as the primary phone sign-in (Face ID / fingerprint, phishing-resistant), after openGym's design: discoverable credentials, one-time enrollment code from the CLI (`add-passkey`), a fresh sign-in required to add or remove one, a signature-counter check, and refusal to remove the last way in. Needs the `webauthn` package. Password + code stays as the recovery path.
- Optional encryption of the database and the secrets file at rest (SQLCipher / an encrypted secret store), so a copied volume is useless without a key held elsewhere.
- A nonce-based Content-Security-Policy (the templates still use inline scripts and styles, so `'unsafe-inline'` is allowed today).
- A `SECURITY.md` and a threat model; Content-Security-Policy headers; secret scanning in CI.
- Pin the Docker base image by digest and the GitHub Actions by commit SHA; hash-locked requirements.

### 7.10 Engineering

- CI on every push: tests on Python 3.10 to 3.14, lint, type checking, dependency audit, secret scan.
- Dependabot, release notes, a changelog and semantic versions; publish to PyPI so `pipx install kosh` works.
- Windows support (keyring, paths, line endings) and a `docker compose` profile for a scheduled sync.
- Parser robustness: fuzz and property tests, a corpus of synthetic layouts per issuer, a "verified on a real statement" column in the support matrix.
- Issue and PR templates that ask for a layout description instead of a real file.
- Optional auto-merge of duplicate folios when the evidence is overwhelming (same fund house and many identical transactions); always manual today.
- Documents page hint for old rows recorded before email details were captured.
- Rewrite the deprecated `starlette.testclient`/`httpx` usage once the upstream warning is resolved.


---

## 8. Environment gotchas (save time)

- `pdfplumber` is now a runtime dependency (bank statements); pdfminer (via casparser) still checks PDF passwords.
- Bumping `SCHEMA_VERSION` upgrades `data/finance.db` the first time any code opens it (`connect_ro`/`connect_rw`), additive only. Backups are written to `data/backups/` before destructive actions.
- **Use `venv/bin/python -m pip ...`**. `venv/bin/pip` belongs to a different Python and silently installs into the wrong place. Development used Python 3.14.
- macOS `sed -i` needs `sed -i '' ...`; in zsh an unmatched glob aborts the whole command (`rm -f path*` with no matches), so quote or avoid it.
- **Restart `python main.py ui` after any Python change.** Templates reload; code does not (a stale server once raised `no attribute xirr_funds`).
- The Host check expects the configured `UI_PORT`; when running a throwaway server on another port, set `UI_PORT` to match.
- Tests must never touch the real Keychain or `data/` (autouse fixture); if you write new tests, build on `db_path`, `tmp_path`, and the fake IMAP.
- Using the stdlib `imaplib`, `search` criteria strings are built from validated sender strings only; keep that validation (it prevents IMAP search injection).
- pdfminer (via casparser) is what checks PDF passwords; `pypdf` is a dev dependency used only to create encrypted test PDFs.
- The test count is a quick health signal: `python -m pytest -q` should end all green.
