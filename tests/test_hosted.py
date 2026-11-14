"""Hosted mode: login (password + authenticator code), signed sessions, throttling, security headers, and the PWA files.
The app is exercised over https on a made-up domain, as a reverse proxy would present it."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import finance.web.app as webapp
from finance.web import auth, pwa
from tests.test_web import client  # noqa: F401  (the local token-mode client)

HOST = "kosh.example.com"
PASSWORD = "correct horse battery"
STATIC = Path(webapp.__file__).parent / "static"


@pytest.fixture(autouse=True)
def private_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path / "data")


def make_client(db_path, tmp_path, *, totp=None, creds=None, host=HOST, scheme="https", hosts=None):
    creds = creds or (auth.hash_password(PASSWORD), totp)
    app = webapp.create_app("", db_path, mail_config_path=str(tmp_path / "mb.json"), auth="login",
                            credentials=lambda: creds, extra_hosts=hosts or [HOST])
    c = TestClient(app, base_url=f"{scheme}://{host}", follow_redirects=False, headers={"origin": f"{scheme}://{host}"})
    c.creds = creds
    return c


def login(c, password=PASSWORD, code="", **extra):
    return c.post("/login", data={"password": password, "code": code, **extra})


def code_now(secret):
    import time
    return auth._hotp(secret, int(time.time() // 30))


# ── primitives ───────────────────────────────────────────────────────────────

def test_password_hash_verifies_only_the_right_password_and_is_salted():
    h = auth.hash_password(PASSWORD)
    assert auth.verify_password(PASSWORD, h) and not auth.verify_password("wrong", h)
    assert auth.hash_password(PASSWORD) != h and PASSWORD not in h and h.startswith("scrypt$")
    assert not auth.verify_password(PASSWORD, "garbage") and not auth.verify_password(PASSWORD, "md5$1$2$3$ab$cd")


def test_totp_matches_the_rfc_6238_test_vector_and_refuses_a_replayed_code():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"                     # "12345678901234567890", the RFC's key
    assert auth._hotp(secret, 59 // 30) == "287082"                  # RFC 6238: T=59 -> 94287082, last six digits
    v = auth.TotpVerifier(clock=lambda: 59)
    assert v.verify(secret, "287082") and not v.verify(secret, "287082")      # a code works once
    assert not auth.TotpVerifier(clock=lambda: 59).verify(secret, "000000")
    assert not auth.TotpVerifier(clock=lambda: 59).verify(secret, "28708")     # wrong length
    assert auth.TotpVerifier(clock=lambda: 89).verify(secret, "287082")        # one step of clock drift is tolerated
    assert not auth.TotpVerifier(clock=lambda: 200).verify(secret, "287082")   # but not several


def test_throttle_locks_after_five_failures_doubles_and_resets_on_success():
    now = [1000.0]
    t = auth.Throttle(clock=lambda: now[0])
    for _ in range(5):
        assert t.retry_after("a") == 0
        t.failed("a")
    assert 0 < t.retry_after("a") <= 61 and t.retry_after("b") == 0            # per address
    now[0] += 61
    assert t.retry_after("a") == 0
    for _ in range(5):
        t.failed("a")
    assert 61 < t.retry_after("a") <= 121                                      # the second lock is longer
    t.succeeded("a")
    assert t.retry_after("a") == 0


def test_safe_next_only_allows_paths_on_this_site():
    for bad in ("//evil.example", "https://evil.example/x", "evil", "/\\evil.example", ""):
        assert auth.safe_next(bad) == "/"
    assert auth.safe_next("/transactions?q=sip") == "/transactions?q=sip"


# ── the login flow ───────────────────────────────────────────────────────────

def test_pages_need_a_session_and_a_browser_is_sent_to_the_login_page(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    r = c.get("/transactions?q=sip", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Ftransactions%3Fq%3Dsip"
    assert c.get("/transactions").status_code == 401 and c.post("/import").status_code == 401
    assert "Password" in c.get("/login").text


def test_a_correct_password_signs_in_with_a_hardened_cookie(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    r = login(c, next="/transactions")
    assert r.status_code == 303 and r.headers["location"] == "/transactions"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie
    assert c.get("/").status_code == 200 and c.get("/accounts").status_code == 200
    assert c.get("/login").status_code == 303                                   # already signed in


def test_wrong_password_gets_one_vague_message_and_no_session(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    r = login(c, "wrong password here")
    assert r.status_code == 401 and "Incorrect password or code." in r.text and "set-cookie" not in r.headers
    assert c.get("/").status_code == 401


def test_an_authenticator_code_is_required_when_one_is_set(db_path, tmp_path):
    secret = auth.new_totp_secret()
    c = make_client(db_path, tmp_path, totp=secret)
    assert "authenticator" in c.get("/login").text
    for bad in ("", "000000", "abcdef"):
        assert login(c, code=bad).status_code == 401
    assert login(c, "wrong password here", code_now(secret)).status_code == 401  # the code alone is not enough
    ok = login(c, code=code_now(secret))
    assert ok.status_code == 303 and c.get("/").status_code == 200
    c.post("/logout")
    assert login(c, code=code_now(secret)).status_code == 401                    # the same code twice is refused


def test_guessing_is_throttled_even_for_the_right_password(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    for _ in range(5):
        assert login(c, "nope nope nope").status_code == 401
    r = login(c)
    assert r.status_code == 429 and "Too many attempts" in r.text and int(r.headers["retry-after"]) > 0
    assert c.get("/").status_code == 401


def test_login_cannot_be_used_to_bounce_to_another_site(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    assert login(c, next="//evil.example/steal").headers["location"] == "/"
    assert login(c, next="https://evil.example").headers["location"] == "/"


def test_a_cookie_does_not_survive_a_password_change_or_sign_out_everywhere(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    login(c)
    cookie = c.cookies.get("__Host-kosh_session")
    assert auth.session_valid(cookie, c.creds)
    assert not auth.session_valid(cookie, (auth.hash_password("a different password"), None))   # new password
    assert not auth.session_valid(cookie, (c.creds[0], auth.new_totp_secret()))                  # new authenticator
    assert not auth.session_valid(cookie, c.creds, now=2 ** 33)                                  # expired
    assert not auth.session_valid(cookie[:-3] + "abc", c.creds) and not auth.session_valid("", c.creds)
    c.post("/logout", data={"everywhere": "1"})
    assert not auth.session_valid(cookie, c.creds)                                               # the key was rotated


def test_sign_out_clears_this_device(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    login(c)
    r = c.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/").status_code == 401


def test_only_the_configured_domain_is_answered_and_forms_must_come_from_it(db_path, tmp_path):
    assert make_client(db_path, tmp_path, host="evil.example").get("/login").status_code == 400
    c = make_client(db_path, tmp_path)
    assert c.post("/login", data={"password": PASSWORD}, headers={"origin": "https://evil.example"}).status_code == 403
    assert make_client(db_path, tmp_path, host=HOST.upper()).get("/login").status_code == 200   # host names are case-insensitive


def test_no_password_set_means_nothing_is_served(db_path, tmp_path):
    app = webapp.create_app("", db_path, auth="login", credentials=lambda: None, extra_hosts=[HOST])
    c = TestClient(app, base_url=f"https://{HOST}", follow_redirects=False)
    assert c.get("/").status_code == 503 and c.get("/login").status_code == 503


def test_every_response_carries_security_headers_and_pages_are_never_cached(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    login(c)
    for path in ("/", "/login", "/transactions"):
        h = c.get(path).headers
        assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in h["content-security-policy"] and "default-src 'self'" in h["content-security-policy"]
        assert h["strict-transport-security"].startswith("max-age=")
    assert "no-store" not in c.get("/static/style.css").headers.get("cache-control", "")


def test_the_hosted_server_refuses_to_start_unprotected(monkeypatch):
    from finance.web import server
    monkeypatch.setattr(auth, "load_credentials", lambda: None)
    with pytest.raises(SystemExit, match="set-password"):
        server.serve_hosted()
    monkeypatch.setattr(auth, "load_credentials", lambda: ("h", None))
    monkeypatch.setattr(server, "ALLOWED_HOSTS", [])
    with pytest.raises(SystemExit, match="KOSH_ALLOWED_HOSTS"):
        server.serve_hosted()


def test_set_password_writes_a_private_hash_and_an_authenticator_secret(monkeypatch, capsys):
    import stat
    from finance import cli
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli.set_password() == 0
    out = capsys.readouterr().out
    pw_hash, secret = auth.load_credentials()
    assert auth.verify_password(PASSWORD, pw_hash) and secret and secret in out and PASSWORD not in out
    assert stat.S_IMODE((auth.DATA_DIR / "auth.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((auth.DATA_DIR / ".session_key").stat().st_mode) == 0o600
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "short")
    assert cli.set_password() == 1


# ── the installable app ──────────────────────────────────────────────────────

def test_the_manifest_and_service_worker_load_without_signing_in(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["display"] == "standalone" and data["start_url"] == "/" and data["short_name"] == "Kosh"
    sizes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in data["icons"]:
        assert c.get(icon["src"]).status_code == 200 and (STATIC / icon["src"].removeprefix("/static/")).read_bytes()[:4] == b"\x89PNG"
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
    assert c.get("/transactions").status_code == 401                          # everything else still needs the login


def test_the_service_worker_never_stores_pages_or_data():
    sw = pwa.SERVICE_WORKER
    assert sw.count("c.put(") == 1                                             # the one cache write...
    static_branch = sw.split("req.mode === 'navigate'")[0]
    assert "c.put(req, copy)" in static_branch and "/static/" in static_branch  # ...is for /static/ files only
    navigate_branch = sw.split("req.mode === 'navigate'")[1]
    assert "caches" not in navigate_branch and "cache.put" not in sw           # pages: network only, then a data-free offline notice
    assert "req.method !== 'GET'" in sw                                        # uploads and forms are never touched
    assert "You are offline" in pwa.OFFLINE_PAGE and "Nothing is kept on this device" in pwa.OFFLINE_PAGE


def test_pages_link_the_manifest_and_register_the_worker_and_apple_icon(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    login(c)
    page = c.get("/").text
    for needle in ('rel="manifest" href="/manifest.webmanifest"', 'rel="apple-touch-icon"', "viewport-fit=cover",
                   'name="theme-color"', "serviceWorker", "Sign out"):
        assert needle in page
    assert "Sign out everywhere" in c.get("/identity").text


def test_the_manifest_is_reachable_in_local_token_mode_too(client):
    assert client.get("/manifest.webmanifest").status_code == 200 and client.get("/sw.js").status_code == 200
    assert client.get("/static/icons/icon-192.png").status_code == 200
    assert client.get("/").status_code == 401 and "Sign out" not in client.get("/", headers={}).text


def test_phone_styles_exist():
    css = (STATIC / "style.css").read_text()
    assert "@media(max-width:700px)" in css and "overflow-x:auto" in css and "env(safe-area-inset" in css
    assert re.search(r"font-size:16px;min-height:42px", css)                  # tap-size inputs that iOS will not zoom into


# ── borrowed from openGym: __Host- cookie, duplicate refusal, capped hashing, /64 throttling, activity log ─────────

def test_on_https_the_session_cookie_is_host_only_and_secure(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    cookie = login(c).headers["set-cookie"]
    assert cookie.startswith("__Host-kosh_session=") and "Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie
    assert c.get("/").status_code == 200


def test_over_plain_http_the_unprefixed_name_is_used_since_browsers_refuse_the_prefix(db_path, tmp_path):
    c = make_client(db_path, tmp_path, scheme="http", host="localhost:8765", hosts=["localhost:8765"])
    cookie = login(c).headers["set-cookie"]
    assert cookie.startswith("kosh_session=") and "Secure" not in cookie and c.get("/").status_code == 200


def test_two_different_values_under_one_cookie_name_are_both_refused(db_path, tmp_path):
    c = make_client(db_path, tmp_path)
    login(c)
    good = c.cookies.get("__Host-kosh_session")
    assert auth.cookie_value(f"__Host-kosh_session={good}; __Host-kosh_session={good}", "__Host-kosh_session") == good
    assert auth.cookie_value(f"__Host-kosh_session=planted; __Host-kosh_session={good}", "__Host-kosh_session") == ""
    assert auth.cookie_value(f"__Host-kosh_session={good}; __Host-kosh_session=planted", "__Host-kosh_session") == ""
    c.cookies.clear()
    r = c.get("/", headers={"cookie": f"__Host-kosh_session={good}; __Host-kosh_session=planted", "accept": "text/html"})
    assert r.status_code == 303                                               # a shadowing attempt gets nothing
    assert c.get("/", headers={"cookie": f"kosh_session={good}", "accept": "text/html"}).status_code == 303   # wrong name on https


def test_throttling_counts_an_ipv6_block_as_one_address_and_unwraps_mapped_ipv4():
    assert auth.client_key("2001:db8:abcd:12::1") == auth.client_key("2001:db8:abcd:12:ffff:ffff:ffff:ffff") == "2001:db8:abcd:12::/64"
    assert auth.client_key("2001:db8:abcd:13::1") != auth.client_key("2001:db8:abcd:12::1")
    assert auth.client_key("::ffff:203.0.113.9") == auth.client_key("203.0.113.9") == "203.0.113.9"
    assert auth.client_key("not an ip") == "not an ip" and auth.client_key("") == "unknown"


def test_a_flood_of_password_checks_is_capped_and_answered_busy(db_path, tmp_path, monkeypatch):
    c = make_client(db_path, tmp_path)
    for _ in range(2):
        assert auth._HASH_SLOTS.acquire(timeout=0)                           # two checks already running
    monkeypatch.setattr(auth._HASH_SLOTS, "acquire", lambda timeout=None: False)   # and a third gives up waiting
    try:
        r = login(c)
    finally:
        monkeypatch.undo()
        for _ in range(2):
            auth._HASH_SLOTS.release()
    assert r.status_code == 503 and "busy" in r.text and "set-cookie" not in r.headers
    assert login(c).status_code == 303                                        # and it recovers


def test_sign_ins_are_logged_privately_without_any_secret_and_shown_on_identity(db_path, tmp_path):
    import stat
    c = make_client(db_path, tmp_path)
    login(c, "wrong password here")
    login(c)
    c.post("/logout")
    login(c)
    events = auth.recent_events()
    assert [e["event"] for e in events] == ["signed in", "signed out", "signed in", "sign-in failed"] and events[-1]["ok"] is False
    text = (auth.DATA_DIR / "audit.log").read_text()
    assert PASSWORD not in text and "wrong password here" not in text and "scrypt" not in text
    assert stat.S_IMODE((auth.DATA_DIR / "audit.log").stat().st_mode) == 0o600
    page = c.get("/identity").text
    assert "Recent sign-in activity" in page and "sign-in failed" in page


def test_the_activity_log_is_bounded(tmp_path):
    for i in range(auth._LOG_LIMIT + 50):
        auth.audit(f"event {i}")
    assert len((auth.DATA_DIR / "audit.log").read_text().splitlines()) == auth._LOG_LIMIT
    assert auth.recent_events(1)[0]["event"] == f"event {auth._LOG_LIMIT + 49}"
