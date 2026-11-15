# Hosting Kosh on a server and using it from your phone

Kosh is an installable web app (PWA). Run it on a small VPS, open it on your phone, tap **Add to Home Screen**, and it behaves
like an app: its own icon, full screen, no browser bar. Everything, from creating the first profile to importing statements,
is done in the browser. There is no command-line password step.

**Read this first.** Kosh holds your balances and transactions and, if you use mailbox sync, a mail app password. Putting it on a
server means putting that on a machine on the internet. Choose the safest access you can live with:

| Way in | Exposure | Use it when |
|---|---|---|
| **Tailscale** (recommended) | None. The server has no public web port; only your own devices can reach it | You can install Tailscale on your phone. This is the safest option |
| **Public HTTPS** | The sign-in page is on the internet, protected by passwords, passkeys, throttling and invite-only sign-up | You need to open it from any browser |

## What protects you

- **Each person's data is encrypted for that person alone.** Every profile has its own random key. The ledger, saved secrets,
  mailbox settings, uploaded statements, job logs and backups are all sealed (AES-256-GCM) under it. The key is stored only
  *wrapped* by the person's password and by a one-time recovery key, so **no one else can read it: not another profile, not the
  admin, and not someone who copies the server's disk or a backup.**
- **Invite-only.** The first person to set up becomes the admin, and needs a setup code that the server prints once at start-up.
  Everyone after that needs a one-time invite code from the admin. The admin can invite, disable and remove people, but has no
  key to anyone's data.
- **Sign in with a passkey** (Face ID, fingerprint, a security key) or a password. A passkey signs you in; your password also
  unlocks your data. After a server restart (or 8 idle hours) you enter your password once to unlock it.
- **A forgotten password is recoverable** with the recovery key you saved at sign-up. Lose both and the data is gone for good
  (that is what "encrypted for you alone" means).
- Five wrong guesses lock that address out for a growing time (a signed-in person's own unlock and password change are limited per profile, so a stranger can never lock you out); sessions last 30 days and end when you change your password, press
  **Sign out everywhere**, or are disabled. Cookies are HttpOnly, SameSite=Strict, and on HTTPS Secure with the `__Host-` prefix.
  Every page is `no-store`; the installed app caches only its stylesheet, scripts and icons, never your figures.
- The Account page lists your recent sign-ins and failures (and the admin sees everyone's, never anyone's money).

The full list of protections and limits is in [SECURITY.md](../SECURITY.md).

## What does not protect you (be honest with yourself)

- While you are unlocked, your key is in the server's memory. Someone with root on the server **can** read it then. Encryption
  protects data at rest, backups and other profiles, not a compromised running server. Use a host you trust, keep it patched,
  allow only SSH keys, and prefer a short idle timeout (`KOSH_IDLE_LOCK_HOURS`).
- A mailbox app password inside a profile is a credential an attacker with your unlocked profile could use. Use a revocable
  one, or skip mailbox sync and upload statements by hand.
- A statement is briefly written to `/tmp` while it is read. The compose files and the command below mount `/tmp` in memory
  (`tmpfs`), so it never reaches the disk, and run the container read-only with a memory limit.

## Option A: Tailscale (no public exposure)

```bash
# on the server, with Docker and Tailscale installed and `tailscale up` done
docker run -d --name kosh --restart unless-stopped \
  -e KOSH_TRUST_PROXY=1 -e KOSH_ALLOWED_HOSTS=myserver.tailnet-name.ts.net \
  --read-only --tmpfs /tmp:size=512m --cap-drop ALL --security-opt no-new-privileges --memory 1536m --pids-limit 256 \
  -p 127.0.0.1:8765:8765 -v kosh-data:/app/data ghcr.io/phaninderg/kosh:latest
docker logs kosh                                          # shows the one-time setup code
tailscale serve --bg --https=443 http://127.0.0.1:8765   # HTTPS on your tailnet, with a real certificate
```

Open `https://myserver.tailnet-name.ts.net/signup` on your phone (with Tailscale on), enter the setup code, and create your
profile. `KOSH_ALLOWED_HOSTS` must be the exact name `tailscale serve` shows you.

## Option B: public HTTPS with Caddy

1. Create a DNS `A` record for a name you own (say `kosh.example.com`) pointing at the server.
2. Open only ports 22, 80 and 443 in the server's firewall (`ufw allow 22,80,443/tcp && ufw allow 443/udp && ufw enable`).
3. On the server:

   ```bash
   git clone https://github.com/phaninderg/kosh.git && cd kosh
   echo "KOSH_DOMAIN=kosh.example.com" > .env
   docker compose -f docker-compose.vps.yml up -d
   docker compose -f docker-compose.vps.yml logs kosh      # the one-time setup code
   ```

4. Open `https://kosh.example.com/signup`, enter the setup code, choose a name and a password (12+ characters), and **save the
   recovery key** shown next. Caddy gets a Let's Encrypt certificate by itself. Check the padlock.

The server answers only to the domain you set, over HTTPS.

## Family members

The admin opens **People**, creates an **invite code** (one use, valid for 7 days) and gives it to the person. They open
`/signup` on the same address, enter the code, and create their own profile with their own password and recovery key. Each person
sees only their own data. The admin can disable or remove a profile (removal deletes its encrypted data for good) but cannot
read it or reset its password.

## Put it on your phone

- **iPhone (Safari):** open the address, Share, **Add to Home Screen**.
- **Android (Chrome):** menu, **Install app** (or **Add to Home screen**).

It needs HTTPS, which both options above provide; an installed app will not work over plain `http://`.
Then add a passkey: **Account → Add a passkey on this device** (needs a sign-in from the last ten minutes), or make a code on
your laptop (**Account → Make a code…**) and enter it on the phone's sign-in page.

## Automatic mail sync and the idle lock

Each mailbox can sync by itself **every hour or every 6 hours** (Mailboxes page, *Automatic sync*). It works only while your data
is unlocked, and it never unlocks anything or postpones the idle lock. So after you stop using Kosh it keeps syncing for
`KOSH_IDLE_LOCK_HOURS` (8 by default), then pauses; the next time you unlock, an overdue sync starts within a minute. To keep it
running longer, set a longer `KOSH_IDLE_LOCK_HOURS`, knowing your data key then stays in the server's memory that long.

## Day-to-day

- **Upgrade:** `docker compose -f docker-compose.vps.yml pull && docker compose -f docker-compose.vps.yml up -d`. Data stays in the volume.
- **Back up** the whole volume (it is already encrypted, so the archive is as private as the server):
  `docker run --rm -v kosh-data:/d -v "$PWD":/b alpine tar czf /b/kosh-$(date +%F).tgz -C /d .`
- **Lost phone?** Sign in elsewhere and use **Account → Sign out everywhere**, then remove its passkey.
- **Forgot your password?** Use **Forgot your password?** on the sign-in page with your recovery key.
- **Logs:** `docker compose -f docker-compose.vps.yml logs -f kosh`. The app does not log passwords, codes or statement contents.
