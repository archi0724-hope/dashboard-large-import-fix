/* Shared administrator menu. Credentials are submitted only to the server. */
"use strict";
(async () => {
  const request = async (path, body) => {
    const response = await fetch('/api/admin/' + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || 'Login failed');
    return data;
  };
  let session;
  try { session = await request('session'); } catch { session = {admin: false, configured: false}; }
  window.dashboardAdmin = session.admin;
  document.documentElement.dataset.access = session.admin ? 'admin' : 'viewer';
  const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = '/static/access.css?v=1'; document.head.append(style);
  // Each embedded page still applies its own presentation controls.
  if (window.top !== window) return;
  const bar = document.createElement('div'); bar.className = 'account-toolbar';
  bar.innerHTML = '<span class="account-role"></span><button type="button" class="account-avatar" aria-label="Admin account" aria-expanded="false" title="Admin account">A<span></span></button>';
  document.body.append(bar);
  bar.querySelector('.account-role').textContent = session.admin ? 'Admin' : 'View only';
  const panel = document.createElement('div'); panel.className = 'account-panel'; panel.hidden = true; panel.id = 'admin-panel';
  panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Admin account');
  const avatar = bar.querySelector('button'); avatar.setAttribute('aria-controls', panel.id);
  if (session.admin) {
    panel.innerHTML = '<h2>Admin account</h2><p>You can update shared data and workspace settings.</p><button type="button" id="admin-logout">Sign out of admin</button><p class="account-error" role="alert"></p>';
    panel.querySelector('button').onclick = async () => {
      try { await request('logout', {}); location.reload(); } catch(e) { panel.querySelector('.account-error').textContent = e.message; }
    };
  } else {
    panel.innerHTML = '<h2>Admin sign in</h2><p>Everyone can browse, search and download. Only the administrator can make changes.</p><form><label>Email address<input name="email" type="email" autocomplete="username" required maxlength="254"></label><label>Password<input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label><p class="account-error" role="alert"></p><button type="submit">Sign in</button></form>';
    if (!session.configured) {
      panel.querySelector('.account-error').textContent = 'Admin login has not been configured on this server.';
      panel.querySelector('button').disabled = true;
    }
    panel.querySelector('form').onsubmit = async event => {
      event.preventDefault(); const form = event.target; const button = form.querySelector('button'); button.disabled = true;
      try { await request('login', {email: form.elements.email.value, password: form.elements.password.value}); location.reload(); }
      catch(e) { form.querySelector('.account-error').textContent = e.message; form.elements.password.value = ''; button.disabled = false; }
    };
  }
  bar.append(panel);
  const close = () => { panel.hidden = true; avatar.setAttribute('aria-expanded', 'false'); };
  avatar.onclick = () => { panel.hidden = !panel.hidden; avatar.setAttribute('aria-expanded', String(!panel.hidden)); if (!panel.hidden) panel.querySelector('input,button')?.focus(); };
  document.addEventListener('click', event => { if (!bar.contains(event.target)) close(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !panel.hidden) { close(); avatar.focus(); } });
})();
