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

### Your data is encrypted for you alone

- Every profile has its own random 256-bit data key. The ledger, saved secrets (mailbox app passwords, name, PAN, date of birth,
  PDF passwords), mailbox settings, uploaded statements, job logs and backups are sealed with **AES-256-GCM** under it; file
  names on disk are opaque and each file is bound to its name, so swapping files fails.
- The key is stored only **wrapped** under keys derived (scrypt, N=2^15) from the person's password and from a one-time recovery
  key. The right password both proves who you are and unwraps the key. There is no admin override and no server-wide key.
- The unwrapped key lives only in memory, while the person is unlocked. It is dropped on lock, after 8 idle hours
  (`KOSH_IDLE_LOCK_HOURS`), on **Sign out everywhere**, when a profile is disabled, and on restart. A passkey signs you in but
  cannot unlock data by itself; the password does.
- So the admin, other profiles, and anyone who copies the data folder or a backup get ciphertext. `data/users.json` holds
  display names, roles, wrapped keys and public passkey records, and no financial data.

### Profiles

- The first profile needs a **setup code** the server prints once at start-up and becomes the admin; everyone else needs a
  one-time **invite code** (60 bits, stored hashed, 7 days). The admin can invite, disable and remove people (removal deletes
  that profile's data for good, after typing their name) and cannot read anyone's data. The last admin cannot be removed.
- Job ids, logs and settings are scoped to the profile that created them.

### Signing in

- **Passkeys** (WebAuthn, discoverable credentials, user verification required, attestation none), verified server-side with a
  signature-counter check; challenges are single-use and bound to their ceremony. Adding or removing a passkey needs a sign-in
  from the last ten minutes; another device can add one with a one-time, ten-minute, single-use code.
- **Password** (12+ characters), checked by unwrapping the key (a wrong password fails the GCM tag), off the event loop, two at a
  time. A forgotten password is reset with the **recovery key**, which is then replaced.
- **Throttling:** five wrong guesses from an address lock it out, doubling up to an hour; IPv6 counts as its /64; a global cap of
  30 failures an hour. Unknown names cost the same work as wrong passwords.
- **Sessions:** a signed cookie (HMAC-SHA256, key in `data/.session_key`, mode 0600) naming the profile and a session version;
  changing the password, **Sign out everywhere** or disabling a profile ends every cookie. HttpOnly, SameSite=Strict; on HTTPS
  Secure and named `__Host-kosh_session`. Duplicate cookies with different values are both refused.
- **Activity log** (`data/audit.log`, mode 0600, last 1000 lines): who, what, success, address; never a secret.
- Every response: `Cache-Control: no-store`, a Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`; HSTS on
  HTTPS. Forms must come from the same site; the app answers only to its configured hosts.
- The installed phone app caches only its stylesheet, scripts and icons, never pages or figures.

### Data and network

- Statements are parsed locally; nothing is sent anywhere. IMAP is read-only and verifies the server's certificate.
- Uploads: 50 MB per file, 25 files, `%PDF` magic only; the client's file name is never used as a path.

### What it does not do (read this before hosting)

- **A compromised running server can read data while a profile is unlocked** (the key is in memory then). Encryption protects data
  at rest, backups and other profiles. Use a host you trust, keep it patched, and use a short idle timeout.
- **Losing both the password and the recovery key loses the data**, by design.
- A mailbox app password saved in an unlocked profile could be used by someone who controls the running server. Use a
  revocable one, or skip mailbox sync.
- A statement is written to a temporary file for the moment it is parsed.
- `data/users.json` and `audit.log` hold display names and sign-in events in the clear.
- The Content-Security-Policy still allows inline scripts and styles (the templates use them). A nonce-based policy is on the
  [ROADMAP](ROADMAP.md).
