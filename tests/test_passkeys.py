"""Passkeys (WebAuthn), exercised with a software authenticator: registering one, signing in with it, adding one on another
device with a one-time code, and everything a forged or replayed attempt must fail."""

import time

import pytest

from finance.web import auth
from pathlib import Path

from tests.softkey import SoftKey
from tests.test_accounts import PASSWORD, Site

STATIC = Path(__file__).resolve().parent.parent / "finance" / "web" / "static"


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


def register(browser, key, name="Phone"):
    opts = browser.post("/account/passkeys/options")
    assert opts.status_code == 200, opts.text
    j = opts.json()
    return browser.post("/account/passkeys/verify", json={"cid": j["cid"], "name": name, "credential": key.create(j["options"])})


def sign_in(browser, key, **kw):
    j = browser.post("/auth/passkey/login/options").json()
    return browser.post("/auth/passkey/login/verify", json={"cid": j["cid"], "credential": key.get(j["options"], **kw)})


def test_a_signed_in_person_can_add_a_passkey_and_see_it_listed(site):
    b, _ = site.person("Asha")
    key = SoftKey()
    assert register(b, key).json() == {"ok": True}
    (pk,) = site.users.by_name("Asha")["passkeys"]
    assert pk["id"] == key.id and pk["name"] == "Phone" and pk["sign_count"] == 0
    assert "Phone" in b.get("/account").text


def test_the_options_ask_for_a_discoverable_credential_with_user_verification(site):
    b, _ = site.person("Asha")
    o = b.post("/account/passkeys/options").json()["options"]
    assert o["authenticatorSelection"]["residentKey"] == "required" and o["authenticatorSelection"]["userVerification"] == "required"
    assert o["attestation"] == "none" and o["rp"]["id"] == "testserver" and o["user"]["name"] == "Asha"
    login = b.post("/auth/passkey/login/options").json()["options"]
    assert login["userVerification"] == "required" and login.get("allowCredentials", []) == []


def test_signing_in_with_a_passkey_gives_a_session_and_the_data_unlocks_only_if_it_is_already_open(site):
    b, _ = site.person("Asha")
    key = SoftKey(); register(b, key)
    phone = site.browser()
    r = sign_in(phone, key)
    assert r.status_code == 200 and r.json()["next"] == "/" and phone.get("/").status_code == 200   # the data was already unlocked
    uid = site.users.by_name("Asha")["id"]
    site.registry.lock(uid)                                                                        # the server restarted
    other = site.browser()
    r = sign_in(other, key)
    assert r.json()["next"] == "/unlock"
    assert other.get("/transactions").status_code == 423                                           # a passkey alone does not open the data
    assert other.post("/unlock", data={"password": PASSWORD}).status_code == 303
    assert other.get("/transactions").status_code == 200


def test_a_passkey_sign_in_records_the_new_counter_and_a_replay_is_refused(site):
    b, _ = site.person("Asha")
    key = SoftKey(); register(b, key)
    sign_in(site.browser(), key)
    assert site.users.by_name("Asha")["passkeys"][0]["sign_count"] == 1
    j = site.browser().post("/auth/passkey/login/options").json()
    stale = key.get(j["options"], counter=1)                                                       # not greater than the stored count
    r = site.browser().post("/auth/passkey/login/verify", json={"cid": j["cid"], "credential": stale})
    assert r.status_code == 400


def test_a_challenge_works_once_and_only_for_its_own_kind(site):
    b, _ = site.person("Asha")
    key = SoftKey(); register(b, key)
    j = site.browser().post("/auth/passkey/login/options").json()
    cred = key.get(j["options"])
    body = {"cid": j["cid"], "credential": cred}
    assert site.browser().post("/auth/passkey/login/verify", json=body).status_code == 200
    assert site.browser().post("/auth/passkey/login/verify", json=body).status_code == 400          # replayed: the challenge is spent
    o = b.post("/account/passkeys/options").json()
    wrong_kind = site.browser().post("/auth/passkey/login/verify", json={"cid": o["cid"], "credential": cred})
    assert wrong_kind.status_code == 400                                                           # a registration challenge cannot sign in


@pytest.mark.parametrize("flaw", ["wrong origin", "wrong rp", "no user verification", "unknown key"])
def test_a_flawed_assertion_is_refused(site, flaw):
    b, _ = site.person("Asha")
    key = SoftKey(); register(b, key)
    kw = {"wrong origin": {"origin": "http://evil.example"}, "no user verification": {"user_verified": False}}.get(flaw, {})
    signer = {"wrong rp": SoftKey(rp_id="evil.example"), "unknown key": SoftKey()}.get(flaw, key)
    if flaw == "wrong rp":
        signer.key, signer.cred_id = key.key, key.cred_id                                           # right key, wrong relying party
    browser = site.browser()
    r = sign_in(browser, signer, **kw)
    assert r.status_code in (400, 404) and "set-cookie" not in r.headers
    assert browser.get("/").status_code == 401


def test_registration_refuses_a_passkey_made_for_another_site_or_without_user_verification(site):
    b, _ = site.person("Asha")
    for key, kw in ((SoftKey(), {"origin": "http://evil.example"}), (SoftKey(rp_id="evil.example"), {}), (SoftKey(), {"user_verified": False})):
        j = b.post("/account/passkeys/options").json()
        r = b.post("/account/passkeys/verify", json={"cid": j["cid"], "name": "x", "credential": key.create(j["options"], **kw)})
        assert r.status_code == 400
    assert site.users.by_name("Asha")["passkeys"] == []


def test_adding_or_removing_a_passkey_needs_a_sign_in_from_the_last_ten_minutes(site, monkeypatch):
    b, _ = site.person("Asha")
    key = SoftKey(); register(b, key)
    real = time.time
    with monkeypatch.context() as later:
        later.setattr("time.time", lambda: real() + 15 * 60)
        assert b.post("/account/passkeys/options").status_code == 403
        assert b.post("/account/passkeys/verify", json={}).status_code == 403
        assert b.post("/account/passkeys/remove", data={"id": key.id}).status_code == 403
        assert b.post("/account/device-code").status_code == 403
        assert len(site.users.by_name("Asha")["passkeys"]) == 1
    assert b.post("/account/passkeys/remove", data={"id": key.id}).status_code == 303
    assert site.users.by_name("Asha")["passkeys"] == []
    assert sign_in(site.browser(), key).status_code == 404                                         # a removed passkey no longer signs in


def test_a_passkey_cannot_be_registered_twice_or_by_two_people(site):
    a, _ = site.person("Asha"); r, _ = site.person("Ravi")
    key = SoftKey(); register(a, key)
    j = r.post("/account/passkeys/options").json()
    again = r.post("/account/passkeys/verify", json={"cid": j["cid"], "name": "mine", "credential": key.create(j["options"])})
    assert again.status_code == 400 and "already registered" in again.json()["error"]


def test_a_second_device_adds_a_passkey_with_a_one_time_code_and_signs_in_with_it(site):
    b, _ = site.person("Asha")
    code = site.users.make_device_code(site.users.by_name("Asha")["id"])
    new_phone, key = site.browser(), SoftKey()
    j = new_phone.post("/auth/link/options", json={"code": code}).json()
    assert new_phone.post("/auth/link/verify", json={"cid": j["cid"], "name": "New phone", "credential": key.create(j["options"])}).json() == {"ok": True}
    assert site.users.by_name("Asha")["passkeys"][0]["name"] == "New phone"
    assert new_phone.get("/").status_code == 401                                                   # the code did not sign the device in
    assert sign_in(new_phone, key).status_code == 200 and new_phone.get("/").status_code == 200
    again = site.browser().post("/auth/link/options", json={"code": code})
    assert again.status_code == 400                                                                # one code, one passkey


def test_a_device_code_expires_and_a_newer_one_replaces_it(site, monkeypatch):
    site.person("Asha")
    uid = site.users.by_name("Asha")["id"]
    old = site.users.make_device_code(uid)
    new = site.users.make_device_code(uid)
    b = site.browser()
    assert b.post("/auth/link/options", json={"code": old}).status_code == 400
    assert b.post("/auth/link/options", json={"code": new}).status_code == 200
    real = time.time
    with monkeypatch.context() as later:
        later.setattr("time.time", lambda: real() + 11 * 60)
        assert site.browser().post("/auth/link/options", json={"code": new}).status_code == 400


def test_guessing_device_codes_is_throttled(site):
    site.person("Asha")
    code = site.users.make_device_code(site.users.by_name("Asha")["id"])
    c = site.browser()
    for _ in range(5):
        assert c.post("/auth/link/options", json={"code": "AAAA-AAAA-AAAA"}).status_code == 400
    assert c.post("/auth/link/options", json={"code": code}).status_code == 429                  # even the right one, for now


def test_a_disabled_profiles_passkey_does_not_sign_in(site):
    admin, _ = site.person("Asha"); ravi, _ = site.person("Ravi")
    key = SoftKey(); register(ravi, key)
    admin.post(f"/admin/users/{site.users.by_name('Ravi')['id']}/disable")
    assert sign_in(site.browser(), key).status_code == 403


# ── passkeys need a domain name, not an IP address ────────────────────────────────────

def test_on_an_ip_address_every_passkey_endpoint_explains_instead_of_failing_in_the_browser(site):
    b, _ = site.person("Asha")
    ip = site.browser()
    ip.base_url = "http://127.0.0.1:8765"
    ip.headers["origin"] = "http://127.0.0.1:8765"
    ip.cookies.set("kosh_session", b.cookies.get("kosh_session"), domain="127.0.0.1")
    for path, body in (("/account/passkeys/options", None), ("/auth/passkey/login/options", {})):
        r = ip.post(path, json=body) if body is not None else ip.post(path)
        assert r.status_code == 400 and "domain name, not an IP address" in r.json()["error"] and "http://localhost:8765" in r.json()["error"]
    assert site.users.by_name("Asha")["passkeys"] == []
    ok = b.post("/account/passkeys/options")                                  # the same person on a real name is fine
    assert ok.status_code == 200


def test_the_passkey_script_hides_its_buttons_on_an_ip_address_and_says_why():
    js = (STATIC / "passkey.js").read_text()
    assert "domain name, not an IP address" in js and "localhost:" in js and js.index("domain name, not an IP") < js.index("querySelectorAll('[data-passkey]')")


def test_the_server_prints_the_localhost_address_not_the_ip(monkeypatch, capsys):
    from finance.web import server
    monkeypatch.setattr(server, "UI_HOST", "127.0.0.1"); monkeypatch.setattr(server, "PUBLIC_PORT", 8765)
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(server.webbrowser, "open", lambda url: print("OPENED", url))
    server.serve()
    out = capsys.readouterr().out
    assert "http://localhost:8765" in out and "127.0.0.1" not in out.replace("OPENED", "")
