import pytest
from pydantic import SecretStr

from finance.passwords import DecryptAgent, Identity, MissingValue, validate_template
from tests.mailfix import make_pdf

ID = {"name": "Test Person", "dob": "1990-05-17", "pan": "ABCDE1234F", "mobile": "+91 98765 43210",
      "password": "s3cret!"}


def test_template_resolution():
    i = Identity(ID)
    assert i.resolve("{name4}{ddmm}") == "test1705"
    assert i.resolve("{NAME4}{ddmmyyyy}") == "TEST17051990"
    assert i.resolve("{pan}") == "abcde1234f" and i.resolve("{PAN}") == "ABCDE1234F"
    assert i.resolve("{mobile5}") == "43210" and i.resolve("{ddmmyy}-{yyyy}") == "170590-1990"
    assert i.resolve("{password}") == "s3cret!" == i.resolve("{cas_password}")        # the old token still works


def test_template_validation():
    validate_template("{name4}{ddmm}")
    for bad in ("", "no tokens", "{bogus}", "{name4}{whatever}"):
        with pytest.raises(ValueError):
            validate_template(bad)


def test_missing_values_are_named_but_never_leaked():
    with pytest.raises(MissingValue) as e:
        Identity({"name": "Test Person"}).resolve("{name4}{ddmm}{pan}")
    assert e.value.fields == ["dob", "pan"] and "Test" not in str(e.value)


def test_unencrypted_pdf_needs_no_password():
    r = DecryptAgent(Identity({})).unlock(make_pdf(), "a@b.com", [], [])
    assert r.ok and not r.encrypted and r.password is None


def test_finds_working_template_and_caches_it():
    agent = DecryptAgent(Identity(ID))
    pdf = make_pdf("test1705")
    r = agent.unlock(pdf, "bank@x.com", ["{password}", "{name4}{ddmm}"], ["{pan}"])
    assert r.ok and r.encrypted and r.template == "{name4}{ddmm}"
    assert r.password.get_secret_value() == "test1705"
    assert "test1705" not in repr(r) and isinstance(r.password, SecretStr)
    agent.cache["bank@x.com"] = r.template
    r2 = DecryptAgent(Identity(ID), {"bank@x.com": "{name4}{ddmm}"}).unlock(pdf, "bank@x.com", ["{pan}"], [])
    assert r2.template == "{name4}{ddmm}"                   # cached template is tried first


def test_defaults_used_after_sender_templates():
    r = DecryptAgent(Identity(ID)).unlock(make_pdf("s3cret!"), "a@b.com", ["{pan}"], ["{password}"])
    assert r.ok and r.template == "{password}"


def test_failure_reason_is_helpful_and_clean():
    r = DecryptAgent(Identity({"name": "Test Person"})).unlock(make_pdf("whatever"), "a@b.com",
                                                                ["{name4}{ddmm}", "{name4}"], [])
    assert not r.ok and r.encrypted
    assert "tried 1 password candidate" in r.reason and "dob" in r.reason
    assert "Test" not in r.reason and "test" not in r.reason


def test_no_templates_configured():
    r = DecryptAgent(Identity(ID)).unlock(make_pdf("x"), "a@b.com", [], [])
    assert not r.ok and "no password templates" in r.reason


def test_priority_password_is_tried_first_and_extras_last():
    pdf = make_pdf("custom-pw-1")
    r = DecryptAgent(Identity(ID)).unlock(pdf, "a@b.com", ["{pan}"], [], priority=["custom-pw-1"])
    assert r.ok and r.template is None and r.password.get_secret_value() == "custom-pw-1"
    saved = DecryptAgent(Identity({**ID, "extra_passwords": ["nope", "custom-pw-1"]})).unlock(pdf, "a@b.com", ["{pan}"], [])
    assert saved.ok and saved.template is None
    bad = DecryptAgent(Identity({**ID, "extra_passwords": ["nope"]})).unlock(pdf, "a@b.com", ["{pan}"], [])
    assert not bad.ok and "custom-pw-1" not in bad.reason and "nope" not in bad.reason


# ── {card4}: the last 4 digits of the card number (Tata: ddmmyyyy + card ending) ─────────────────────────

def test_card4_template_resolves_with_a_given_ending_and_validates():
    validate_template("{ddmmyyyy}{card4}")
    assert Identity(ID).resolve("{ddmmyyyy}{card4}", "4321") == "170519904321"
    with pytest.raises(MissingValue):
        Identity(ID).resolve("{ddmmyyyy}{card4}")


def test_card4_known_endings_are_tried_first_then_remembered():
    pdf = make_pdf("170519904321")
    r = DecryptAgent(Identity(ID), card4s=["1111", "4321"]).unlock(pdf, "tata", ["{ddmmyyyy}{card4}"], [])
    assert r.ok and r.template == "{ddmmyyyy}{card4}" and r.card4 == "4321"
    assert r.password.get_secret_value() == "170519904321"


def test_card4_unknown_ending_is_searched_as_a_last_resort(monkeypatch):
    monkeypatch.setattr("finance.passwords.CARD4_SPACE", 100)
    pdf = make_pdf("170519900042")
    r = DecryptAgent(Identity(ID)).unlock(pdf, "tata", ["{ddmmyyyy}{card4}"], [])
    assert r.ok and r.card4 == "0042"
    miss = DecryptAgent(Identity(ID)).unlock(make_pdf("170519909999"), "tata", ["{ddmmyyyy}{card4}"], [])
    assert not miss.ok


def test_card4_remembered_endings_come_from_identity():
    assert Identity({**ID, "card4": "1234, 5678 junk 99"}).card4_values() == ["1234", "5678"]


def test_a_found_card_ending_is_remembered_in_the_keyring():
    Identity().remember_card4("4321"); Identity().remember_card4("4321"); Identity().remember_card4("0042")
    assert Identity().card4_values() == ["4321", "0042"]


def test_the_default_password_is_always_tried_last_without_any_template():
    pdf = make_pdf("s3cret!")                                                 # ID's password
    r = DecryptAgent(Identity(ID)).unlock(pdf, "anyone@bank.example", [])
    assert r.ok and r.template == "{password}"
    off = DecryptAgent(Identity(ID)).unlock(pdf, "anyone@bank.example", [], [])      # an explicit empty list turns it off
    assert not off.ok
