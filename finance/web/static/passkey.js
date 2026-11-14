// Passkey ceremonies (WebAuthn) for sign-in, adding a passkey, and adding one on a new device with a code.
(function () {
  const b64 = {
    dec: s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')), c => c.charCodeAt(0)),
    enc: b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''),
  };
  const say = t => { const m = document.getElementById('pk-msg'); if (m) m.textContent = t; };
  async function post(url, body) {
    const r = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || 'Something went wrong. Try again.');
    return j;
  }
  const creation = o => { o.challenge = b64.dec(o.challenge); o.user.id = b64.dec(o.user.id); (o.excludeCredentials || []).forEach(c => c.id = b64.dec(c.id)); return o; };
  const request = o => { o.challenge = b64.dec(o.challenge); (o.allowCredentials || []).forEach(c => c.id = b64.dec(c.id)); return o; };
  const regJSON = c => ({id: c.id, rawId: b64.enc(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults(),
    response: {attestationObject: b64.enc(c.response.attestationObject), clientDataJSON: b64.enc(c.response.clientDataJSON),
               transports: c.response.getTransports ? c.response.getTransports() : []}});
  const authJSON = c => ({id: c.id, rawId: b64.enc(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults(),
    response: {authenticatorData: b64.enc(c.response.authenticatorData), clientDataJSON: b64.enc(c.response.clientDataJSON),
               signature: b64.enc(c.response.signature), userHandle: c.response.userHandle ? b64.enc(c.response.userHandle) : null}});

  async function register(prefix, name, extra) {
    const {cid, options} = await post(prefix + '/options', extra);
    const cred = await navigator.credentials.create({publicKey: creation(options)});
    return post(prefix + '/verify', {cid, name, credential: regJSON(cred)});
  }
  async function signIn(next) {
    const {cid, options} = await post('/auth/passkey/login/options');
    const cred = await navigator.credentials.get({publicKey: request(options)});
    const r = await post('/auth/passkey/login/verify', {cid, credential: authJSON(cred)});
    const here = p => { try { const u = new URL(p || '/', location.origin); return u.origin === location.origin ? u.pathname + u.search : '/'; } catch (e) { return '/'; } };
    location.href = r.next === '/unlock' ? '/unlock?next=' + encodeURIComponent(here(next)) : here(next);
  }

  if (!window.PublicKeyCredential) return;                       // no passkey support: the buttons stay hidden
  document.querySelectorAll('[data-passkey]').forEach(btn => {
    btn.hidden = false;
    btn.addEventListener('click', async () => {
      btn.disabled = true; say('');
      try {
        const kind = btn.dataset.passkey;
        if (kind === 'login') await signIn(btn.dataset.next);
        else if (kind === 'add') {
          const field = btn.dataset.nameFrom && document.getElementById(btn.dataset.nameFrom);
          await register('/account/passkeys', field ? field.value : '');
          say('Passkey added.'); if (field) location.href = '/account?flash=Passkey+added.';
        } else if (kind === 'link') {
          const code = document.getElementById(btn.dataset.codeFrom).value;
          await register('/auth/link', 'This device', {code});
          say('Passkey added. Signing you in…'); await signIn('/');
        }
      } catch (e) { say(e.name === 'NotAllowedError' ? 'Cancelled.' : e.message); }
      btn.disabled = false;
    });
  });
})();
