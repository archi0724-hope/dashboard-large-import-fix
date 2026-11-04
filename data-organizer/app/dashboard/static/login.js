"use strict";
const byId = id => document.getElementById(id);
async function signIn(path, body) {
  const response = await fetch('/api/admin/' + path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.detail || 'Unable to sign in. Please try again.');
  location.assign('/');
}
byId('toggle').onclick = () => {
  const show = byId('password').type === 'password';
  byId('password').type = show ? 'text' : 'password';
  byId('toggle').setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  byId('toggle').setAttribute('aria-pressed', String(show));
};
byId('forgot').onclick = () => {
  const panel = byId('recovery'); panel.hidden = !panel.hidden;
  byId('forgot').setAttribute('aria-expanded', String(!panel.hidden));
};
byId('login').onsubmit = async event => {
  event.preventDefault(); const button = event.target.querySelector('[type=submit]'); button.disabled = true;
  byId('message').textContent = '';
  try { await signIn('login', {email:byId('email').value, password:byId('password').value}); }
  catch(e) { byId('message').textContent = e.message; byId('password').value = ''; button.disabled = false; }
};
byId('viewer').onclick = async () => {
  try { await signIn('viewer', {}); } catch(e) { byId('message').textContent = e.message; }
};
