import os
os.environ["LEDGER_HASH_KEY"] = "test-key-not-a-secret"   # before any finance import

from datetime import date
from decimal import Decimal

import pytest
from casparser.types import (CASData, Folio, InvestorInfo, Scheme, SchemeValuation, StatementPeriod,
                             TransactionData)

from finance.parsers.cas import CasParser

FOLIO = "1234567890"          # synthetic
FOLIO2 = "9999990001"

def make_cas(*, period=("2024-01-01", "2025-01-31"), extra_txn=True, close_calc_off=False, schemes=("A", "B"),
             folio=FOLIO, variant=0):
    def txn(d, desc, amt, units, nav, bal, typ):
        return TransactionData(date=date.fromisoformat(d), description=desc, amount=Decimal(amt),
                               units=Decimal(units), nav=Decimal(nav), balance=Decimal(bal), type=typ)

    def scheme_a():
        txns = [txn("2024-01-10", "SIP", str(5000 + variant), "50", "100", "50", "PURCHASE_SIP"),
                txn("2024-02-10", "SIP", str(5000 + variant), "40", "125", "90", "PURCHASE_SIP")]
        if extra_txn:
            txns.append(txn("2025-01-10", "SIP", str(5000 + variant), "25", "200", "115", "PURCHASE_SIP"))
        close = Decimal("115") if extra_txn else Decimal("90")
        return Scheme(scheme="Synthetic Flexi Cap Fund - Direct Growth", rta_code="SYN1", rta="CAMS",
                      isin="INF000SYN001", amfi="100001", open=Decimal(0), close=close,
                      close_calculated=close + (Decimal("5") if close_calc_off else 0),
                      valuation=SchemeValuation(date=date.fromisoformat(period[1]), nav=Decimal("210"),
                                                cost=Decimal("15000") if extra_txn else Decimal("10000"),
                                                value=close * 210),
                      transactions=txns)

    def scheme_b():
        txns = [txn("2024-03-01", "Purchase", str(10000 + variant), "100", "100", "100", "PURCHASE"),
                txn("2024-09-01", "Redemption", str(-12000 - variant), "-100", "120", "0", "REDEMPTION")]
        return Scheme(scheme="Synthetic Liquid Fund - Direct Growth", rta_code="SYN2", rta="CAMS",
                      isin="INF000SYN002", amfi="100002", open=Decimal(0), close=Decimal(0),
                      close_calculated=Decimal(0),
                      valuation=SchemeValuation(date=date.fromisoformat(period[1]), nav=Decimal("125"),
                                                cost=Decimal(0), value=Decimal(0)),
                      transactions=txns)

    chosen = [{"A": scheme_a, "B": scheme_b}[k]() for k in schemes]
    return CASData(
        statement_period=StatementPeriod(**{"from": period[0], "to": period[1]}),
        folios=[Folio(folio=folio, amc="Synthetic AMC", schemes=chosen)],
        investor_info=InvestorInfo(name="Test Person", email="t@example.invalid", address="-", mobile="0"),
        cas_type="DETAILED", file_type="CAMS")


@pytest.fixture
def stmt():
    return CasParser().from_casdata(make_cas())


PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    """Nothing a test does may touch the real data/ folder: profiles, vaults and logs live under tmp_path, and the key
    derivation is made cheap (the stored record carries its own parameters, so production settings are untouched)."""
    from finance import vault
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(vault, "USERS_DIR", tmp_path / "data" / "users")
    monkeypatch.setattr(vault, "KDF", {"n": 2**4, "r": 1, "p": 1})
    monkeypatch.setenv("LEDGER_HASH_KEY", "test-hash-key")
    for k in ("KOSH_ALLOWED_HOSTS", "KOSH_TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def users(private_data):
    from finance.users import Users
    return Users()


@pytest.fixture
def registry(private_data):
    from finance.vault import Registry
    return Registry()


@pytest.fixture(autouse=True)
def me(users, registry):
    """A signed-in, unlocked profile for every test: 'the database' and 'the secrets' are its vault, exactly as in the app.
    Yields the profile record; `me["vault"]` is not stored: use vault.current_vault.get()."""
    from finance import vault
    user, dek, _recovery = users.sign_up("Test Person", PASSWORD, users.new_setup_code())
    token = vault.current_vault.set(registry.unlock(user["id"], dek))
    yield user
    vault.current_vault.reset(token)


@pytest.fixture
def db_path(me):
    """The signed-in profile's encrypted ledger."""
    from finance import vault
    return vault.VAULT_DB


def make_summary(*, as_of="2025-03-31", folios=None):
    """A SUMMARY statement the way casparser returns it: fund house 'N/A', no transactions, single date.
    `folios` = [(folio, [(isin, name, units, nav, cost_or_None)])]; defaults to the detailed fixture's folio."""
    folios = folios or [(FOLIO, [("INF000SYN001", "Synthetic Flexi Cap Fund - Direct Growth", "115", "250", "15000")])]
    out = []
    for folio, schemes in folios:
        out.append(Folio(folio=folio, amc="N/A", PAN="N/A", KYC="N/A", PANKYC="N/A", schemes=[
            Scheme(scheme=name, rta_code="X", rta="CAMS", isin=isin, amfi="1", open=Decimal(units), close=Decimal(units),
                   close_calculated=Decimal(units),
                   valuation=SchemeValuation(date=date.fromisoformat(as_of), nav=Decimal(nav),
                                             cost=Decimal(cost) if cost else None, value=Decimal(units) * Decimal(nav)),
                   transactions=[])
            for isin, name, units, nav, cost in schemes]))
    return CASData(statement_period=StatementPeriod(**{"from": as_of, "to": as_of}), folios=out,
                   investor_info=InvestorInfo(name="Test Person", email="t@example.invalid", address="-", mobile="0"),
                   cas_type="SUMMARY", file_type="CAMS")
