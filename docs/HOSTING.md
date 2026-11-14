# Hosting Kosh on a server and using it from your phone

Kosh is an installable web app (PWA). Run it on a small VPS, open it on your phone, tap **Add to Home Screen**, and it
behaves like an app: its own icon, full screen, no browser bar.

**Read this first.** Kosh holds your balances, transactions and (if you use mailbox sync) a mail app password. Putting it
on a server means putting that on a machine on the internet. Choose the safest access you can live with:

| Way in | Exposure | Use it when |
|---|---|---|
| **Tailscale** (recommended) | None. The server has no public web port; only your own devices can reach it | You can install Tailscale on your phone. This is the safest option |
| **Public HTTPS + login** | The login page is on the internet, protected by a password and an authenticator code | You need to open it from any browser |

The full list of protections and limits is in [SECURITY.md](../SECURITY.md). In both cases the app asks for a password. Do not run it without one anywhere except `127.0.0.1`.

## What protects you

- Sign-in with a password (stored only as a salted scrypt hash) and, by default, a 6-digit code from an authenticator app.
- Five wrong guesses lock that address out, with the lock growing to an hour; a global cap stops guessing from many addresses.
- Sessions are signed, last 30 days, and stop working when you change the password or press **Sign out everywhere**.
- The Identity page lists recent sign-ins and failures (and `data/audit.log` keeps them), so you can spot guessing.
- The cookie is HttpOnly, SameSite=Strict and, on HTTPS, Secure with the `__Host-` prefix (a sibling subdomain cannot shadow it); every response carries `no-store`, a Content-Security-Policy and
  anti-framing headers; forms must come from the same site; the app only answers to your domain.
- The installed app caches only its stylesheet, scripts and icons. **Pages and figures are never stored on the phone**:
  offline you see a notice, not old balances.

## What does not protect you (be honest with yourself)

- **The data on the server is not encrypted by Kosh.** The ledger (`finance.db`) and the saved secrets (`secrets.json`: mailbox
  app passwords, your Identity details) are plain files in the Docker volume, readable by whoever controls the server.
  Use a provider you trust, turn on disk encryption if it is offered, keep the OS patched, and allow only SSH keys.
- Anyone who gets your password **and** your phone's authenticator can sign in. Use a long random password.
- A mailbox app password on a server is a credential an attacker could use to read that mailbox. Use a dedicated mailbox or
  Gmail app password you can revoke, or upload statements by hand and skip mailbox sync.

## Option A: Tailscale (no public exposure)

```bash
# on the server, with Docker and Tailscale installed and `tailscale up` done
docker run -d --name kosh --restart unless-stopped \
  -e KOSH_AUTH=login -e KOSH_TRUST_PROXY=1 -e KOSH_ALLOWED_HOSTS=myserver.tailnet-name.ts.net \
  -p 127.0.0.1:8765:8765 -v kosh-data:/app/data ghcr.io/phaninderg/kosh:latest
docker exec -it kosh python main.py set-password        # add the printed key to your authenticator app
tailscale serve --bg --https=443 http://127.0.0.1:8765  # HTTPS on your tailnet, with a real certificate
```

Open `https://myserver.tailnet-name.ts.net` on your phone (with Tailscale on) and sign in. `KOSH_ALLOWED_HOSTS` must be
the exact name `tailscale serve` shows you.

## Option B: public HTTPS with Caddy

1. Create a DNS `A` record for a name you own (say `kosh.example.com`) pointing at the server.
2. Open only ports 22, 80 and 443 in the server's firewall (`ufw allow 22,80,443/tcp && ufw allow 443/udp && ufw enable`).
3. On the server:

   ```bash
   git clone https://github.com/phaninderg/kosh.git && cd kosh
   echo "KOSH_DOMAIN=kosh.example.com" > .env
   docker compose -f docker-compose.vps.yml up -d
   docker compose -f docker-compose.vps.yml exec kosh python main.py set-password
   ```

   `set-password` asks for a password (12+ characters) and prints an authenticator key once. Add it to your authenticator app.
   Pass `--no-totp` only if the server is reachable solely through a private network.
4. Caddy gets a Let's Encrypt certificate by itself. Open `https://kosh.example.com`, sign in, and check the padlock.

## Put it on your phone

- **iPhone (Safari):** open the address, Share, **Add to Home Screen**.
- **Android (Chrome):** menu, **Install app** (or **Add to Home screen**).

It needs HTTPS, which both options above provide; an installed app will not work over plain `http://`.

## Day-to-day

- **Upgrade:** `docker compose -f docker-compose.vps.yml pull && docker compose -f docker-compose.vps.yml up -d`. Your data stays in the volume.
- **Back up** (do it before upgrades and on a schedule):
  `docker run --rm -v kosh-data:/d -v "$PWD":/b alpine tar czf /b/kosh-$(date +%F).tgz -C /d .`
  The archive contains everything above, secrets included: store it as carefully as the server itself.
- **Lost phone?** Sign in elsewhere and press **Sign out everywhere** on the Identity page, or run `set-password` again.
- **Locked out?** `docker compose -f docker-compose.vps.yml exec kosh python main.py set-password` resets the password and the authenticator.
- **Logs:** `docker compose -f docker-compose.vps.yml logs -f kosh`. The app does not log passwords, codes or statement contents.
