"""ICICI Bank account statements (savings / current, PPF, linked fixed deposits): the combined statement that arrives by
email every month, and the "Statement of Transactions" you request yourself (a financial year or any period).

Deterministic: pdfplumber word positions, no LLM. The layout is a set of tables whose columns sit at fixed
x positions; each row's text is centred on its date line, so wrapped description lines are given to the nearest
date line. Direction (money in or out) comes from the DEPOSITS / WITHDRAWALS column and is then checked against
the printed running balance, so a row can never be imported with the wrong sign. Three more checks refuse the
whole statement instead of importing something wrong: the balance chain, each page's printed totals, and
opening + movements = closing.

The two layouts differ in how a table ends: the combined statement closes every page's table with a `Total:` row;
the requested one repeats its header on each page and closes a whole section with one `TOTAL` row. Page headers and
footers that repeat inside a section are ignored. Sections this statement can carry but which are not parsed yet (recurring deposits, other account types) are
reported as warnings, never silently skipped.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from finance.models import GENERIC_ACCOUNT_LABEL, AccountTxn, ParsedAccount, ParsedAccountStatement, ParsedDeposit
from finance.parsers.base import register
from finance.parsers.pdftext import Word, group_lines as _lines, line_text as _text, read_pages
from finance.redact import redact

INSTITUTION = "ICICI Bank"
ACCOUNT_TYPES = {"savings": "bank", "current": "bank", "salary": "bank", "ppf": "ppf"}

_DATE = re.compile(r"^(\d{2}-\d{2}-\d{4})(.*)$")
_AMOUNT = re.compile(r"^\d[\d,]*\.\d{2}$")
_SECTION = re.compile(r"^Statement of Transactions in (?:(?P<label>.+?) )?Account (?:Number: )?(?P<id>\S+) in INR for the period "
                      r"(?P<from>[A-Za-z]+ \d{1,2}, \d{4}) - (?P<to>[A-Za-z]+ \d{1,2}, \d{4})$")
_SUMMARY_ROW = re.compile(r"^(?P<label>[A-Za-z][A-Za-z ]*?) A/c (?P<id>\S+) (?P<bal>\d[\d,]*\.\d{2})"
                          r"(?: (?P<fd>\d[\d,]*\.\d{2}) (?P<total>\d[\d,]*\.\d{2}))?\b")
_AS_ON = re.compile(r"Statement summary for Customer ID \S+ as on (?P<d>[A-Za-z]+ \d{1,2}, \d{4})")
_FD_LINKED = re.compile(r"DEPOSITS LINKED TO ACCOUNT No\. (?P<id>\S+)")
_FD_ROW = re.compile(
    r"^(?P<num>\d{6,}) (?P<open>\d{2}-\d{2}-\d{4}) (?P<principal>\d[\d,]*\.\d{2}) (?P<rate>\d+(?:\.\d+)?) "
    r"(?P<tenure>\d+ (?:Yrs?|Years?|Mths?|Months?|Days?)(?: \d+ (?:Yrs?|Years?|Mths?|Months?|Days?))*) "
    r"(?P<maturity_amt>\d[\d,]*\.\d{2}) (?P<maturity>\d{2}-\d{2}-\d{4}) "
    r"(?P<balance>\d[\d,]*\.\d{2}) (?P<status>[A-Za-z][A-Za-z /&\-]*)$")
_FD_LOOKS_LIKE_ONE = re.compile(r"^(?P<num>\d{6,}) \d{2}-\d{2}-\d{4} \d[\d,]*\.\d{2}\b")       # starts like a deposit row
_SR = re.compile(r"(?<![A-Za-z])SR\d{6,}")
_RRN = re.compile(r"/(\d{12})(?:/|$|\s)")

AMOUNT_X = 360          # amounts sit right of this; description text never does
MODE_X = 143            # MODE column is left of the PARTICULARS column
COL_TOLERANCE = 30      # how far an amount's right edge may sit from its column header's right edge (columns are 50+ apart)
PAGE_SHIFT = 1000.0     # added to a line's position per page, so rows keep their order across a page break


def _num(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _day(text: str) -> date:
    return datetime.strptime(text, "%d-%m-%Y").date()


def _long_day(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def _reference(text: str) -> str:
    """A transaction reference (not an account number): the transfer id a bank prints on both legs of an
    own-account transfer, else the 12-digit UPI/IMPS reference."""
    if (m := _SR.search(text)):
        return m.group(0)
    if (m := _RRN.search(text)):
        return m.group(1)
    return ""


def _distribute(rows: list[dict], extra: list[tuple[float, str]]) -> list[list[tuple[float, str]]]:
    """Give each wrapped description line to its row. A row's text block is vertically centred on its date line
    and lines are evenly spaced inside and between rows, so 'nearest date line' mis-assigns lines that sit midway.
    Instead, the lines between two date lines are split where the upper row's block is best centred."""
    out: list[list[tuple[float, str]]] = [[] for _ in rows]
    if not rows:
        return out
    remaining = sorted(extra)
    k = sum(1 for t, _ in remaining if t < rows[0]["top"])
    heads, remaining = remaining[:k], remaining[k:]
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["top"] if i + 1 < len(rows) else float("inf")
        n = sum(1 for t, _ in remaining if t < nxt)
        gap, remaining = remaining[:n], remaining[n:]
        own = [(r["top"], r["particulars"])] if r["particulars"] else []

        def off_centre(split: int) -> float:
            tops = [t for t, _ in heads + own + gap[:split]]
            return abs((min(tops) + max(tops)) / 2 - r["top"]) if tops else float("inf")

        split = len(gap) if nxt == float("inf") else min(range(len(gap) + 1), key=off_centre)
        out[i] = sorted(heads + own + gap[:split])
        heads = gap[split:]
    return out


class _Section:
    def __init__(self, label: str, identifier: str, period_from: date, period_to: date):
        self.label, self.identifier = label, identifier
        self.period_from, self.period_to = period_from, period_to
        self.type = ACCOUNT_TYPES.get(label.strip().lower())      # None for a kind this version has no name for: resolved at the end
        self.generic = False                                       # true when the kind is not one we know, so a failed check skips it
        self.failure = ""                                          # why an account of an unknown kind could not be read
        self.opening: Decimal | None = None
        self.last: Decimal | None = None
        self.txns: list[AccountTxn] = []


class _Parser:
    def __init__(self) -> None:
        self.sections: dict[str, _Section] = {}
        self.cur: _Section | None = None
        self.cols: dict[str, float] | None = None
        self.chunk: list[list[Word]] | None = None
        self.summary: dict[str, Decimal] = {}
        self.as_of: date | None = None
        self.fd_linked = ""
        self.fd_rows: list[tuple[str, re.Match]] = []
        self.summary_fd: dict[str, Decimal] = {}            # per account: the linked-FD balance the summary page prints
        self.fd_unreadable = 0                              # deposit-looking rows that could not be read
        self.summary_label: dict[str, str] = {}             # account number -> the kind the summary page lists it as
        self.recurring_seen = False
        self.warnings: list[str] = []
        self.page = 0
        self.skip_page_top = False        # inside a section at the start of a page: ignore lines until the header repeats

    # ── feeding lines ─────────────────────────────────────────────────────────

    def new_page(self) -> None:
        self.page += 1
        self.skip_page_top = self.chunk is not None        # a section runs on: what comes first is the page's own header

    def feed(self, line: list[Word]) -> None:
        text = _text(line)
        if self.chunk is not None:
            if self.skip_page_top:
                if self._is_table_header(line):            # the table's header repeats at the top of each page
                    self._read_columns(line)
                    self.skip_page_top = False
                return
            if line[0].text in ("Total:", "TOTAL"):
                self._close_chunk(line)
            elif _SECTION.match(text) or text.startswith("DATE MODE PARTICULARS"):
                raise ValueError("a transaction table ended without its Total row; layout not recognised")
            else:
                self.chunk.append([Word(w.x0, w.x1, w.top + self.page * PAGE_SHIFT, w.text) for w in line])
            return
        m = _SECTION.match(text)
        if m:
            self._start_section(m)
            return
        if self.cur and self._is_table_header(line):
            self._read_columns(line)
            self.chunk = []
            return
        self._summary_line(text)

    @staticmethod
    def _is_table_header(line: list[Word]) -> bool:
        return line[0].text == "DATE" and "PARTICULARS" in {w.text for w in line}

    def _read_columns(self, line: list[Word]) -> None:
        right = {w.text: w.x1 for w in line}
        try:
            self.cols = {"dep": right["DEPOSITS"], "wd": right["WITHDRAWALS"], "bal": right["BALANCE"]}
        except KeyError:
            raise ValueError("transaction table header has no DEPOSITS / WITHDRAWALS / BALANCE columns")

    def _start_section(self, m: re.Match) -> None:
        ident, label = m.group("id"), m.group("label") or ""
        if ident not in self.sections:
            self.sections[ident] = _Section(label, ident, _long_day(m.group("from")), _long_day(m.group("to")))
        self.cur = self.sections[ident]

    def _summary_line(self, text: str) -> None:
        if (m := _AS_ON.search(text)):
            self.as_of = _long_day(m.group("d"))
        if (m := _SUMMARY_ROW.match(text)):
            self.summary[m.group("id")] = _num(m.group("bal"))
            self.summary_label[m.group("id")] = m.group("label").strip()
            if m.group("fd"):
                self.summary_fd[m.group("id")] = _num(m.group("fd"))
        if (m := _FD_LINKED.search(text)):
            self.fd_linked = m.group("id")
        if (m := _FD_ROW.match(text)):
            self.fd_rows.append((self.fd_linked, m))
        elif (m := _FD_LOOKS_LIKE_ONE.match(text)):         # never skip a deposit silently
            self.fd_unreadable += 1
            self.warnings.append(f"A fixed deposit row (…{m.group('num')[-4:]}) is in a layout this version cannot read, so that "
                                 f"deposit was not imported.")
        if re.search(r"RECURRING DEPOSIT", text, re.I):
            self.recurring_seen = True

    # ── one table (the rows between a header and its Total row) ───────────────

    def _col(self, w: Word) -> str:
        name, edge = min(self.cols.items(), key=lambda kv: abs(w.x1 - kv[1]))
        if abs(w.x1 - edge) > COL_TOLERANCE:
            raise ValueError(f"amount {w.text} does not sit under any amount column")
        return name

    def _close_chunk(self, total_line: list[Word]) -> None:
        sec, lines = self.cur, self.chunk
        self.chunk = None
        totals = {}
        for w in total_line[1:]:
            if _AMOUNT.match(w.text):
                totals[self._col(w)] = _num(w.text)
        if set(totals) != {"dep", "wd", "bal"}:
            raise ValueError("Total row is missing an amount")

        anchors, extra = [], []
        for line in lines:
            m = _DATE.match(line[0].text) if line[0].x0 < 60 else None
            (anchors if m else extra).append(line)
        rows = [self._anchor(line) for line in anchors]
        pieces = _distribute(rows, [(line[0].top, _text(line)) for line in extra])

        if sec.failure:                                  # an account of an unknown kind that already failed: nothing more to read
            return
        try:
            dep_sum = wd_sum = Decimal(0)
            for r, parts in zip(rows, pieces):
                particulars = re.sub(r"\s+", " ", " ".join(t for _, t in sorted(parts))).strip()
                if particulars == "B/F":
                    if sec.opening is None:
                        sec.opening, sec.last = r["bal"], r["bal"]
                    continue
                if (r["dep"] is None) == (r["wd"] is None) or r["bal"] is None:
                    raise ValueError(f"row dated {r['date']}: expected one amount in DEPOSITS or WITHDRAWALS and a balance")
                if sec.last is None:
                    raise ValueError(f"row dated {r['date']} comes before the opening balance")
                amount = r["dep"] if r["dep"] is not None else -r["wd"]
                if sec.last + amount != r["bal"]:
                    raise ValueError(f"row dated {r['date']}: {sec.last} {'+' if amount >= 0 else '-'} {abs(amount)} "
                                     f"is not the printed balance {r['bal']}")
                dep_sum += r["dep"] or 0
                wd_sum += r["wd"] or 0
                sec.last = r["bal"]
                full = re.sub(r"\s+", " ", f"{r['mode']} {particulars}").strip()
                sec.txns.append(AccountTxn(
                    date=r["date"], description=redact(full, limit=300), amount=amount, balance=r["bal"],
                    kind="CREDIT" if amount > 0 else "DEBIT", mode=r["mode"], reference=_reference(full)))

            if (dep_sum, wd_sum) != (totals["dep"], totals["wd"]) or sec.last != totals["bal"]:
                raise ValueError(f"page totals do not match the rows read (deposits {dep_sum} vs {totals['dep']}, "
                                 f"withdrawals {wd_sum} vs {totals['wd']}, balance {sec.last} vs {totals['bal']})")
        except ValueError as exc:
            if sec.type is not None:                     # a kind we know must add up, or the whole statement is refused
                raise
            sec.failure = str(exc)                       # an unknown kind is left out with a warning and cannot block the others

    def _anchor(self, line: list[Word]) -> dict:
        m = _DATE.match(line[0].text)
        mode_words = [m.group(2)] if m.group(2) else []
        particulars: list[str] = []
        amounts: dict[str, Decimal] = {}
        for w in line[1:]:
            if _AMOUNT.match(w.text) and w.x1 > AMOUNT_X:
                amounts[self._col(w)] = _num(w.text)
            elif w.x0 < MODE_X:
                mode_words.append(w.text)
            else:
                particulars.append(w.text)
        return {"date": _day(m.group(1)), "top": line[0].top, "mode": " ".join(mode_words),
                "particulars": " ".join(particulars), "dep": amounts.get("dep"), "wd": amounts.get("wd"),
                "bal": amounts.get("bal")}

    # ── result ────────────────────────────────────────────────────────────────

    def _resolve_kinds(self) -> None:
        """Decide what every section is. A statement lists whatever accounts you hold, so nothing here is limited to the kinds
        we have seen: a section whose heading (or the summary page) names a kind we know is that kind; one that names a kind
        we do not know (an overdraft, anything new) is read exactly the same way and kept as its own account under that name;
        one that names nothing is recognised as the PPF when the savings account's `TO PPF` rows share their SR transfer id with
        its rows, and otherwise kept as a generic account. Every one of them must still pass the balance checks."""
        ppf_legs = {t.reference for s in self.sections.values() if s.type == "bank" for t in s.txns
                    if t.reference.startswith("SR") and re.search(r"\bPPF\b", t.description, re.I)}
        for sec in self.sections.values():
            if sec.type is not None:
                continue
            label = (sec.label or self.summary_label.get(sec.identifier, "")).strip()
            if label and label.lower() in ACCOUNT_TYPES:
                sec.label, sec.type = label, ACCOUNT_TYPES[label.lower()]
            elif label:
                sec.label, sec.type, sec.generic = label.title(), "bank", True
            elif any(t.reference in ppf_legs for t in sec.txns):
                sec.label, sec.type = "PPF", "ppf"
            else:
                sec.label, sec.type, sec.generic = GENERIC_ACCOUNT_LABEL, "bank", True

    def result(self) -> ParsedAccountStatement:
        if self.chunk is not None:
            raise ValueError("a transaction table ended without its Total row; layout not recognised")
        self._resolve_kinds()
        accounts: list[ParsedAccount] = []
        for sec in self.sections.values():
            if sec.failure:                                # (only ever set for a kind we did not know when reading its rows)
                self.warnings.append(f"{sec.label} account …{sec.identifier[-4:]} was left out: {sec.failure}")
                continue
            if sec.opening is None:
                if sec.generic:
                    self.warnings.append(f"{sec.label} account …{sec.identifier[-4:]} was left out: no opening balance row was found.")
                    continue
                raise ValueError(f"{sec.label} account …{sec.identifier[-4:]}: no opening balance (B/F) row found")
            acct = ParsedAccount(type=sec.type, label=sec.label, institution=INSTITUTION, identifier=sec.identifier,
                                 period_from=sec.period_from, period_to=sec.period_to, opening_balance=sec.opening,
                                 closing_balance=sec.last, txns=sec.txns)
            errs = acct.integrity_errors()
            if errs and sec.generic:                       # an account of a kind we do not know must not block the ones we do
                self.warnings.append(f"{sec.label} account …{sec.identifier[-4:]} was left out: {errs[0]}")
                continue
            if errs:
                raise ValueError(f"{sec.label} account …{sec.identifier[-4:]}: {errs[0]}")
            if sec.label == GENERIC_ACCOUNT_LABEL:
                self.warnings.append(f"An account …{sec.identifier[-4:]} whose type the statement does not name was imported as a separate "
                                     f"“{GENERIC_ACCOUNT_LABEL}”: its balance matches its transactions, but nothing says what kind it is.")
            if sec.identifier in self.summary and self.summary[sec.identifier] != acct.closing_balance:
                self.warnings.append(f"{sec.label} account …{sec.identifier[-4:]}: the summary page shows "
                                     f"{self.summary[sec.identifier]} but the transactions end at {acct.closing_balance}.")
            accounts.append(acct)
        if not accounts:
            raise ValueError("no account transactions found; is this an ICICI account statement?")

        if self.recurring_seen and not any("ecurring" in a.label for a in accounts):
            self.warnings.append("Recurring deposits appear on this statement but are not imported yet.")
        as_of = self.as_of or max(a.period_to for a in accounts)
        deposits = [ParsedDeposit(
            number=m.group("num"), institution=INSTITUTION, linked_account=linked, open_date=_day(m.group("open")),
            principal=_num(m.group("principal")), rate=Decimal(m.group("rate")), tenure=m.group("tenure"),
            maturity_date=_day(m.group("maturity")), maturity_amount=_num(m.group("maturity_amt")),
            current_balance=_num(m.group("balance")), status=m.group("status").strip(), as_of=as_of)
            for linked, m in self.fd_rows]
        return ParsedAccountStatement(
            parser=IciciStatementParser.name,
            period_from=min(a.period_from for a in accounts), period_to=max(a.period_to for a in accounts),
            accounts=accounts, deposits=deposits, warnings=self.warnings, as_of=as_of,
            fd_summary={k: v for k, v in self.summary_fd.items() if any(a.identifier == k for a in accounts)},
            fd_complete=self.fd_unreadable == 0)


def parse_pages(pages: list[list[Word]]) -> ParsedAccountStatement:
    p = _Parser()
    for words in pages:
        p.new_page()
        for line in _lines(words):
            p.feed(line)
    return p.result()


class IciciStatementParser:
    name = "icici"

    def parse(self, path: Path, password: str | None) -> ParsedAccountStatement:
        return parse_pages(read_pages(path, password))


register(IciciStatementParser())
