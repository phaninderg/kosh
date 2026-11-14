# Security policy

Kosh is self-hosted: you run it, you hold the data. This file says how to report a problem and, more usefully, what the
app protects you from and what it does not. (The shape of this document, and several of the ideas in the sign-in code,
follow the [openGym](https://github.com/DuarteSantos8/openGym) project, which solved the same problem for a phone app.)

## Supported versions

Only the latest release (semver tags, `v0.1.0` and up). Fixes ship in the next release and in the `latest` image.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting (Security tab, **Report a vulnerability**). Please do not put a working exploit
in a public issue. Useful in a report: the version, whether you run the image or a source checkout, what sits in front of
the app (Caddy, Tailscale, nothing), steps to reproduce, and what an attacker gets. This is a one-person project: expect
days, not hours, and no bounty.

## In scope

- Getting past the sign-in: forging or replaying a session cookie, skipping the password or authenticator code, getting
  more password checks than the throttle allows, or reaching any page or upload route without a session.
- Reading another site's session, cross-site request forgery, or cross-site scripting in the UI.
- Reading or writing files outside the data folder through an upload, a job id or a backup name.
- Anything that sends your statement data, passwords or mailbox credentials off the machine, or into a log.
- A default in `docker-compose.yml`, `docker-compose.vps.yml` or the Dockerfile that exposes more than this file says.

## Out of scope

- Anything that already needs access to the host, the Docker socket or the data volume. The operator is trusted.
- Missing rate limits on routes other than sign-in. Put volume limits in your reverse proxy.
- Running over plain `http://` on a network: the password and cookie then cross it in the clear. Use HTTPS (Caddy or
  Tailscale, see [docs/HOSTING.md](docs/HOSTING.md)).
- Scanner output with no working exploit.

## Security model

### Two ways to run it

- **Local (default).** Listens on `127.0.0.1` only. A random token is printed at start, becomes a cookie on first use, and is
  required for every request. The Host header is checked (DNS rebinding), and forms must come from the same site.
- **Hosted (`KOSH_AUTH=login`).** For a server you reach from your phone. It refuses to start without a password and
  without `KOSH_ALLOWED_HOSTS`, and it answers only to the domains you list.

### Hosted sign-in

- **Password** (12+ characters) stored only as a salted scrypt hash (N=2^14, r=8, p=1); checked in constant time, off the
  event loop, and never more than two at once, so a flood of sign-in attempts cannot be used to exhaust the CPU.
- **Authenticator code** (RFC 6238, 6 digits) on by default. A code is spent only by a correct password and works once.
- **Throttling.** Five wrong guesses from an address lock it out, the lock doubling up to an hour; an IPv6 address counts as
  its /64 and an IPv4-mapped one as the IPv4 address; a global cap of 30 failures an hour stops many addresses sharing the work.
- **Sessions** are a signed cookie (HMAC-SHA256 under a random key in `data/.session_key`, mode 0600) valid for 30 days. It is
  bound to the current password and authenticator secret, so changing either signs every device out; **Sign out everywhere**
  replaces the key. The cookie is `HttpOnly` and `SameSite=Strict`; on HTTPS it is `Secure` and named `__Host-kosh_session`,
  which makes the browser itself reject a sibling subdomain's attempt to plant a shadowing cookie. If the same cookie name
  arrives twice with different values, both are refused.
- **Activity log.** `data/audit.log` (mode 0600, last 500 lines) records sign-ins, failures, lock-outs and sign-outs with the
  address, never a secret. The Identity page shows the latest.
- Every response carries `Cache-Control: no-store`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and a Content-Security-Policy (`default-src 'self'`, no framing, forms to self only); HTTPS
  responses add HSTS.
- The installed phone app caches only its stylesheet, scripts and icons. Pages, balances and transactions are never stored
  on the device; offline you see a notice.

### Data and secrets

- Statements are parsed locally; nothing is sent to any service. IMAP access is read-only and verifies the server's certificate.
- Mailbox app passwords and identity details live in the OS keyring, or in `data/secrets.json` (mode 0600) in a container.
- Uploads are limited to 50 MB, 25 files, and must start with `%PDF`; the client's file name is never used as a path.

### What it does not do (read this before hosting)

- **The ledger and the secrets file are not encrypted at rest.** Whoever controls the server can read them. Use a host you
  trust, full-disk encryption, and SSH keys only.
- A mailbox app password on a server is a credential an attacker with the server could use. Use a revocable one, or skip
  mailbox sync and upload statements by hand.
- The Content-Security-Policy still allows inline scripts and styles (the templates use them). A nonce-based policy is on the
  [ROADMAP](ROADMAP.md).
- Passkeys (Face ID, fingerprint) are planned and not built yet; today the sign-in is password plus an authenticator code.
