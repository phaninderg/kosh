"""Synthetic bank-statement PDFs for parser tests: a tiny PDF writer plus a builder that lays text out with the
same geometry as an ICICI combined statement (date column, centred multi-line cells, right-aligned amounts,
per-page Total rows, linked fixed deposits). No real data."""

from __future__ import annotations

import io
from decimal import Decimal

H = 8.5                          # line pitch inside and between cells
DEP, WD, BAL = 407.0, 486.0, 559.0   # right edges of the amount columns
PAGE_LIMIT = 760.0


def inr(v: Decimal | int | str) -> str:
    """Indian digit grouping: 1,00,000.00"""
    s = f"{abs(Decimal(v)):.2f}"
    whole, frac = s.split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:]); head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail]) + "." + frac


def _width(text: str, size: float = 8) -> float:
    return sum(0.556 if c.isdigit() else 0.278 if c in ",." else 0.6 for c in text) * size


def build_pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    """pages: [[(x, top, text)]] -> PDF bytes (Helvetica 8pt, A4)."""
    objs: list[bytes] = []

    def add(body: str | bytes) -> int:
        objs.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objs)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("")                                                    # pages: filled in below
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for items in pages:
        ops = ["BT /F1 8 Tf"]
        last_x = last_y = 0.0
        for x, top, text in items:
            y = 842 - top - 6
            ops.append(f"{x - last_x:.2f} {y - last_y:.2f} Td ({text.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj")
            last_x, last_y = x, y
        ops.append("ET")
        stream = "\n".join(ops)
        c = add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Contents {c} 0 R "
                        f"/Resources << /Font << /F1 3 0 R >> >> >>"))
    objs[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def encrypt(pdf: bytes, password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(pdf)))
    w.encrypt(password, algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def row(date: str, amount: str | int, *, head=(), tail=(), own="", mode="", glue=False, bal=None, col=None) -> dict:
    """A statement row. `amount` is signed (negative = withdrawal). `head`/`tail` are wrapped description lines
    above/below the date line; `own` is text printed on the date line itself. `bal` / `col` corrupt the printed
    balance / amount column (for tamper tests)."""
    return dict(date=date, amount=Decimal(str(amount)), head=list(head), tail=list(tail), own=own, mode=mode,
                glue=glue, bal=bal, col=col)


def section(label: str, ident: str, opening: str, rows: list[dict], period=("October 01, 2026", "October 31, 2026"),
            total_bal=None, summary_label: str | None = None) -> dict:
    """`summary_label` is the kind the summary page lists the account as, when the section heading itself names none."""
    return dict(label=label, ident=ident, opening=Decimal(opening), rows=rows, period=period, total_bal=total_bal,
                summary_label=summary_label)


def icici_statement(sections: list[dict], *, fds: list[dict] | None = None, fd_column: dict[str, str] | None = None, rows_per_page: int = 99,
                    total_dep_error: str | None = None) -> bytes:
    """Lay out sections one after another, breaking pages (with a Total row and a repeated header) every
    `rows_per_page` rows or when the page is full."""
    pages: list[list[tuple[float, float, str]]] = [[]]
    state = {"y": 60.0}

    def put(x, text, y=None):
        pages[-1].append((x, state["y"] if y is None else y, text))

    def put_right(edge, text, y=None):
        put(edge - _width(text), text, y)

    def newline(n=1.0):
        state["y"] += H * n

    put(37, "Please find below the Statement summary for Customer ID XXXXX0001 as on October 31, 2026.")
    newline(2)
    put(37, "ACCOUNT DETAILS - INR"); newline()
    closing = {}
    for s in sections:
        bal = s["opening"]
        for r in s["rows"]:
            bal += r["amount"]
        closing[s["ident"]] = bal
        fd = (fd_column or {}).get(s["ident"])                      # the summary's linked-deposit column, when the layout has one
        put(37, f"{s['label']} A/c {s['ident']} {inr(bal)} " + (f"{fd} {inr(bal + Decimal(fd.replace(',', '')))} " if fd else "") + "Registered"); newline()
    if fds:
        newline()
        put(37, "FIXED DEPOSITS LINKED TO ACCOUNT - INR"); newline()
        for fd in fds:
            put(37, f"DEPOSITS LINKED TO ACCOUNT No. {fd['linked']}"); newline()
            put(37, fd["text"]); newline()
        put(37, "Grand Total 50,000.00 53,400.00 50,000.00"); newline()
    newline(2)

    def header(s, first: bool):
        put(37, f"Statement of Transactions in {s['label']} Account {s['ident']} in INR for the period "
                f"{s['period'][0]} - {s['period'][1]}")
        newline(1.6)
        put(37, "DATE"); put(77, "MODE"); put(144, "PARTICULARS")
        put_right(DEP, "DEPOSITS"); put_right(WD, "WITHDRAWALS"); put_right(BAL, "BALANCE")
        newline(1.6)

    for s in sections:
        if state["y"] > 600:
            pages.append([]); state["y"] = 60.0
        header(s, True)
        bal = s["opening"]
        put(35, "01-10-2026"); put(144, "B/F"); put_right(BAL, inr(bal)); newline(1.5)
        sum_dep = sum_wd = Decimal(0)
        n_on_page = 0
        for r in s["rows"]:
            lines = r["head"] + ([r["own"]] if r["own"] else []) + r["tail"]
            block = max(len(lines), 1) * H
            if n_on_page >= rows_per_page or state["y"] + block > PAGE_LIMIT:
                put(141, "Total:"); put_right(DEP, inr(sum_dep)); put_right(WD, inr(sum_wd)); put_right(BAL, inr(bal))
                pages.append([]); state["y"] = 60.0
                header(s, False)
                sum_dep = sum_wd = Decimal(0)
                n_on_page = 0
            y0 = state["y"]
            tops = [y0 + k * H for k in range(len(lines))]
            anchor = (tops[0] + tops[-1]) / 2 if tops else y0
            k = 0
            for text in r["head"]:
                put(144, text, tops[k]); k += 1
            if r["own"]:
                put(144, r["own"], tops[k]); k += 1
            for text in r["tail"]:
                put(144, text, tops[k]); k += 1
            bal += r["amount"]
            printed = r["bal"] if r["bal"] is not None else bal
            if r["glue"] and r["mode"]:
                first, _, rest = r["mode"].partition(" ")
                put(35, r["date"] + first, anchor)
                if rest:
                    put(100, rest, anchor)
            else:
                put(35, r["date"], anchor)
                if r["mode"]:
                    put(77, r["mode"], anchor)
            is_dep = r["amount"] > 0 if r["col"] is None else r["col"] == "dep"
            put_right(DEP if is_dep else WD, inr(r["amount"]), anchor)
            put_right(BAL, inr(printed), anchor)
            sum_dep += r["amount"] if r["amount"] > 0 else 0
            sum_wd += -r["amount"] if r["amount"] < 0 else 0
            state["y"] = y0 + block
            n_on_page += 1
        put(141, "Total:")
        put_right(DEP, total_dep_error or inr(sum_dep)); put_right(WD, inr(sum_wd))
        put_right(BAL, inr(s["total_bal"] if s["total_bal"] is not None else bal))
        newline(3)
    return build_pdf(pages)


FD_TEXT = "900000000001 05-03-2026 50,000.00 6.75 12 Mths 0 Days 53,400.00 05-03-2027 50,000.00 Registered"


# ── the statement you request from net banking ("Statement of Transactions" for a period) ─────────────────────────
# Same tables as the emailed combined statement, but the header repeats on every page, a section ends with one TOTAL
# row (not one per page), the heading says "Account Number:", and each page starts with lines that are not part of the
# table (a page counter and the customer's name).

RQ_DEP_HEAD, RQ_WD_HEAD, RQ_BAL_HEAD = 425.0, 495.0, 585.0         # right edges of the header words
RQ_DEP, RQ_WD, RQ_BAL = 435.0, 507.0, 592.0                          # right edges of the amounts (a little to the right)


def icici_requested_statement(sections: list[dict], *, rows_per_page: int = 99, total_dep_error: str | None = None,
                              total_bal_error: str | None = None, furniture: str = "MR. TEST PERSON") -> bytes:
    """`sections` as for icici_statement (label may be '' for a heading that names no account type)."""
    pages: list[list[tuple[float, float, str]]] = [[]]
    state = {"y": 60.0}

    def put(x, text, y=None):
        pages[-1].append((x, state["y"] if y is None else y, text))

    def put_right(edge, text, y=None):
        put(edge - _width(text), text, y)

    def newline(n=1.0):
        state["y"] += H * n

    def header():
        put(27, "DATE"); put(76, "MODE**"); put(158, "PARTICULARS")
        put_right(RQ_DEP_HEAD, "DEPOSITS"); put_right(RQ_WD_HEAD, "WITHDRAWALS"); put_right(RQ_BAL_HEAD, "BALANCE")
        newline(1.8)

    def new_page(in_section: bool):
        pages.append([])
        state["y"] = 41.0
        put(524, "Page"); put(554, str(len(pages))); put(564, "of"); put(578, "9")      # the page counter, then the name
        state["y"] = 128.0
        put(27, furniture)
        state["y"] = 151.0
        if in_section:
            header()

    put(27, "ACCOUNT DETAILS - INR"); newline(1.6)
    put(27, "ACCOUNT TYPE"); put(181, "A/c"); put(196, "BALANCE(I)"); newline()
    for s in sections:
        if s["label"] or s.get("summary_label"):
            bal = s["opening"] + sum((r["amount"] for r in s["rows"]), Decimal(0))
            put(27, f"{s.get('summary_label') or s['label']} A/c {s['ident']} {inr(bal)} 0.00 {inr(bal)} Registered"); newline()
    newline(2)
    for s in sections:
        if state["y"] > 600:
            new_page(False)
        label = f"{s['label']} " if s["label"] else ""
        put(27, f"Statement of Transactions in {label}Account Number: {s['ident']} in INR for the period "
                f"{s['period'][0]} - {s['period'][1]}")
        newline(1.6)
        header()
        bal = s["opening"]
        put(27, "01-04-2025"); put(158, "B/F"); put_right(RQ_BAL, inr(bal)); newline(1.5)
        dep = wd = Decimal(0)
        n = 0
        for r in s["rows"]:
            lines = r["head"] + ([r["own"]] if r["own"] else []) + r["tail"]
            block = max(len(lines), 1) * H
            if n >= rows_per_page or state["y"] + block > PAGE_LIMIT:
                new_page(True)
                n = 0
            y0 = state["y"]
            tops = [y0 + k * H for k in range(len(lines))]
            anchor = (tops[0] + tops[-1]) / 2 if tops else y0
            for text, top in zip(lines, tops):
                put(158, text, top)
            bal += r["amount"]
            put(27, r["date"], anchor)
            if not r.get("no_amount"):
                put_right(RQ_DEP if r["amount"] > 0 else RQ_WD, inr(r["amount"]), anchor)
            if r.get("both_amounts"):                          # a damaged row printing a deposit and a withdrawal
                put_right(RQ_WD if r["amount"] > 0 else RQ_DEP, inr(r["amount"]), anchor)
            put_right(RQ_BAL, inr(r["bal"] if r["bal"] is not None else bal), anchor)
            dep += r["amount"] if r["amount"] > 0 else 0
            wd += -r["amount"] if r["amount"] < 0 else 0
            state["y"] = y0 + block
            n += 1
        put(155, "TOTAL")
        put_right(RQ_DEP, total_dep_error or inr(dep)); put_right(RQ_WD, inr(wd))
        put_right(RQ_BAL, total_bal_error or inr(bal))
        newline(3)
    return build_pdf(pages)
