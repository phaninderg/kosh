"""Regenerate the dummy statements in this folder and their `manifest.json`.

    python -m samples.generate

Every figure, name and number is invented. The PDFs are built from the same synthetic layouts the tests use
(tests/pdffix.py, cardfix.py, npsfix.py), so a layout change in a parser is reflected here. A statement from a real
bank must never be added: build a synthetic one that reproduces its layout instead (see samples/README.md)."""

from __future__ import annotations

import json
from pathlib import Path

from samples.describe import REGISTRY, summarize
from tests.cardfix import crow, axis_card, hdfc_card, icici_card, tata_card
from tests.npsfix import NAMES, nps_statement, nrow, nscheme
from tests.pdffix import FD_TEXT, encrypt, icici_requested_statement, icici_statement, row, section

HERE = Path(__file__).parent
SAMPLES: list[dict] = []                                         # manifest entries, in generation order


def add(folder: str, name: str, parser: str, pdf: bytes, *, password: str | None = None, about: str) -> None:
    (HERE / folder).mkdir(exist_ok=True)
    path = HERE / folder / name
    path.write_bytes(encrypt(pdf, password) if password else pdf)
    st = REGISTRY[parser].parse(path, password)                  # a sample that does not parse must not be committed
    SAMPLES.append({"file": f"{folder}/{name}", "parser": parser, "password": password, "about": about,
                    "expect": summarize(st)})


# ── bank statements (ICICI) ───────────────────────────────────────────────────

def savings_rows():
    return [row("01-10-2026", "-9000", head=["ACH/EXAMPLE-SIP/ICIC700/TKO"], tail=["T0000001"]),
            row("05-10-2026", "200000", mode="NET BANKING", head=["ACME TRADERS LIMITED"], tail=["INF/INFT/000000000001/goods"]),
            row("07-10-2026", "-500", head=["UPI/100000000001/Coffee"], tail=["UPI/shop@bank/Bank"]),
            row("15-10-2026", "-5000", own="TO PPF 004000000000_SR123456789"),
            row("28-10-2026", "150", own="INT.PD:01-07-2026 to 30-09-2026")]


def bank() -> None:
    combined = icici_statement(
        [section("PPF", "XXXXXXXX5678", "100000.00", [row("15-10-2026", "5000", own="FRM SB 236000000000_SR123456789")]),
         section("Savings", "XXXXXXXX1234", "300000.00", savings_rows())],
        fds=[dict(linked="XXXXXXXX1234", text=FD_TEXT)])
    add("bank", "icici_monthly_combined.pdf", "icici", combined,
        about="The statement ICICI emails every month: savings, PPF and a linked fixed deposit in one PDF.")
    add("bank", "icici_monthly_combined_encrypted.pdf", "icici", combined, password="TEST0115",
        about="The same statement, password-protected (password is in the manifest).")
    requested = icici_requested_statement(
        [section("Savings", "XXXXXXXX1234", "10000.00",
                 [row("05-04-2025", "-500", head=["UPI/100000000001/Coffee"], tail=["UPI/shop@bank/Bank"]),
                  row("06-04-2025", "25000", head=["NEFT-ACME-SALARY"], tail=["REF 777"]),
                  row("07-04-2025", "-1200.50", own="IMPS/200000000002/Rent"),
                  row("09-04-2025", "150", own="INT.PD:01-01-2025 to 31-03-2025"),
                  row("11-04-2025", "10000", own="NEFT-REFUND")],
                 period=("April 01, 2025", "March 31, 2026"))],
        rows_per_page=2)
    add("bank", "icici_requested_period.pdf", "icici", requested,
        about="The 'Statement of Transactions' requested from net banking for a period; header repeats on every page.")


# ── credit cards ──────────────────────────────────────────────────────────────

def card_rows():
    return [crow("02/10/2026", "SHOP ONE MUMBAI", "1200.50", tail="IN"),
            crow("05/10/2026", "UPI 123456789012 COFFEE", "300"),
            crow("06/10/2026", "Payment received", "1500", credit=True),
            crow("12/10/2026", "REFUND SHOP ONE", "200", credit=True),
            crow("20/10/2026", "BOOKS ONLINE", "999.99")]


def cards() -> None:
    add("cards", "icici_card.pdf", "icici_card", icici_card("2000.00", card_rows()),
        about="ICICI credit card statement.")
    add("cards", "hdfc_card_one.pdf", "hdfc_card", hdfc_card("2000.00", card_rows(), card="123456XXXXXX1111"),
        about="HDFC credit card statement (first of two cards; the parser keeps them as separate accounts).")
    add("cards", "hdfc_card_two.pdf", "hdfc_card",
        hdfc_card("500.00", card_rows()[:3], card="123456XXXXXX2222"),
        about="HDFC credit card statement for a second card with different spends.")
    add("cards", "axis_card.pdf", "axis_card", axis_card("2000.00", card_rows()), about="Axis credit card statement.")
    add("cards", "tata_card_encrypted.pdf", "tata_card", tata_card("2000.00", card_rows()), password="150819900099",
        about="Tata Neu card statement; real ones open with ddmmyyyy of the birth date followed by the card's last 4 digits.")


# ── NPS ───────────────────────────────────────────────────────────────────────

def schemes():
    return [nscheme(NAMES[0], "100.0000", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000"), nrow("31-Jan-26", "-12.00", "50.0000")]),
            nscheme(NAMES[1], "200.0000", "30.0000", [nrow("05-Jan-26", "3000.00", "30.0000")]),
            nscheme(NAMES[2], "10.0000", "20.0000", [nrow("05-Jan-26", "1000.00", "20.0000")])]


def nps() -> None:
    add("nps", "nps_monthly_emailed.pdf", "nps", nps_statement(schemes(), layout="seq"),
        about="Monthly NPS transaction statement as emailed: schemes one below another.")
    add("nps", "nps_monthly_side_by_side.pdf", "nps", nps_statement(schemes(), layout="cols"),
        about="Monthly NPS statement with the schemes side by side (three-line rows).")
    add("nps", "nps_portal_annual.pdf", "nps",
        nps_statement(schemes(), portal=True, period=("April 01, 2025", "March 31, 2026")),
        about="Annual statement downloaded from the NPS portal: four-digit years, full period.")


def main() -> None:
    bank(); cards(); nps()
    (HERE / "manifest.json").write_text(json.dumps(SAMPLES, indent=2) + "\n")
    print(f"wrote {len(SAMPLES)} samples and manifest.json")


if __name__ == "__main__":
    main()
