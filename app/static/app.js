'use strict';
/* ContextOS UI. No framework, no inline handlers. Every piece of data from the server (Slack text included) is put into
   the page with textContent / createTextNode through h(), never innerHTML, so message text cannot inject markup. */

// ------------------------------------------------------------------ helpers ---
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k.startsWith('aria-') && typeof v === 'boolean') { el.setAttribute(k, String(v)); continue; }
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (s, r = document) => r.querySelector(s);
const state = { settingsTab: 'connections', expand: false, csrf: null, user: null, config: {}, tab: 'assistant', timers: [], levelsInfo: null, filter: 'important', source: '', query: '', eventCursor: 0, stageFilter: null, events: [] };

class ApiError extends Error {
  constructor(status, code, message, retryAfter) { super(message); this.status = status; this.code = code; this.retryAfter = retryAfter; }
}

async function api(path, { method = 'GET', body, quiet401 = false } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  let res;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 45000);  // a hung server must not freeze the page forever
  try {
    res = await fetch(path, { method, headers, credentials: 'same-origin', signal: ctl.signal, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new ApiError(0, 'network', e && e.name === 'AbortError' ? 'The server took too long to answer. Please try again.' : "Can't reach the server. Check your connection and try again.");
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON */ }
  if (res.ok) return data;
  const d = (data && data.detail) || {};
  const retry = d.retry_after || parseInt(res.headers.get('Retry-After') || '0', 10) || 0;
  const err = new ApiError(res.status, d.code || 'error', d.message || (typeof d === 'string' ? d : 'Something went wrong. Please try again.'), retry);
  if (res.status === 401 && !quiet401 && state.user) { sessionEnded(); }
  throw err;
}

async function streamAssistant(question, onToken, history = [], onSources = () => {}) {
  const headers = { Accept: 'text/event-stream', 'Content-Type': 'application/json' };
  if (state.csrf) headers['X-CSRF-Token'] = state.csrf;
  let res;
  try {
    res = await fetch('/assistant/ask/stream', {
      method: 'POST', headers, credentials: 'same-origin', body: JSON.stringify({ question, history: history.slice(-8) }),
    });
  } catch {
    throw new ApiError(0, 'network', "Can't reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { /* empty or non-JSON */ }
    const detail = (data && data.detail) || {};
    if (res.status === 401 && state.user) sessionEnded();
    throw new ApiError(res.status, detail.code || 'error',
      detail.message || (typeof detail === 'string' ? detail : 'Something went wrong. Please try again.'),
      detail.retry_after || 0);
  }
  if (!res.body) throw new ApiError(0, 'stream_unavailable', 'Your browser could not open the assistant response stream.');

  const reader = res.body.getReader(), decoder = new TextDecoder();
  let buffer = '', result = null;
  function dispatch(frame) {
    let event = 'message', data = '';
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).trimStart();
    }
    if (!data) return;
    const payload = JSON.parse(data);
    if (event === 'sources') {
      if (payload && Array.isArray(payload.sources)) onSources(payload.sources);
    } else if (event === 'token') {
      if (payload && typeof payload.text === 'string') onToken(payload.text);
    } else if (event === 'done') {
      if (payload && typeof payload === 'object') result = payload;
    } else if (event === 'error') {
      throw new ApiError(0, 'assistant_stream', payload && typeof payload.message === 'string'
        ? payload.message : 'The assistant could not finish this response. Please try again.');
    }
  }
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let boundary;
    while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + (buffer.startsWith('\r\n', boundary) ? 4 : 2));
      dispatch(frame);
    }
    if (done) break;
  }
  if (buffer.trim()) dispatch(buffer);
  if (!result) throw new ApiError(0, 'assistant_stream', 'The assistant response ended unexpectedly. Please try again.');
  return result;
}

function toast(msg, ms = 4500) {
  const t = h('div', { class: 'toast', role: 'status' }, msg);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), ms);
}
function ago(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function when(iso) { return iso ? new Date(iso).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''; }
function clearTimers() { state.timers.forEach(clearInterval); state.timers = []; }
function every(ms, fn) { state.timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); }
function busy(btn, on, label) {
  if (!btn) return;
  btn.disabled = on; btn.classList.toggle('is-busy', on);
  if (on) { btn.dataset.l = btn.textContent; btn.textContent = ''; btn.append(h('span', { class: 'spin' }), ' ', label || 'Working…'); }
  else if (btn.dataset.l != null) { btn.textContent = btn.dataset.l; }
}
function modal(title, build, { wide = false } = {}) {
  const dlg = h('dialog', { class: wide ? 'wide' : '', 'aria-label': title });
  const close = () => { dlg.close(); dlg.remove(); };
  dlg.append(h('div', { class: 'in' }, h('div', { class: 'top' }, h('h2', {}, title), h('button', { class: 'btn sm ghost', type: 'button', onclick: close, 'aria-label': 'Close' }, '✕')), build(close)));
  dlg.addEventListener('cancel', () => setTimeout(() => dlg.remove(), 0));
  document.body.append(dlg);
  dlg.showModal();
  return close;
}

// ---------------------------------------------------------------- validation ---
const EMAIL_LOCAL = /^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$/;
const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
function checkEmail(raw) { // mirrors app/auth.py validate_email so users get the same answer before they submit
  const e = (raw || '').trim().toLowerCase();
  if (!e) return 'Enter your email address.';
  if (e.length > 254 || /[^\x21-\x7e]/.test(e)) return "That doesn't look like an email address (no spaces or special characters).";
  if ((e.match(/@/g) || []).length !== 1) return 'An email address needs exactly one @, like name@example.com.';
  const [local, domain] = e.split('@');
  if (!local || local.length > 64 || !EMAIL_LOCAL.test(local)) return "The part before the @ isn't valid.";
  const labels = domain.split('.');
  if (labels.length < 2 || !labels.every((l) => LABEL.test(l))) return "The part after the @ isn't valid. It should look like example.com.";
  if (!/^([a-z]{2,24}|xn--[a-z0-9-]{2,59})$/.test(labels[labels.length - 1])) return 'The ending of the address (like .com) is not valid.';
  return '';
}
function passwordScore(pw, email) {
  let s = 0;
  if (pw.length >= 10) s++;
  if (pw.length >= 14) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw)) s++;
  if (/[^A-Za-z0-9]/.test(pw)) s++;
  const local = (email || '').split('@')[0].toLowerCase();
  if (local.length >= 4 && pw.toLowerCase().includes(local)) s = Math.min(s, 1);
  if (new Set(pw).size < 5) s = Math.min(s, 1);
  return Math.min(4, s);
}

// ------------------------------------------------------------------- session ---
const REASONS = {
  rate_limited: 'Too many attempts. Please wait a moment and try again.', not_configured: 'Google sign-in is not set up on this server yet.',
  missing_state: 'Your sign-in expired. Please try again.', invalid_state: 'Your sign-in expired or was started in a different browser. Please try again.',
  exchange_failed: 'Google did not accept the sign-in. Please try again.', network: 'Could not reach Google. Please try again.',
  bad_token: 'Google returned something unexpected. Please try again.', bad_issuer: 'Google returned something unexpected. Please try again.',
  bad_audience: 'This sign-in was meant for a different app. Please try again.', expired: 'The sign-in took too long. Please try again.',
  bad_nonce: 'Your sign-in could not be verified. Please try again.', bad_subject: 'Google returned something unexpected. Please try again.',
  email_unverified: 'Your Google email address is not verified, so ContextOS cannot sign you in with it.',
  bad_email: "Google's email address for this account isn't usable here.", account_conflict: 'That email is already linked to a different Google account.',
  account_disabled: 'This account is disabled.', missing_code: 'Google did not send a sign-in code. Please try again.',
};
const SLACK_REASONS = {
  access_denied: 'You cancelled on Slack. Nothing was connected.', invalid_state: 'That connection link expired. Please start again.', missing_state: 'That connection link expired. Please start again.',
  not_configured: 'Slack is not set up on this server yet (the admin needs SLACK_CLIENT_ID / SECRET).', identity_mismatch: 'This ContextOS account is already linked to a different Slack user in that workspace.',
  invalid_team_for_non_distributed_token: 'This Slack app is not installed or distributed for that workspace.', bad_redirect_uri: 'The redirect URL in the Slack app settings does not match this site.',
};

const GMAIL_REASONS = {
  access_denied: 'You cancelled on Google. Nothing was connected.', invalid_state: 'That connection link expired. Please start again.',
  scope_denied: 'Gmail access was not ticked on the Google screen, so nothing was connected.', no_refresh_token: 'Google did not give long-term access. Remove ContextOS at myaccount.google.com/permissions and connect again.',
  not_configured: 'Google is not set up on this server yet.', not_logged_in: 'Please sign in to ContextOS first.',
};
const CALENDAR_REASONS = {
  access_denied: 'You cancelled on Google. Nothing was connected.', invalid_state: 'That Calendar connection link expired. Please start again.',
  scope_denied: 'Read-only Calendar access was not granted, so nothing was connected.', no_refresh_token: 'Google did not give long-term access. Remove ContextOS at myaccount.google.com/permissions and connect again.',
  not_configured: 'Google sign-in is not set up on this server yet.', encryption_not_configured: 'Calendar token encryption is not configured on this server.',
  not_logged_in: 'Please sign in to ContextOS first.', missing_code: 'Google did not return a connection code. Please try again.',
  exchange_failed: 'Google could not complete the Calendar connection. Check the authorised redirect URI and try again.',
  bad_identity: 'Google could not verify the connected account. Please try again.', invalid_response: 'Google returned an unexpected response. Please try again.',
  network: 'Could not reach Google. Please try again.',
};

function sessionEnded() {
  state.user = null; state.csrf = null; clearTimers();
  renderAuth({ notice: { kind: 'info', text: 'Your session ended, so we signed you out. Please sign in again.' } });
}

async function boot() {
  const params = new URLSearchParams(location.search);
  const flash = { login: params.get('login'), reason: params.get('reason'), wait: params.get('wait'), slack: params.get('slack'), gmail: params.get('gmail'), googleCalendar: params.get('google_calendar'), isNew: params.get('new') };
  if (location.search) history.replaceState(null, '', location.pathname + location.hash);
  try { state.config = await api('/api/config', { quiet401: true }); } catch (e) { return fatal(e); }
  let me;
  try { me = await api('/auth/me', { quiet401: true }); } catch (e) { return fatal(e); }
  if (!me.authenticated) {
    let notice = null;
    if (flash.login === 'error') notice = { kind: 'err', text: (REASONS[flash.reason] || 'Sign-in failed. Please try again.') + (flash.wait ? ` (try again in ${flash.wait}s)` : '') };
    else if (flash.login === 'denied') notice = { kind: 'info', text: 'You cancelled the Google sign-in. You can try again any time.' };
    return renderAuth({ notice });
  }
  state.user = me.user; state.csrf = me.csrf_token;
  const tab = (location.hash || '').replace('#', '');
  if (visibleTabs().some((t) => t[0] === tab)) state.tab = tab;
  if (flash.slack === 'connected') { state.tab = landingAfterConnect(); renderShell(); toast('Slack connected. Importing your recent messages now. They will appear in your inbox in a moment.', 7000); return; }
  if (flash.slack === 'denied' || flash.slack === 'error') { state.tab = 'settings'; renderShell(); toast(SLACK_REASONS[flash.reason] || 'Slack could not be connected. Please try again.', 8000); return; }
  if (flash.gmail === 'connected') { state.tab = landingAfterConnect(); renderShell(); toast('Gmail connected (read-only). Importing the last 14 days now.', 7000); return; }
  if (flash.gmail === 'denied' || flash.gmail === 'error') { state.tab = 'settings'; renderShell(); toast(GMAIL_REASONS[flash.reason] || 'Gmail could not be connected. Please try again.', 9000); return; }
  if (flash.googleCalendar === 'connected') { state.tab = 'assistant'; renderShell(); toast('Google Calendar connected (read-only). Ask about today’s schedule.', 7000); return; }
  if (flash.googleCalendar === 'denied' || flash.googleCalendar === 'error') { state.tab = 'settings'; renderShell(); toast(CALENDAR_REASONS[flash.reason] || 'Google Calendar could not be connected. Please try again.', 9000); return; }
  if (flash.login === 'ok' && flash.isNew) toast('Welcome to ContextOS! Let’s connect your Slack.');
  renderShell();
}
function fatal(e) { $('#app').replaceChildren(h('div', { class: 'boot' }, h('p', {}, e.message), h('button', { class: 'btn', onclick: () => location.reload() }, 'Retry'))); }

// --------------------------------------------------------------------- login ---
function renderAuth({ notice } = {}) {
  clearTimers();
  document.title = 'Sign in · ContextOS';
  let mode = state.config.registration_open ? (location.hash === '#signup' ? 'register' : 'login') : 'login';
  const root = h('div', { class: 'auth' });
  const side = h('div', { class: 'auth-side' }, h('div', { class: 'brand' }, h('i', {}, 'C'), 'ContextOS'),
    h('h1', {}, 'Your Slack, sorted by what actually matters.'),
    h('ul', {}, h('li', {}, h('b', {}, 'Priority inbox'), 'Urgent first. Chatter fades away.'),
      h('li', {}, h('b', {}, 'Projects, found for you'), 'We notice what you keep discussing and keep a running summary.'),
      h('li', {}, h('b', {}, 'Replies you owe'), 'Never lose track of who is waiting on you.'),
      h('li', {}, h('b', {}, 'Private by design'), 'Your data is yours alone. Delete it any time.')));
  const card = h('div', { class: 'card' });
  root.append(side, h('div', { class: 'auth-main' }, card));
  $('#app').replaceChildren(root);

  function draw() {
    const g = state.config.google_login, pw = state.config.password_auth;
    const parts = [h('div', { class: 'brand' }, h('i', {}, 'C'), 'ContextOS'), h('h2', { style: 'margin-top:14px' }, mode === 'login' ? 'Welcome back' : 'Create your account')];
    if (notice) parts.push(h('div', { class: `banner ${notice.kind}`, role: 'alert' }, notice.text));
    if (g) parts.push(h('a', { class: 'btn block', href: '/auth/google/start', style: 'margin-top:14px' }, googleLogo(), 'Continue with Google'));
    else parts.push(h('div', { class: 'banner info' }, 'Google sign-in is not configured on this server yet' + (pw ? ', so use email below.' : '.')));
    if (g && pw) parts.push(h('div', { class: 'divider' }, 'or use email'));
    if (pw) parts.push(passwordForm());
    parts.push(h('p', { class: 'small muted', style: 'margin-top:16px' }, 'By continuing you agree that ContextOS reads the Slack conversations you choose, only to organise them for you.'));
    card.replaceChildren(...parts);
  }

  function passwordForm() {
    const email = h('input', { class: 'input', id: 'email', type: 'email', autocomplete: 'username', inputmode: 'email', autocapitalize: 'off', spellcheck: 'false', required: true, maxlength: '254' });
    const emailHint = h('div', { class: 'hint', id: 'email-hint', 'aria-live': 'polite' });
    const pw = h('input', { class: 'input', id: 'password', type: 'password', autocomplete: mode === 'login' ? 'current-password' : 'new-password', required: true, maxlength: '200' });
    const pwHint = h('div', { class: 'hint', id: 'pw-hint' });
    const meter = h('div', { class: 'meter', hidden: mode === 'login' }, h('i', {}));
    const name = h('input', { class: 'input', id: 'name', autocomplete: 'name', maxlength: '200' });
    const show = h('button', { type: 'button', 'aria-label': 'Show password', onclick: () => { const t = pw.type === 'password'; pw.type = t ? 'text' : 'password'; show.textContent = t ? 'Hide' : 'Show'; } }, 'Show');
    const err = h('div', { class: 'banner err', role: 'alert', hidden: true });
    const submit = h('button', { class: 'btn primary block', type: 'submit' }, mode === 'login' ? 'Sign in' : 'Create account');
    let touched = false, locked = null;

    function validateEmailField(show) {
      const m = checkEmail(email.value);
      email.setAttribute('aria-invalid', m && show ? 'true' : 'false');
      emailHint.className = 'hint' + (m && show ? ' bad' : !m && email.value ? ' good' : '');
      emailHint.textContent = m && show ? m : !m && email.value ? 'Looks good' : '';
      return !m;
    }
    function validatePw() {
      if (mode === 'login') { pwHint.textContent = ''; return pw.value.length > 0; }
      const sc = passwordScore(pw.value, email.value);
      const bar = meter.firstChild; bar.style.width = `${(sc / 4) * 100}%`; bar.style.background = ['#c73927', '#c73927', '#f2a21b', '#0a8a76', '#0a8a76'][sc];
      const short = pw.value.length < 10;
      pwHint.className = 'hint' + (pw.value ? (short ? ' bad' : ' good') : '');
      pwHint.textContent = !pw.value ? 'At least 10 characters. A few random words works well.' : short ? `${10 - pw.value.length} more character(s) needed.` : ['', 'Weak: avoid common words or your email name.', 'Okay', 'Strong', 'Very strong'][sc] || 'Okay';
      return !short;
    }
    email.addEventListener('input', () => { if (touched) validateEmailField(true); validatePw(); });
    email.addEventListener('blur', () => { touched = true; validateEmailField(true); });
    pw.addEventListener('input', validatePw);

    const form = h('form', { novalidate: true, onsubmit: async (ev) => {
      ev.preventDefault();
      if (locked) return;
      err.hidden = true; touched = true;
      const okE = validateEmailField(true), okP = validatePw();
      if (!okE) { email.focus(); return; }
      if (!okP) { pw.focus(); return; }
      busy(submit, true, mode === 'login' ? 'Signing in…' : 'Creating…');
      try {
        const body = mode === 'login' ? { email: email.value, password: pw.value } : { email: email.value, password: pw.value, name: name.value };
        const out = await api(mode === 'login' ? '/auth/login' : '/auth/register', { method: 'POST', body, quiet401: true });
        state.user = out.user; state.csrf = out.csrf_token;
        pw.value = '';
        renderShell();
        if (mode === 'register') toast('Welcome to ContextOS! Let’s connect your Slack.');
      } catch (e) {
        busy(submit, false);
        err.hidden = false; err.textContent = e.message;
        if (e.code === 'bad_email') { email.focus(); }
        if (e.status === 429 && e.retryAfter) countdown(e.retryAfter, e.message);
        else if (e.code === 'bad_credentials') { pw.select(); }
      }
    } },
    mode === 'register' ? h('div', { class: 'field' }, h('label', { for: 'name' }, 'Your name (optional)'), name) : null,
    h('div', { class: 'field' }, h('label', { for: 'email' }, 'Email'), email, emailHint),
    h('div', { class: 'field' }, h('label', { for: 'password' }, 'Password'), h('div', { class: 'pw' }, pw, show), meter, pwHint),
    err, submit);

    function countdown(sec, base) {
      locked = true; submit.disabled = true;
      const tick = () => {
        if (sec <= 0) { locked = null; submit.disabled = false; err.hidden = true; return; }
        err.hidden = false; err.textContent = `${base.replace(/\s*Try again.*$/i, '')} You can try again in ${sec}s.`; sec -= 1; setTimeout(tick, 1000);
      };
      tick();
    }
    const tabs = state.config.registration_open ? h('div', { class: 'tabs2', role: 'tablist' },
      h('button', { type: 'button', role: 'tab', 'aria-selected': mode === 'login', onclick: () => { mode = 'login'; draw(); } }, 'Sign in'),
      h('button', { type: 'button', role: 'tab', 'aria-selected': mode === 'register', onclick: () => { mode = 'register'; draw(); } }, 'Create account')) : null;
    return h('div', {}, tabs, form);
  }
  draw();
}
function googleLogo() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 48 48'); svg.setAttribute('width', '18'); svg.setAttribute('height', '18'); svg.setAttribute('aria-hidden', 'true');
  for (const [d, c] of [['M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 8 3l5.7-5.7C34 6.1 29.3 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.4-.4-3.5z', '#FFC107'], ['M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 8 3l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z', '#FF3D00'], ['M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z', '#4CAF50'], ['M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z', '#1976D2']]) {
    const p = document.createElementNS(ns, 'path'); p.setAttribute('d', d); p.setAttribute('fill', c); svg.append(p);
  }
  return svg;
}

// --------------------------------------------------------------------- shell ---
const TABS = [['assistant', '✦', 'Assistant'], ['today', '☀', 'Inbox'], ['projects', '◧', 'Projects'], ['people', '☺', 'People'], ['pipeline', '⇄', 'Pipeline', true], ['settings', '⚙', 'Settings']];
// The Pipeline page (live stage log, queue, service status) is for administrators only; the server enforces it too.
function visibleTabs() { return TABS.filter((t) => !t[3] || (state.user && state.user.is_admin)); }
function landingAfterConnect() { return state.user && state.user.is_admin ? 'pipeline' : 'today'; }
function renderShell() {
  clearTimers();
  const nav = h('nav', { class: 'nav', 'aria-label': 'Main' }, h('div', { class: 'brand' }, h('i', {}, 'C'), 'ContextOS'),
    visibleTabs().map(([id, ico, label]) => h('button', { type: 'button', 'aria-current': state.tab === id ? 'page' : null, onclick: () => go(id) }, h('span', { 'aria-hidden': 'true' }, ico), label)),
    h('div', { class: 'grow' }), h('div', { class: 'me' }, h('b', {}, state.user.name || 'Your account'), h('span', {}, state.user.email),
      h('button', { class: 'signout-link', type: 'button', onclick: signOut }, 'Sign out')));
  const main = h('main', { class: state.tab === 'assistant' ? 'main assistant-main' : 'main', id: 'view' });
  $('#app').replaceChildren(h('div', { class: 'shell' }, nav, main));
  if (!visibleTabs().some((t) => t[0] === state.tab)) state.tab = 'assistant';
  document.title = `${TABS.find((t) => t[0] === state.tab)[2]} · ContextOS`;
  ({ today: viewToday, projects: viewProjects, people: viewPeople, assistant: viewAssistant, pipeline: viewPipeline, settings: viewSettings })[state.tab](main).catch((e) => main.replaceChildren(h('div', { class: 'banner err' }, e.message)));
}
async function loadSampleData(ev) {
  const btn = ev && ev.currentTarget;
  busy(btn, true, 'Loading…');
  try {
    const r = await api('/api/demo/seed?size=full', { method: 'POST' });
    const admin = state.user && state.user.is_admin;
    toast(`Sample data loading: Slack + ${r.emails_ingested ?? 0} emails are being processed${admin ? '. Opening the pipeline…' : '. Give it a minute, then check Inbox, Projects and People.'}`, 6000);
    go(admin ? 'pipeline' : 'today');
  } catch (e) { toast(e.message); } finally { busy(btn, false); }
}
function go(tab) { state.tab = tab; location.hash = tab; renderShell(); }
function openInbox(filter = 'all', source = '') {
  state.filter = filter; state.source = source; state.query = '';
  go('today');
}
async function signOut() {
  try {
    await api('/auth/logout', { method: 'POST' });
    state.user = null; state.csrf = null; clearTimers();
    renderAuth({ notice: { kind: 'ok', text: 'You are signed out.' } });
  } catch (e) {
    toast(`Could not sign out: ${e.message}`);
  }
}

function header(title, sub, ...actions) { return h('div', { class: 'top' }, h('div', {}, h('h1', {}, title), sub ? h('div', { class: 'sub' }, sub) : null), h('div', { class: 'row' }, actions)); }
const isAdmin = () => !!(state.user && state.user.is_admin);
function cleanAssistantText(text) { return (text || '').replace(/\s*\[#\d*\]?/g, '').replace(/\[\d{0,2}$/, '').replace(/\bnullnull\b/gi, '').trim(); }
function initials(name) { const p = (name || '?').replace(/[^\p{L}\p{N} ]/gu, '').trim().split(/\s+/); return ((p[0] || '?')[0] + (p.length > 1 ? p[p.length - 1][0] : '')).toUpperCase(); }
const HUES = [12, 28, 150, 190, 215, 262, 320, 345];
function hueFor(name) { let x = 0; for (const c of name || '') x = (x * 31 + c.charCodeAt(0)) >>> 0; return HUES[x % HUES.length]; }
function avatar(name, size = 38) { return h('span', { class: 'avatar', 'aria-hidden': 'true', style: `width:${size}px;height:${size}px;background:hsl(${hueFor(name)} 55% 42%)` }, initials(name)); }
/* Turn "Priya needs the deck [2]." into text + clickable numbered chips, and "- item" lines into a list. Text only, never innerHTML. */
function richAnswer(text, sources, onCite) {
  const chip = (n) => {
    const src = (sources || []).find((x) => x.n === n);
    return src ? h('button', { class: 'cite-chip', type: 'button', title: src.kind === 'summary' ? `Project: ${src.title}` : `${src.author || 'Message'} · ${src.channel_name || ''}`, 'aria-label': `Source ${n}`, onclick: () => onCite(src) }, String(n)) : null;
  };
  const inline = (line) => {
    const out = []; let last = 0; const re = /\[(\d{1,2})\]/g; let m;
    while ((m = re.exec(line))) { out.push(line.slice(last, m.index).replace(/\s+$/, '')); const c = chip(+m[1]); if (c) out.push(c); last = m.index + m[0].length; }
    out.push(line.slice(last));
    return out;
  };
  const box = h('div', { class: 'answer-text' }); let list = null;
  for (const raw of (text || '').split('\n')) {
    const bullet = /^\s*[-•*]\s+(.*)$/.exec(raw);
    if (bullet) { if (!list) { list = h('ul', { class: 'answer-list' }); box.append(list); } list.append(h('li', {}, ...inline(bullet[1]))); continue; }
    list = null;
    if (raw.trim()) box.append(h('p', {}, ...inline(raw)));
  }
  return box;
}
function sourceCard(src, onOpen) {
  const isSummary = src.kind === 'summary';
  const label = isSummary ? `Project · ${src.title}` : `${src.provider === 'gmail' ? 'Gmail' : 'Slack'} · ${src.channel_name || ''}`;
  return h('button', { class: 'source-card', type: 'button', onclick: () => onOpen(src) },
    h('span', { class: 'source-n' }, String(src.n)), h('span', { class: 'source-body' }, h('b', {}, label), h('span', {}, `${src.author ? src.author + ': ' : ''}${(src.text || '').slice(0, 110)}`)));
}
function openSource(src) { if (src.kind === 'summary') projectDialog(src.project_id, () => {}); else if (src.message_id) sourceDialog(src.message_id); }

// --------------------------------------------------------------------- today ---
async function viewToday(main) {
  const [conn, dash] = await Promise.all([api('/integrations/slack/connection'), api('/dashboard/summary')]);
  if (!conn.connections.length && !dash.messages_total) return main.replaceChildren(onboarding(conn));
  const inbox = h('div', {}), side = h('div', {});
  const refreshBtn = h('button', { class: 'btn', onclick: () => { load.lastSig = null; load(); } }, 'Refresh');
  const urgentCount = dash.urgent ?? 0;
  const summary = h('div', { class: 'attention-summary' },
    h('span', {}, `${dash.messages_total} ${dash.messages_total === 1 ? 'message' : 'messages'} in your inbox`),
    h('span', { class: 'summary-separator', 'aria-hidden': 'true' }, '·'),
    h('button', { type: 'button', class: 'text-action', onclick: () => { state.filter = 'urgent'; state.expand = false; load(); } }, `${urgentCount} urgent`),
    h('span', { class: 'summary-separator', 'aria-hidden': 'true' }, '·'),
    h('button', { type: 'button', class: 'text-action', onclick: () => { document.querySelector('#inbox-reminders')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); } }, `${dash.pending_reminders} reminders`));
  main.replaceChildren(header('Inbox', 'The conversations most likely to need your attention.', refreshBtn), summary, h('div', { class: 'grid2' }, inbox, side));
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search by words or meaning…', 'aria-label': 'Search messages', value: state.query, style: 'max-width:260px' });
  let t; search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { state.query = search.value.trim(); load(); }, 300); });
  const priorityFilters = h('div', { class: 'chips' }, [['urgent', 'Urgent'], ['high', 'High'], ['normal', 'Normal'], ['all', 'Everything']].map(([p, l]) => h('button', { class: 'chip', type: 'button', 'data-f': p, 'aria-pressed': state.filter === p, onclick: () => { state.filter = p; load(); } }, l)));
  const sourceFilters = h('div', { class: 'chips' }, [['', 'All sources'], ['slack', 'Slack'], ['gmail', 'Gmail']].map(([v, l]) => h('button', { class: 'chip', type: 'button', 'data-s': v, 'aria-pressed': state.source === v, onclick: () => { state.source = v; load(); } }, l)));
  const list = h('div', {});
  const listTitle = h('h2', {}, 'Needs attention');
  const listDescription = h('p', { class: 'list-description' }, 'One top-ranked message for each priority.');
  const resetPriority = h('button', { class: 'text-action', type: 'button', onclick: () => { state.filter = 'important'; load(); } }, 'Back to needs attention');
  resetPriority.hidden = true;
  inbox.append(h('section', { class: 'inbox-section' }, h('div', { class: 'hd' }, h('div', { class: 'inbox-title' }, listTitle, resetPriority), search),
    h('div', { class: 'filter-group' }, h('span', { class: 'filter-label' }, 'Priority'), priorityFilters),
    h('div', { class: 'filter-group' }, h('span', { class: 'filter-label' }, 'Source'), sourceFilters), listDescription, list));
  const awaiting = h('div', {}), rems = h('div', {}), sugg = h('div', {});
  let showAllWaiting = false;
  let waitingMessages = [];
  const waitingDetails = h('details', { class: 'side-section' }, h('summary', {}, 'Waiting on you'), awaiting);
  side.append(h('section', { class: 'side-section', id: 'inbox-reminders' }, h('h2', {}, 'Reminders'), rems), waitingDetails,
    h('details', { class: 'side-section' }, h('summary', {}, 'Meeting suggestions'), sugg));

  function renderWaiting() {
    const waitingRows = showAllWaiting ? waitingMessages : waitingMessages.slice(0, 3);
    awaiting.replaceChildren(...(waitingMessages.length ? waitingRows.map((m) => h('div', { class: 'waiting-item' },
      h('div', {}, h('b', {}, m.author_name || m.user_id || 'Someone'), h('span', { class: 'small muted' }, ` · ${m.channel_name} · ${ago(m.message_created_at)}`)),
      h('button', { class: 'text-action', type: 'button', onclick: () => sourceDialog(m.id) }, 'Open'))) : [h('div', { class: 'empty small' }, 'Nobody is waiting on you.')]),
      waitingMessages.length > 3 ? h('button', { class: 'text-action waiting-toggle', type: 'button', onclick: () => { showAllWaiting = !showAllWaiting; renderWaiting(); } }, showAllWaiting ? 'Show less' : 'View all') : null);
    waitingDetails.querySelector('summary').textContent = `Waiting on you (${waitingMessages.length})`;
  }

  async function load() {
    priorityFilters.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.f === state.filter)));
    sourceFilters.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.s === state.source)));
    listTitle.textContent = state.filter === 'urgent' ? 'Urgent' : state.filter === 'high' ? 'High priority' : state.filter === 'normal' ? 'Normal priority' : state.filter === 'all' ? 'All messages' : 'Needs attention';
    resetPriority.hidden = state.filter === 'important';
    listDescription.hidden = state.filter !== 'important';
    let aw, rm, sg;
    const qs = new URLSearchParams({ limit: state.filter === 'important' ? '4' : state.filter === 'urgent' && !state.expand ? '6' : '40', include_self: state.filter === 'all' ? 'true' : 'false' }); if (state.filter === 'urgent' && state.expand) qs.set('expand', 'true'); if (state.filter !== 'all') qs.set('priority', state.filter); if (state.source) qs.set('source', state.source);
    let msgs, searchNote = null;
    const calls = [api('/api/awaiting-reply'), api('/reminders'), api('/schedule/suggestions')];
    if (state.query) {
      const sq = new URLSearchParams({ q: state.query, limit: '40' }); if (state.filter !== 'all') sq.set('priority', state.filter); if (state.source) sq.set('source', state.source);
      const [sr, ...rest] = await Promise.all([api('/api/search?' + sq), ...calls]); msgs = sr.results; [aw, rm, sg] = rest;
      searchNote = !isAdmin() ? null : sr.semantic === 'on' ? 'Matched by words and by meaning (Qdrant).' : sr.semantic === 'no_hits' ? 'Nothing close in meaning; showing word matches.' : sr.semantic === 'off' ? 'Word matches only. Search by meaning needs Qdrant (ask your administrator).' : null;
    } else {
      const [mm, ...rest] = await Promise.all([api('/messages?' + qs), ...calls]); msgs = mm; [aw, rm, sg] = rest;
    }
    if (state.query && state.filter === 'important') {
      msgs = ['P0', 'P1', 'P2', 'P3'].map((p) => msgs.find((m) => m.priority_label === p)).filter(Boolean);
    }
    const sig = JSON.stringify([msgs.map((m) => [m.id, m.more_in_conversation]), searchNote, state.expand]);
    if (sig !== load.lastSig) {
      load.lastSig = sig;
      const total = msgs.length && msgs[0].urgent_total ? msgs[0].urgent_total : 0;
      list.replaceChildren(...(searchNote ? [h('div', { class: 'search-note' }, searchNote)] : []),
        ...(msgs.length ? msgs.map((m) => inboxMessageCard(m, load)) : [h('div', { class: 'empty' }, state.query || state.filter !== 'important' ? (state.filter === 'urgent' ? 'Nothing urgent. Enjoy the quiet.' : 'Nothing matches those filters.') : 'You’re all caught up. New important conversations will appear here.')]),
        ...(state.filter === 'urgent' && !state.expand && total > msgs.length ? [h('button', { class: 'text-action', type: 'button', style: 'margin-top:12px', onclick: () => { state.expand = true; load(); } }, `Show all ${total} urgent conversations`)] : []));
    }
    waitingMessages = aw;
    renderWaiting();
    rems.replaceChildren(...(rm.length ? rm.slice(0, 3).map((r) => h('div', { class: 'item' }, h('div', {}, h('div', {}, r.title.slice(0, 110)), h('div', { class: 'small muted' }, 'Due ' + when(r.due_at))),
      h('button', { class: 'btn sm', onclick: async () => { await api(`/reminders/${r.id}/complete`, { method: 'POST' }); load(); } }, 'Done'))) : [h('div', { class: 'empty small' }, 'No pending reminders.')]));
    sugg.replaceChildren(...(sg.length ? sg.slice(0, 3).map((e) => h('div', { class: 'item' }, h('div', {}, h('div', {}, e.title.slice(0, 100)), h('div', { class: 'small muted' }, when(e.proposed_start) + (e.is_provisional ? ' · time guessed' : ''))),
      h('button', { class: 'btn sm', onclick: async () => { await api(`/schedule/suggestions/${e.id}/approve`, { method: 'POST' }); toast('Approval recorded'); load(); } }, 'Approve'))) : [h('div', { class: 'empty small' }, 'No meeting ideas.')]));
  }
  await load();
  every(30000, () => load().catch(() => {}));
}

const FEEDBACK = { not_urgent: ['Not urgent', 'Got it: less like this will reach Urgent.'], wrong_person: ['Wrong person', 'Thanks: I’ll treat this sender as less relevant to you.'],
  not_mine: ['Not mine', 'Thanks: I’ll treat this sender as less relevant to you.'], always_show: ['Always show', 'Pinned: this stays in Urgent until you mark it Done.'] };
async function triage(m, kind, reload, hours, fbLabel) {
  try {
    const r = kind === 'feedback' ? await api(`/messages/${m.id}/feedback`, { method: 'POST', body: { label: fbLabel } })
      : await api(`/messages/${m.id}/${kind}`, { method: 'POST', body: kind === 'snooze' ? { hours } : {} });
    const label = kind === 'feedback' ? FEEDBACK[fbLabel][1] : kind === 'handled' ? `Cleared ${r.cleared} message${r.cleared === 1 ? '' : 's'}.` : `Snoozed for ${hours >= 24 ? 'tomorrow' : hours + ' hours'}.`;
    const t = h('div', { class: 'toast' }, label, ' ', h('button', { class: 'toast-undo', type: 'button', onclick: async () => { t.remove(); try { await api('/messages/triage/undo', { method: 'POST', body: { ids: r.ids } }); reload.lastSig = null; reload(); } catch (e) { toast(e.message); } } }, 'Undo'));
    $('#toasts').append(t); setTimeout(() => t.remove(), 7000);
    reload.lastSig = null; reload();
  } catch (e) { toast(e.message); }
}
function inboxMessageCard(m, reload) {
  const priorityName = { P0: 'Urgent', P1: 'High priority', P2: 'Normal', P3: 'Normal' }[m.priority_label] || 'Important';
  return h('article', { class: `inbox-message ${m.priority_label}` },
    h('div', { class: 'inbox-message-heading' },
      h('span', { class: `priority-label ${m.priority_label}` }, priorityName),
      m.is_self ? h('span', { class: 'message-context' }, 'Sent by you') : null,
      h('span', { class: 'inbox-message-meta' }, `${m.provider === 'gmail' ? 'Gmail' : 'Slack'} · ${m.author_name || m.user_id || 'Unknown'} · ${ago(m.message_created_at)}`)),
    h('div', { class: 'inbox-message-channel' }, `${m.conv_type === 'im' || m.conv_type === 'email' ? '' : '#'}${m.channel_name}`, m.more_in_conversation ? h('span', { class: 'more-pill' }, `+${m.more_in_conversation} more here`) : null),
    h('p', { class: 'inbox-message-text' }, m.text),
    m.reason ? h('p', { class: 'why-line' }, m.reason) : null,
    h('div', { class: 'inbox-message-footer' },
      reload ? h('details', { class: 'not-right' }, h('summary', {}, 'Not right?'),
        h('div', { class: 'not-right-menu' }, Object.entries(FEEDBACK).map(([k, v]) => h('button', { class: 'chip', type: 'button', onclick: () => triage(m, 'feedback', reload, 0, k) }, v[0])))) : h('span', {}),
      h('div', { class: 'row triage-actions' },
        h('button', { class: 'text-action', type: 'button', onclick: () => sourceDialog(m.id) }, 'View'),
        reload ? h('button', { class: 'btn sm', type: 'button', title: m.more_in_conversation ? 'Clears this whole conversation' : 'Clear this message', onclick: () => triage(m, 'handled', reload) }, '✓ Done') : null,
        reload ? h('button', { class: 'btn sm ghost', type: 'button', onclick: () => triage(m, 'snooze', reload, 4) }, 'Snooze 4h') : null)));
}

function messageCard(m) {
  return h('article', { class: `msg ${m.priority_label}` },
    h('div', { class: 'row', style: 'justify-content:space-between' }, h('div', { class: 'row' }, h('span', { class: `pill ${m.priority_label}` }, m.priority_label), h('span', { class: 'pill src-' + (m.provider || 'slack') }, m.provider === 'gmail' ? '✉ Gmail' : '# Slack'), m.is_demo ? h('span', { class: 'pill demo' }, 'demo') : null, m.mentions_me ? h('span', { class: 'pill' }, '@you') : null, m.is_bot ? h('span', { class: 'pill' }, 'bot') : null),
      h('div', { class: 'row' }, m.match ? h('span', { class: `pill ${m.match === 'words' ? '' : 'ok'}`, title: m.similarity ? `similarity ${m.similarity}` : '' }, m.match === 'words' ? 'words' : m.match === 'both' ? 'words + meaning' : 'by meaning') : null,
        h('button', { class: 'btn sm ghost', onclick: () => relatedDialog(m) }, 'Related'),
        state.user && state.user.is_admin ? h('button', { class: 'btn sm ghost', onclick: () => journeyDialog(m) }, 'Pipeline trace') : null, m.provider === 'gmail' ? null : h('button', { class: 'btn sm', onclick: () => replyDialog(m) }, 'Reply'))),
    h('div', { class: 'meta' }, `${m.author_name || m.user_id || 'unknown'} · ${m.conv_type === 'im' || m.conv_type === 'email' ? '' : '#'}${m.channel_name} · ${ago(m.message_created_at)}`),
    h('div', { class: 'txt' }, m.text), m.reason ? h('div', { class: 'why' }, 'Why: ' + m.reason) : null);
}

function onboarding(conn) {
  const wrap = h('div', { style: 'max-width:760px;margin:20px auto' });
  const levelsBox = h('div', {});
  wrap.append(h('div', { class: 'card', style: 'width:100%' }, h('h1', {}, `Welcome${state.user.name ? ', ' + state.user.name.split(' ')[0] : ''} 👋`), h('p', { class: 'muted' }, 'Connect Slack and/or Gmail. Only what matters shows up, grouped by project.'),
    h('div', { class: 'steps' }, h('div', { class: 'step done' }, h('span', { class: 'n' }, '✓'), h('div', {}, h('b', {}, 'Signed in'), h('div', { class: 'muted small' }, state.user.email))),
      h('div', { class: 'step' }, h('span', { class: 'n' }, '2'), h('div', { style: 'flex:1' }, h('b', {}, 'Choose what ContextOS may read'), h('div', { class: 'muted small' }, 'You can change this any time in Settings.'), levelsBox)),
      h('div', { class: 'step' }, h('span', { class: 'n' }, '3'), h('div', {}, h('b', {}, 'Your workspace, kept up to date'), h('div', { class: 'muted small' }, 'Recent conversations are imported automatically and new messages appear in your inbox.'))))));
  levelPicker(levelsBox, conn.oauth_configured);
  return wrap;
}

function gmailCard(configured) {
  return h('div', { class: 'level', style: 'margin-top:12px;display:block' }, h('b', {}, '✉ Gmail (read-only)'), h('div', { class: 'muted small' }, 'Adds important emails to the same inbox and project folders. Promotions, newsletters and spam are skipped. ContextOS can never send, delete or label mail.'),
    configured ? h('a', { class: 'btn', style: 'margin-top:8px', href: '/integrations/gmail/connect' }, 'Connect Gmail') : h('div', { class: 'banner warn' }, isAdmin() ? 'Gmail needs GOOGLE_CLIENT_ID / SECRET on the server.' : 'Gmail isn’t available yet. Ask your administrator.'));
}

async function levelPicker(box, configured, current) {
  if (!state.levelsInfo) state.levelsInfo = await api('/integrations/slack/access-levels');
  const info = state.levelsInfo; let level = current || info.default;
  const desc = { essential: 'Only public channels you are in. Nothing private is ever touched. A good way to start.', recommended: 'Public + private channels, DMs and group DMs, read-only. This is what makes priorities and “waiting on you” accurate.', with_replies: 'Everything above, plus the ability to post a reply as you, only after you press Send on text you reviewed.' };
  const names = { essential: 'Public channels only', recommended: 'All my conversations (read-only)', with_replies: 'All my conversations + replies I approve' };
  box.replaceChildren(h('div', { class: 'levels', role: 'radiogroup', 'aria-label': 'Slack access level' }, info.levels.map((l) => h('label', { class: 'level' },
    h('input', { type: 'radio', name: 'lvl', value: l.id, checked: l.id === level, onchange: () => { level = l.id; } }),
    h('div', {}, h('b', {}, names[l.id] || l.label), l.id === info.default ? h('span', { class: 'pill ok', style: 'margin-left:8px' }, 'Recommended') : null, h('div', { class: 'muted small' }, desc[l.id]), h('div', { class: 'small muted' }, 'Slack permissions: ' + l.scopes.join(', ')))))),
    current ? null : gmailCard(state.config.google_login),
    h('ul', { class: 'never' }, h('li', {}, 'We never post, edit or delete anything unless you press Send.'), h('li', {}, 'Tokens are encrypted; you can disconnect and erase everything in one click.'), h('li', {}, 'Your messages are visible only to you.')),
    configured ? h('a', { class: 'btn primary', style: 'margin-top:12px', href: '#', onclick: (e) => { e.preventDefault(); location.href = `/integrations/slack/connect?level=${encodeURIComponent(level)}`; } }, 'Continue to Slack') : h('div', { class: 'banner warn' }, isAdmin() ? 'Slack needs SLACK_CLIENT_ID / SLACK_CLIENT_SECRET on the server.' : 'Slack isn’t available yet. Ask your administrator.'));
}

// ------------------------------------------------------------- reply/journey ---
function replyDialog(m, initialText = '') {
  const key = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random();
  modal('Reply in Slack', (close) => {
    const text = h('textarea', { class: 'input', maxlength: '4000', 'aria-label': 'Reply text', value: initialText }), ok = h('input', { type: 'checkbox', id: 'appr' });
    const err = h('div', { class: 'banner err', hidden: true, role: 'alert' }), send = h('button', { class: 'btn primary', type: 'button', disabled: true }, 'Send reply');
    const sync = () => { send.disabled = !(ok.checked && text.value.trim()); };
    text.addEventListener('input', sync); ok.addEventListener('change', sync);
    send.addEventListener('click', async () => {
      busy(send, true, 'Sending…'); err.hidden = true;
      try { const r = await api(`/messages/${m.id}/reply`, { method: 'POST', body: { text: text.value.trim(), approved: true, client_request_id: key } }); toast(r.simulated ? 'Demo workspace: nothing was sent to Slack.' : 'Reply sent.'); close(); }
      catch (e) { busy(send, false); sync(); err.hidden = false; err.textContent = e.message; }
    });
    return h('div', {}, h('div', { class: 'msg ' + m.priority_label }, h('div', { class: 'meta' }, `${m.author_name || 'someone'} · ${m.channel_name}`), h('div', { class: 'txt' }, m.text.slice(0, 400))),
      h('div', { class: 'field' }, h('label', { for: 'rt' }, 'Your reply'), text), h('label', { class: 'row small', style: 'margin:10px 0' }, ok, 'I reviewed this text. Send it as me, in this thread.'), err, h('div', { class: 'row' }, send, h('button', { class: 'btn', type: 'button', onclick: close }, 'Cancel')));
  });
}
function channelPostDialog(channels, initialText = '') {
  const key = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random();
  modal('Post in a Slack channel', (close) => {
    const available = channels.filter((channel) => channel.can_post);
    const channel = h('select', { class: 'input', 'aria-label': 'Slack channel' },
      ...available.map((item) => h('option', {
        value: `${item.connection_id}:${item.channel_id}`,
        selected: !!item.about_recipient,
      }, `#${item.channel_name}`)));
    const text = h('textarea', { class: 'input', maxlength: '4000', 'aria-label': 'Message text', value: initialText });
    const approval = h('input', { type: 'checkbox' });
    const error = h('div', { class: 'banner err', hidden: true, role: 'alert' });
    const send = h('button', { class: 'btn primary', type: 'button', disabled: true }, 'Post message');
    const sync = () => { send.disabled = !(approval.checked && text.value.trim() && available.length); };
    text.addEventListener('input', sync);
    approval.addEventListener('change', sync);
    send.addEventListener('click', async () => {
      const [connectionId, channelId] = channel.value.split(':');
      busy(send, true, 'Posting…');
      error.hidden = true;
      try {
        const result = await api(`/channels/${encodeURIComponent(connectionId)}/${encodeURIComponent(channelId)}/messages`, {
          method: 'POST',
          body: { text: text.value.trim(), approved: true, client_request_id: key },
        });
        toast(result.simulated ? 'Demo workspace: nothing was sent to Slack.' : 'Message posted to Slack.');
        close();
      } catch (e) {
        busy(send, false);
        sync();
        error.hidden = false;
        error.textContent = e.message;
      }
    });
    return available.length
      ? h('div', {},
        h('p', { class: 'muted small' }, 'Review the destination and text. ContextOS will only post after you confirm.'),
        h('div', { class: 'field' }, h('label', {}, 'Channel'), channel),
        h('div', { class: 'field' }, h('label', {}, 'Message'), text),
        h('label', { class: 'row small', style: 'margin:10px 0' }, approval, 'I reviewed this message. Post it as me.'),
        error, h('div', { class: 'row' }, send, h('button', { class: 'btn', type: 'button', onclick: close }, 'Cancel')))
      : h('div', {}, h('p', { class: 'muted' }, channels.length
        ? 'Your Slack connection does not have permission to post messages. Reconnect with reply permissions to enable posting.'
        : 'No active Slack channels are available. Connect Slack and join a channel first.'),
      h('button', { class: 'btn', type: 'button', onclick: close }, 'Close'));
  });
}
async function openChannelComposer(channels, initialText = '') {
  try {
    const available = channels || (await api('/integrations/slack/channels')).channels;
    channelPostDialog(available, initialText);
  } catch (e) {
    toast(e.message);
  }
}
async function journeyDialog(m) {
  modal('Pipeline trace', (close) => {
    const body = h('div', {}, h('div', { class: 'empty' }, h('span', { class: 'spin' })));
    api(`/messages/${m.id}/journey`).then((j) => body.replaceChildren(h('div', { class: 'msg ' + m.priority_label }, h('div', { class: 'txt' }, m.text.slice(0, 300))),
      j.projects.length ? h('p', {}, 'Linked to: ', j.projects.map((p) => h('span', { class: 'kw' }, p.name))) : null,
      j.events.length ? h('div', { class: 'stream' }, j.events.map(evRow)) : h('p', { class: 'muted' }, j.note || 'No events recorded.'))).catch((e) => body.replaceChildren(h('div', { class: 'banner err' }, e.message)));
    return body;
  }, { wide: true });
}
function evRow(e) { return h('div', { class: `ev ${e.status}` }, h('span', { class: 't' }, new Date(e.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })), h('span', { class: 's' }, e.stage), h('span', {}, e.detail)); }

async function relatedDialog(m) {
  modal('Related messages', () => {
    const body = h('div', {}, h('div', { class: 'empty' }, h('span', { class: 'spin' })));
    api(`/messages/${m.id}/related`).then((r) => body.replaceChildren(
      h('div', { class: 'msg ' + m.priority_label }, h('div', { class: 'txt' }, m.text.slice(0, 300))),
      h('p', { class: 'small muted' }, r.source === 'qdrant' ? 'Closest in meaning, across Slack and Gmail.' : 'Other messages in the same project.'),
      r.related.length ? h('div', {}, r.related.map((x) => h('div', { class: 'item' }, h('div', {}, h('div', { class: 'small muted' }, `${x.provider === 'gmail' ? '✉ Gmail' : '# Slack'} · ${x.author_name || x.user_id || 'unknown'} · ${x.channel_name} · ${ago(x.message_created_at)}${x.similarity ? ` · ${Math.round(x.similarity * 100)}% alike` : ''}`),
        h('div', {}, x.text.slice(0, 240)), x.cross_app ? h('span', { class: 'pill ok' }, 'other app') : null)))) : h('p', { class: 'muted' }, 'Nothing similar found yet.'))).catch((e) => body.replaceChildren(h('div', { class: 'banner err' }, e.message)));
    return body;
  }, { wide: true });
}

// -------------------------------------------------------------------- people ---
async function viewPeople(main) {
  let all = [], sort = 'active', query = '';
  const grid = h('div', { class: 'people-grid' });
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search people…', 'aria-label': 'Search people', oninput: () => { query = search.value.trim().toLowerCase(); draw(); } });
  const sorts = h('div', { class: 'chips' }, [['active', 'Most recent'], ['messages', 'Most messages'], ['projects', 'Most projects'], ['name', 'A–Z']].map(([k, l]) => h('button', { class: 'chip', type: 'button', 'data-k': k, 'aria-pressed': sort === k, onclick: () => { sort = k; draw(); } }, l)));
  const note = h('div', { class: 'small muted' });
  main.replaceChildren(header('People', 'Everyone you talk to, and what they work on with you.'), h('div', { class: 'toolbar' }, search, sorts), note, grid);
  function draw() {
    sorts.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.k === sort)));
    const rows = all.filter((p) => !query || p.name.toLowerCase().includes(query)).sort((x, y) => sort === 'name' ? x.name.localeCompare(y.name) : sort === 'projects' ? y.projects - x.projects || y.messages - x.messages : sort === 'messages' ? y.messages - x.messages : new Date(y.last_active || 0) - new Date(x.last_active || 0));
    note.textContent = all.length ? `${rows.length} of ${all.length} people` : '';
    grid.replaceChildren(...(rows.length ? rows.map((p) => h('button', { class: 'person-card', type: 'button', onclick: () => personDialog(p.name) },
      avatar(p.name, 44),
      h('div', { class: 'person-main' }, h('b', {}, p.name), p.connector ? h('span', { class: 'pill ok' }, 'Key collaborator') : null,
        h('div', { class: 'small muted' }, p.last_active ? `Last active ${ago(p.last_active)}` : 'No recent activity'),
        h('div', { class: 'person-stats' }, h('span', {}, `${p.messages} message${p.messages === 1 ? '' : 's'}`), h('span', {}, `${p.projects} project${p.projects === 1 ? '' : 's'}`))))) :
      [h('div', { class: 'empty panel', style: 'grid-column:1/-1' }, all.length ? 'Nobody matches that search.' : 'No people yet. They show up once your messages are imported.')]));
  }
  const r = await api('/api/people');
  all = r.people; draw();
}
function personDialog(name) {
  modal(name, () => {
    const body = h('div', {}, h('div', { class: 'empty' }, h('span', { class: 'spin' })));
    api('/api/people/detail?name=' + encodeURIComponent(name)).then((d) => body.replaceChildren(
      h('div', { class: 'person-head' }, avatar(name, 56), h('div', {}, h('h3', {}, name), h('div', { class: 'small muted' }, `${d.projects.length} project${d.projects.length === 1 ? '' : 's'} together`))),
      h('h3', { class: 'dlg-h' }, 'Works on'), d.projects.length ? h('div', {}, d.projects.map((p) => h('span', { class: 'kw' }, `${p.name} · ${p.messages}`))) : h('p', { class: 'muted small' }, 'No project links yet.'),
      h('h3', { class: 'dlg-h' }, 'Works most with'), d.works_with.length ? h('div', {}, d.works_with.map((p) => h('span', { class: 'kw' }, `${p.name} · ${p.shared_projects} shared`))) : h('p', { class: 'muted small' }, 'Nobody shares projects with them yet.'),
      isAdmin() ? h('p', { class: 'small muted' }, `Projects via ${d.projects_source}, collaborators via ${d.works_with_source}.`) : null,
      h('h3', { class: 'dlg-h' }, 'Recent important messages'), d.recent.length ? h('div', {}, d.recent.map((m) => h('div', { class: 'item' }, h('div', {}, h('div', { class: 'small muted' }, `${m.provider === 'gmail' ? 'Gmail' : 'Slack'} · ${m.channel_name} · ${ago(m.message_created_at)}`), h('div', {}, m.text.slice(0, 200)))))) : h('p', { class: 'muted small' }, 'Nothing recent.'))).catch((e) => body.replaceChildren(h('div', { class: 'banner err' }, e.message)));
    return body;
  }, { wide: true });
}

// ------------------------------------------------------------------ projects ---
async function viewProjects(main) {
  const grid = h('div', { class: 'folder-grid' });
  const discover = h('button', { class: 'btn', onclick: async () => { busy(discover, true, 'Searching…'); try { await api('/projects/discover', { method: 'POST' }); toast('Looking through your messages for projects…'); setTimeout(() => { busy(discover, false); load(); }, 5000); } catch (e) { busy(discover, false); toast(e.message); } } }, 'Find projects');
  const create = h('button', { class: 'btn primary', onclick: createProjectDialog }, 'New project');
  const sugg = h('div', {});
  main.replaceChildren(header('Projects', 'Folders of related conversations, gathered from Slack and Gmail. Rename, merge or archive to teach it.', discover, create), sugg, grid);
  let tries = 0;
  async function load() {
    const [ps, ms] = await Promise.all([api('/projects'), api('/api/projects/merge-suggestions').catch(() => ({ suggestions: [] }))]);
    sugg.replaceChildren(...(ms.suggestions.length ? [h('div', { class: 'panel' }, h('h3', { style: 'margin-bottom:6px' }, 'These look like the same project'),
      ms.suggestions.map((x) => h('div', { class: 'item' }, h('div', {}, h('b', {}, x.from.name), ' → ', h('b', {}, x.into.name), h('div', { class: 'small muted' }, x.why)),
        h('button', { class: 'btn sm', onclick: async (e) => { if (!confirm(`Merge “${x.from.name}” into “${x.into.name}”?`)) return; busy(e.target, true, 'Merging'); try { await api(`/projects/${x.from.id}/merge`, { method: 'POST', body: { into_id: x.into.id } }); toast('Merged'); load(); } catch (er) { busy(e.target, false); toast(er.message); } } }, 'Merge'))))] : []));
    const folder = (p) => h('button', { class: 'folder', type: 'button', onclick: () => projectDialog(p.id, load) },
      h('span', { class: 'folder-tab' }),
      h('span', { class: 'folder-body' },
        h('span', { class: 'folder-title' }, h('b', {}, p.name), p.status === 'confirmed' ? h('span', { class: 'pill ok' }, 'Confirmed') : null),
        h('span', { class: `small status-${p.health}` }, '● ' + ({ on_track: 'On track', at_risk: 'At risk', blocked: 'Blocked', unknown: 'No status yet' }[p.health] || p.health)),
        h('span', { class: 'folder-sum' }, p.summary_preview || 'Summary on its way…'),
        p.risk ? h('span', { class: 'small', style: 'color:var(--warn)' }, `⚠ ${Math.round(p.risk.share * 100)}% of messages are from ${p.risk.person}`) : null,
        h('span', { class: 'folder-meta' }, `${p.message_count} messages · ${p.author_count} people · ${p.channel_count} conversations`),
        h('span', {}, p.keywords.slice(0, 4).map((k) => h('span', { class: 'kw' }, k)))));
    grid.replaceChildren(...(ps.length ? ps.map(folder) : [h('div', { class: 'empty panel folder-empty', style: 'grid-column:1/-1' },
      h('h3', {}, tries < 3 ? 'Looking for your projects…' : 'No projects yet'),
      h('p', { class: 'muted' }, tries < 3 ? 'This takes a few seconds the first time.' : 'Projects appear when a topic repeats across a few days or conversations. You can also start one yourself.'),
      h('div', { class: 'row', style: 'justify-content:center' }, h('button', { class: 'btn primary', onclick: createProjectDialog }, 'Create a project')))]));
    tries += 1;
    return ps.length;
  }
  const n = await load();
  if (!n) every(5000, () => { if (tries < 6) load().catch(() => {}); });
  else every(30000, () => load().catch(() => {}));
}
function createProjectDialog() {
  modal('New project', (close) => {
    const name = h('input', { class: 'input', maxlength: '100', 'aria-label': 'Project name' }), kw = h('input', { class: 'input', placeholder: 'comma-separated, e.g. phoenix, db cutover', 'aria-label': 'Keywords' }), err = h('div', { class: 'banner err', hidden: true });
    const go2 = h('button', { class: 'btn primary', onclick: async () => { busy(go2, true, 'Creating…'); try { await api('/projects', { method: 'POST', body: { name: name.value, keywords: kw.value.split(',').map((s) => s.trim()).filter(Boolean) } }); close(); toast('Project created'); renderShell(); } catch (e) { busy(go2, false); err.hidden = false; err.textContent = e.message; } } }, 'Create');
    return h('div', {}, h('div', { class: 'field' }, h('label', {}, 'Name'), name), h('div', { class: 'field' }, h('label', {}, 'Keywords that mention it (the name is included)'), kw), err, go2);
  });
}
function projectDialog(id, onChange) {
  modal('Project', (close) => {
    const body = h('div', {}, h('div', { class: 'empty' }, h('span', { class: 'spin' })));
    const reload = () => api(`/projects/${id}`).then(draw).catch((e) => body.replaceChildren(h('div', { class: 'banner err' }, e.message)));
    function draw(p) {
      const s = p.summary, cites = (ids) => ids.map((i) => h('button', { class: 'cite', type: 'button', title: 'Show source message', onclick: () => sourceDialog(i) }, '#' + i));
      body.replaceChildren(h('h2', { style: 'font-size:20px' }, p.name), h('div', { class: `small status-${p.health}` }, '● ' + p.health.replace('_', ' ')),
        p.risk ? h('div', { class: 'banner warn' }, `Single point of failure: ${p.risk.person} wrote ${Math.round(p.risk.share * 100)}% of the ${p.risk.messages} messages here (${p.risk.people} people in total). Consider sharing the knowledge.`) : null,
        s ? h('div', {}, h('p', {}, s.summary), h('p', { class: 'small muted' }, `Summary of ${s.message_count} messages` + (isAdmin() ? ` · v${s.version} · ${s.mode}` : '')),
          s.key_points.length ? h('div', {}, h('h3', {}, 'Key points'), h('ul', { class: 'bul' }, s.key_points.map((k) => h('li', {}, k.text, cites(k.sources))))) : null,
          s.open_items.length ? h('div', {}, h('h3', {}, 'Open items'), h('ul', { class: 'bul' }, s.open_items.map((k) => h('li', {}, k.text, cites(k.sources))))) : null) : h('p', { class: 'muted' }, 'No summary yet.'),
        p.related && p.related.length ? h('div', {}, h('h3', { style: 'margin-top:12px' }, 'Shares people with'), h('div', { class: 'small' }, p.related.map((x) => h('span', { class: 'kw' }, `${x.name} (${x.shared_people})`)), isAdmin() ? h('span', { class: 'muted' }, ` via ${p.related_source}`) : null)) : null,
        h('h3', {}, 'People'), h('div', { class: 'small' }, p.people.length ? p.people.map((x) => h('span', { class: 'kw' }, `${x.name} (${x.messages})`)) : 'None', isAdmin() ? h('span', { class: 'muted' }, ` via ${p.people_source}`) : null),
        h('h3', { style: 'margin-top:12px' }, 'Keywords'), h('div', {}, p.keywords.map((k) => h('span', { class: 'kw' }, k))),
        h('h3', { style: 'margin-top:12px' }, 'Latest in this folder'), h('div', {}, p.recent_messages.slice(0, 12).map((m) => h('div', { class: 'item' }, h('div', {}, h('div', { class: 'small muted' }, `${m.provider === 'gmail' ? '✉ ' : '# '}${m.author_name || m.user_id} · ${m.channel_name} · ${ago(m.message_created_at)}`), h('div', {}, m.text.slice(0, 160)))))),
        h('div', { class: 'row', style: 'margin-top:14px' },
          h('button', { class: 'btn sm', onclick: async (e) => { busy(e.target, true, 'Queued'); try { await api(`/projects/${id}/summarize`, { method: 'POST' }); toast('Summary requested'); setTimeout(reload, 3500); } catch (er) { toast(er.message); } } }, 'Regenerate summary'),
          h('button', { class: 'btn sm', onclick: async () => { const n = prompt('Rename project', p.name); if (n && n.trim().length > 1) { try { await api(`/projects/${id}`, { method: 'PATCH', body: { name: n.trim() } }); reload(); onChange(); } catch (er) { toast(er.message); } } } }, 'Rename'),
          h('button', { class: 'btn sm danger', onclick: async () => { if (confirm('Archive this project? Discovery will not bring it back.')) { await api(`/projects/${id}`, { method: 'PATCH', body: { status: 'archived' } }); close(); onChange(); } } }, 'Archive')));
    }
    reload();
    return body;
  }, { wide: true });
}
function sourceDialog(id) {
  modal(`Message #${id}`, () => { const b = h('div', {}, h('span', { class: 'spin' })); api(`/messages/${id}`).then((m) => b.replaceChildren(messageCard(m))).catch((e) => b.replaceChildren(h('div', { class: 'banner warn' }, e.status === 404 ? 'This message was deleted or has expired under the retention policy.' : e.message))); return b; });
}
// ----------------------------------------------------------------- assistant ---
async function viewAssistant(main) {
  const [dash, projs, conn, calendar] = await Promise.all([
    api('/dashboard/summary'), api('/projects'), api('/integrations/slack/connection'), api('/integrations/google-calendar/connection'),
  ]);
  let empty = !dash.messages_total && !calendar.connected;
  const q = h('input', { class: 'input assistant-question', placeholder: empty ? 'Connect an account to get started…' : 'Ask about your work…', maxlength: '2000', 'aria-label': 'Question', disabled: empty });
  const thread = h('div', { class: 'chat-thread', role: 'log', 'aria-live': 'polite', 'aria-label': 'Assistant conversation' });
  const chatHistory = [];
  const askButton = h('button', { class: 'btn primary', type: 'submit', disabled: empty }, 'Send');
  const suggestions = h('div', { class: 'assistant-suggestions' });
  const ask = async (text) => {
    const question = text.trim();
    if (!question || askButton.disabled) return;
    thread.append(h('div', { class: 'chat-message user' }, h('p', {}, question)));
    const pending = h('div', { class: 'chat-message assistant' }, h('div', { class: 'chat-loading' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), 'Looking through your messages…'));
    thread.append(pending);
    q.value = '';
    askButton.disabled = true;
    pending.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    let found = [], streamedText = '', bodyEl = null, srcEl = null;
    const draw = (text, final) => {
      const body = richAnswer(final ? text : cleanAssistantText(text).replace(/\[\d{0,2}$/, ''), found, openSource);
      if (bodyEl) bodyEl.replaceWith(body); else pending.append(body);
      bodyEl = body;
    };
    const drawSources = (list, cited) => {
      // Sources stay out of the way: a single "+ N sources" control that opens the cards only when the person asks.
      const ordered = [...list].sort((x, y) => (cited.includes(y.n) ? 1 : 0) - (cited.includes(x.n) ? 1 : 0) || x.n - y.n);
      const el = ordered.length ? h('details', { class: 'sources-details' },
        h('summary', {}, h('span', { class: 'plus', 'aria-hidden': 'true' }, '＋'), ` ${ordered.length} source${ordered.length === 1 ? '' : 's'}`),
        h('div', { class: 'source-cards' }, ordered.map((x) => sourceCard(x, openSource)))) : null;
      if (srcEl) { if (el) srcEl.replaceWith(el); else srcEl.remove(); } else if (el) pending.append(el);
      srcEl = el;
    };
    try {
      const r = await streamAssistant(question, (token) => {
        streamedText += token;
        pending.querySelector('.chat-loading')?.remove();
        draw(streamedText, false);
      }, chatHistory.slice(-8), (list) => { found = list; const l = pending.querySelector('.chat-loading'); if (l) l.lastChild.textContent = `Reading ${list.length} message${list.length === 1 ? '' : 's'}…`; });
      const sources = Array.isArray(r.sources) ? r.sources : [];
      found = sources;
      const answer = typeof r.answer === 'string' && r.answer.trim() && r.answer.trim().toLowerCase() !== 'null'
        ? r.answer.trim() : sources.length ? 'A few messages may help answer that.' : 'I couldn’t find anything relevant in your connected messages yet.';
      chatHistory.push({ role: 'user', content: question }, { role: 'assistant', content: answer.replace(/\s*\[\d{1,2}\]/g, '') });
      if (chatHistory.length > 8) chatHistory.splice(0, chatHistory.length - 8);
      pending.querySelector('.chat-loading')?.remove();
      draw(answer, true);
      drawSources(sources, Array.isArray(r.cited) ? r.cited : []);
      if (r.suggested_reply && typeof r.suggested_reply.draft === 'string') {
        pending.append(h('div', { class: 'draft-card' }, h('div', { class: 'meta' }, `Draft for ${r.suggested_reply.recipient}`), h('p', {}, `“${r.suggested_reply.draft}”`),
          h('button', { class: 'btn sm', type: 'button', onclick: () => openChannelComposer(r.suggested_reply.channel_options, r.suggested_reply.draft) }, 'Review and choose channel')));
      }
      if (r.channel_compose) pending.append(h('button', { class: 'btn sm', type: 'button', onclick: () => openChannelComposer() }, 'Compose message'));
      pending.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      suggestions.hidden = true;
    } catch (e) {
      pending.replaceChildren(h('p', { role: 'alert' }, e.message));
      pending.classList.add('chat-error');
    } finally {
      askButton.disabled = empty;
      if (!empty) q.focus();
    }
  };
  const form = h('form', { class: 'row', onsubmit: (e) => { e.preventDefault(); ask(q.value); } }, h('div', { style: 'flex:1;min-width:220px' }, q), askButton);
  const urgentCount = dash.urgent ?? 0;
  let calendarConnected = calendar.connected;
  let connectedCount = conn.connections.filter((c) => c.status === 'connected').length + Number(calendarConnected);
  const questionsFor = (summary, projects) => {
    const questions = ['What needs my attention?'];
    const urgent = summary.urgent ?? 0;
    if (summary.pending_reminders) questions.push('What is due today?');
    if (urgent) questions.push('Summarise the urgent messages');
    if (projects.length) questions.push(`What is the status of ${projects[0].name}?`);
    else if (summary.messages_total) questions.push('Summarise the important updates');
    if (projects.length > 1) questions.push(`Who should I ask about ${projects[0].name}?`);
    return questions;
  };
  const messagesStat = h('button', { class: 'text-action', type: 'button', onclick: () => openInbox('all') }, `${dash.messages_total} messages`);
  const projectsStat = h('button', { class: 'text-action', type: 'button', onclick: () => go('projects') }, `${projs.length} projects`);
  const urgentStat = h('button', { class: 'text-action', type: 'button', onclick: () => openInbox('urgent') }, `${urgentCount} urgent`);
  const sourcesStat = h('button', { class: 'text-action assistant-sources', type: 'button', onclick: () => go('settings') }, `${connectedCount} connected sources`);
  const stats = h('div', { class: 'assistant-summary' },
    messagesStat,
    h('span', { 'aria-hidden': 'true' }, '·'),
    projectsStat,
    h('span', { 'aria-hidden': 'true' }, '·'),
    urgentStat,
    h('span', { 'aria-hidden': 'true' }, '·'),
    sourcesStat);
  const start = h('section', { class: 'assistant-intro' },
    h('h2', {}, empty ? `Welcome${state.user.name ? ', ' + state.user.name.split(' ')[0] : ''}. What would you like to know?` : 'What would you like to know?'),
    h('p', {}, empty ? (calendarConnected ? 'Your Google Calendar is connected. Ask about your schedule.' : connectedCount ? 'Your account is connected. Messages will appear here after the first sync.' : 'Connect Slack, Gmail, or Google Calendar to get started.') : 'Find answers in your connected messages and calendar. Every message-based answer links back to its source.'),
    empty && !connectedCount ? h('a', { class: 'text-action', href: '#settings', onclick: (e) => { e.preventDefault(); go('settings'); } }, 'Connect an account') : null);
  const renderSuggestions = (summary, projects) => suggestions.replaceChildren(...(empty ? [] : [...questionsFor(summary, projects), ...(calendarConnected ? ['Any meetings today?'] : [])].map((s) => h('button', { class: 'suggestion', type: 'button', onclick: () => ask(s) }, s))));
  renderSuggestions(dash, projs);
  main.replaceChildren(header('Assistant'), start, stats, thread, h('div', { class: 'chat-composer' }, h('section', { class: 'assistant-ask' }, form, suggestions,
    h('div', { class: 'chat-status' }, 'Answers use your connected messages and calendar. Message sources are linked below.'))));
  let liveUpdateFailed = false;
  async function refreshLiveSummary() {
    const [latest, latestConn, latestCalendar] = await Promise.all([
      api('/dashboard/summary'), api('/integrations/slack/connection'), api('/integrations/google-calendar/connection'),
    ]);
    const latestUrgent = latest.urgent ?? 0;
    calendarConnected = latestCalendar.connected;
    connectedCount = latestConn.connections.filter((c) => c.status === 'connected').length + Number(calendarConnected);
    messagesStat.textContent = `${latest.messages_total} ${latest.messages_total === 1 ? 'message' : 'messages'}`;
    urgentStat.textContent = `${latestUrgent} urgent`;
    sourcesStat.textContent = `${connectedCount} connected sources`;
    if (empty && (latest.messages_total || latestCalendar.connected)) {
      empty = false;
      q.disabled = false;
      q.placeholder = 'Ask about your work…';
      askButton.disabled = false;
      start.querySelector('p').textContent = calendarConnected && !latest.messages_total
        ? 'Your Google Calendar is connected. Ask about your schedule.'
        : 'Find answers in your connected messages and calendar. Every message-based answer links back to its source.';
    }
    renderSuggestions(latest, projs);
    liveUpdateFailed = false;
  }
  every(30000, () => refreshLiveSummary().catch(() => { liveUpdateFailed = true; }));
}

// ------------------------------------------------------------------ pipeline ---
async function viewPipeline(main) {
  state.eventCursor = 0; state.events = [];
  const status = h('div', { class: 'row', style: 'margin-bottom:12px' }), flow = h('div', { class: 'flow' }), stream = h('div', { class: 'stream', role: 'log', 'aria-label': 'Live pipeline events' });
  const demoBtn = h('button', { class: 'btn', onclick: async () => { busy(demoBtn, true, 'Loading…'); try { await api('/api/demo/seed?size=full', { method: 'POST' }); toast('Sample workspace is flowing through the pipeline.'); } catch (e) { toast(e.message); } busy(demoBtn, false); } }, 'Load sample data');
  const resetBtn = h('button', { class: 'btn danger', onclick: async () => { if (!confirm('Remove the sample workspace and everything derived from it?')) return; await api('/api/demo/reset', { method: 'POST' }); state.events = []; state.eventCursor = 0; stream.replaceChildren(); toast('Sample data removed'); poll(); } }, 'Remove sample data');
  const actions = state.config.demo_enabled ? [demoBtn, resetBtn] : [];
  const checkBox = h('div', {}), runCheck = h('button', { class: 'btn sm', onclick: () => loadChecks() }, 'Run feature check');
  const ICON = { ok: '✓', warn: '…', off: '–', fail: '✕' };
  async function loadChecks() {
    busy(runCheck, true, 'Checking…');
    try {
      const r = await api('/api/selfcheck');
      checkBox.replaceChildren(h('div', { class: 'row small', style: 'margin-bottom:8px' }, ['ok', 'warn', 'fail', 'off'].map((k) => h('span', { class: `pill ${k === 'ok' ? 'ok' : k === 'fail' ? 'err' : k === 'warn' ? 'warn' : ''}` }, `${r.summary[k]} ${k}`))),
        h('div', { class: 'checks' }, r.checks.map((c) => h('div', { class: `chk ${c.status}` }, h('span', { class: 'ic' }, ICON[c.status]), h('b', {}, c.label), h('span', { class: 'small muted' }, c.detail)))));
    } catch (e) { checkBox.replaceChildren(h('div', { class: 'banner err' }, e.message)); } finally { busy(runCheck, false); }
  }
  main.replaceChildren(header('Live pipeline', 'Every stage a message passes through, updating as it happens.', ...actions), status,
    h('div', { class: 'panel' }, h('div', { class: 'hd' }, h('h2', {}, 'Feature check'), runCheck), h('p', { class: 'small muted' }, 'Load the sample data, wait ~30 seconds, then run this: each line is verified against your real rows and service round-trips.'), checkBox),
    h('div', { class: 'panel' }, flow),
    h('div', { class: 'panel' }, h('div', { class: 'hd' }, h('h2', {}, 'Event stream'), h('button', { class: 'chip', id: 'clearf', onclick: () => { state.stageFilter = null; drawStream(); markNodes(); } }, 'Show all')), stream));
  let lastLayers = [];
  function markNodes() { flow.querySelectorAll('.node').forEach((n) => n.setAttribute('aria-pressed', String(n.dataset.stage === state.stageFilter))); }
  function drawStream() { const rows = state.events.filter((e) => !state.stageFilter || e.stage === state.stageFilter).slice(-150); stream.replaceChildren(...rows.map(evRow)); stream.scrollTop = stream.scrollHeight; }
  async function poll() {
    const o = await api('/api/pipeline/overview');
    const w = o.worker;
    status.replaceChildren(...[h('span', { class: `pill ${w.online ? 'ok' : 'err'}` }, w.online ? `● worker online (${w.workers})` : '● worker offline'),
      h('span', { class: 'pill' }, `queue: ${['queued', 'processing', 'failed'].map((k) => `${o.queue[k] || 0} ${k}`).join(' · ')}`), h('span', { class: 'pill' }, `${o.totals.messages} messages · ${o.totals.projects} projects`),
      h('span', { class: `pill ${o.llm.enabled ? 'ok' : 'warn'}` }, o.llm.enabled ? `AI: ${o.llm.model} · ${o.llm.calls_today}/${o.llm.user_daily_cap} today` : 'AI: not configured (extractive)'),
      h('span', { class: 'pill' }, `redis ${o.services.redis} · qdrant ${o.services.qdrant} · neo4j ${o.services.neo4j}`),
      w.hint ? h('span', { class: 'small muted' }, w.hint) : null].filter(Boolean));
    lastLayers = o.layers;
    const groups = [...new Set(o.layers.map((l) => l.group))];
    flow.replaceChildren(...groups.map((g) => h('div', { class: 'col' }, h('h3', {}, g), o.layers.filter((l) => l.group === g).map((l) => {
      const live = l.last_at && Date.now() - new Date(l.last_at).getTime() < 15000;
      return h('button', { class: `node ${l.last_status || ''} ${live ? 'live' : ''}`, type: 'button', 'data-stage': l.stage, 'aria-pressed': String(state.stageFilter === l.stage), title: l.last_detail || 'No activity in the last 24h', onclick: () => { state.stageFilter = state.stageFilter === l.stage ? null : l.stage; drawStream(); markNodes(); } },
        h('span', { class: 'dot' }), h('b', {}, l.label), h('div', { class: 'cnt' }, l.ok + l.skipped + l.error), h('div', { class: 'small muted' }, l.error ? `${l.error} error(s)` : l.skipped ? `${l.skipped} skipped` : l.last_at ? ago(l.last_at) : 'idle'));
    }))));
    const ev = await api(`/api/pipeline/events?since_id=${state.eventCursor}&limit=200`);
    if (ev.events.length) { state.events.push(...ev.events); state.events = state.events.slice(-600); state.eventCursor = ev.cursor; drawStream(); }
  }
  await poll();
  loadChecks();
  every(2500, () => poll().catch(() => {}));
  every(15000, () => loadChecks());
}

// ------------------------------------------------------------------ settings ---
async function viewSettings(main) {
  const connBox = h('div', {}), calendarBox = h('div', {}), smartBox = h('div', {});
  const sections = [['connections', 'Connected accounts', 'Slack, Gmail and Calendar'], ['account', 'Account', 'Your profile and data'], ...(isAdmin() ? [['system', 'System', 'Admin only']] : [])];
  let current = sections.some((x) => x[0] === state.settingsTab) ? state.settingsTab : 'connections';
  const panes = {
    connections: h('div', { class: 'settings-pane' },
      h('section', { class: 'set-card' }, h('div', { class: 'set-head' }, h('h2', {}, 'Slack & Gmail'), h('p', { class: 'muted small' }, 'Messages from these accounts feed your Inbox, Projects and the Assistant. Access is read-only unless you allow replies.')), connBox),
      h('section', { class: 'set-card' }, h('div', { class: 'set-head' }, h('h2', {}, 'Google Calendar'), h('p', { class: 'muted small' }, 'Lets the Assistant answer questions about your schedule.')), calendarBox)),
    account: h('div', { class: 'settings-pane' },
      h('section', { class: 'set-card' }, h('div', { class: 'set-head' }, h('h2', {}, 'Profile')),
        h('div', { class: 'person-head' }, avatar(state.user.name || state.user.email, 56), h('div', {}, h('b', {}, state.user.name || 'Your account'), h('div', { class: 'muted small' }, state.user.email), h('div', { class: 'muted small' }, state.user.signed_in_with_google ? 'Signed in with Google' : 'Signed in with email and password'))),
        h('div', { class: 'row', style: 'margin-top:14px' }, h('button', { class: 'btn', onclick: signOut }, 'Sign out'))),
      h('section', { class: 'set-card danger-zone' }, h('div', { class: 'set-head' }, h('h2', {}, 'Delete my data'), h('p', { class: 'muted small' }, 'Revokes Slack and Gmail access and permanently erases your messages, projects and account.')),
        h('button', { class: 'btn danger', onclick: deleteAccountDialog }, 'Delete my account and data'))),
    system: h('div', { class: 'settings-pane' },
      h('section', { class: 'set-card' }, h('div', { class: 'set-head' }, h('h2', {}, 'Smart features'), h('p', { class: 'muted small' }, 'Search by meaning and the relationship graph.')), smartBox),
      state.config.demo_enabled ? h('section', { class: 'set-card' }, h('div', { class: 'set-head' }, h('h2', {}, 'Sample data'), h('p', { class: 'muted small' }, 'A fake workspace and mailbox that runs through the real pipeline.')),
        h('div', { class: 'row' }, h('button', { class: 'btn', onclick: loadSampleData }, 'Load sample data'), h('button', { class: 'btn danger', onclick: async () => { if (!confirm('Remove the sample workspace and everything derived from it?')) return; try { await api('/api/demo/reset', { method: 'POST' }); toast('Sample data removed'); } catch (e) { toast(e.message); } } }, 'Remove sample data'))) : null),
  };
  const nav = h('nav', { class: 'settings-nav', 'aria-label': 'Settings sections' }, sections.map(([id, label, sub]) => h('button', { type: 'button', 'data-id': id, 'aria-current': id === current ? 'page' : null, onclick: () => show(id) }, h('b', {}, label), h('span', {}, sub))));
  const pane = h('div', { class: 'settings-body' });
  function show(id) { current = id; state.settingsTab = id; nav.querySelectorAll('button').forEach((b) => b.setAttribute('aria-current', b.dataset.id === id ? 'page' : 'false')); pane.replaceChildren(panes[id]); }
  main.replaceChildren(header('Settings', 'Accounts and preferences.'), h('div', { class: 'settings-layout' }, nav, pane));
  show(current);
  async function loadSmart() {
    const st = await api('/api/intelligence/status');
    const badge = (name, x) => h('span', { class: `pill ${x.state === 'on' ? 'ok' : x.state === 'error' ? 'err' : ''}` }, `${name}: ${x.state === 'on' ? 'on' : x.state === 'error' ? 'not reachable' : 'off'}`);
    const rebuild = h('button', { class: 'btn sm', onclick: async () => { busy(rebuild, true, 'Queued…'); try { await api('/api/intelligence/rebuild', { method: 'POST' }); toast('Catching the indexes up with your messages…'); setTimeout(() => { busy(rebuild, false); loadSmart(); }, 4000); } catch (e) { busy(rebuild, false); toast(e.message); } } }, 'Rebuild from my messages');
    smartBox.replaceChildren(h('div', { class: 'row', style: 'margin-bottom:8px' }, badge('Qdrant (meaning)', st.qdrant), badge('Neo4j (relationships)', st.neo4j),
      st.qdrant.state === 'on' ? h('span', { class: 'small muted' }, `${st.qdrant.messages ?? 0} messages and ${st.qdrant.summaries ?? 0} summaries embedded`) : null,
      st.neo4j.state === 'on' ? h('span', { class: 'small muted' }, `${st.neo4j.people ?? 0} people · ${st.neo4j.projects ?? 0} projects · ${st.neo4j.links ?? 0} links in the graph`) : null),
      h('div', { class: 'checks' }, st.features.map((f) => h('div', { class: `chk ${f.on ? 'ok' : 'off'}` }, h('span', { class: 'ic' }, f.on ? '✓' : '–'), h('b', {}, `${f.label} · ${f.engine}`), h('span', { class: 'small muted' }, f.does)))),
      st.qdrant.state === 'off' && st.neo4j.state === 'off' ? h('p', { class: 'small muted', style: 'margin-top:8px' }, 'These need Qdrant and Neo4j to be switched on for this server. Everything else keeps working without them.') : h('div', { class: 'row', style: 'margin-top:10px' }, rebuild));
  }
  async function load() {
    const [c, calendar] = await Promise.all([
      api('/integrations/slack/connection'), api('/integrations/google-calendar/connection'),
    ]);
    connBox.replaceChildren(...(c.connections.length ? c.connections.map(connCard) : [h('p', { class: 'muted' }, 'No Slack workspace connected.')]),
      h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn primary', onclick: () => modal('Connect Slack', () => { const b = h('div', {}); levelPicker(b, c.oauth_configured); return b; }) }, c.connections.length ? 'Connect another workspace' : 'Connect Slack'),
        h('a', { class: 'btn', href: c.gmail_configured ? '/integrations/gmail/connect' : '#', onclick: (e) => { if (!c.gmail_configured) { e.preventDefault(); toast(isAdmin() ? 'Gmail needs GOOGLE_CLIENT_ID / SECRET on the server.' : 'Gmail isn’t available yet. Ask your administrator.'); } } }, c.connections.some((x) => x.provider === 'gmail') ? 'Connect another Gmail' : 'Connect Gmail')));
    const calendarActions = calendar.connected
      ? [
        calendar.configured ? h('a', { class: 'btn sm', href: '/integrations/google-calendar/connect' }, 'Switch account') : null,
        h('button', { class: 'btn sm danger', onclick: async () => {
        if (!confirm('Disconnect Google Calendar? ContextOS will stop reading your events.')) return;
        try {
          const result = await api('/integrations/google-calendar/disconnect', { method: 'POST' });
          toast(result.revoked ? 'Google Calendar disconnected.' : 'Google Calendar disconnected, but Google could not confirm token revocation.');
          await load();
        } catch (e) { toast(e.message); }
        } }, 'Disconnect'),
      ]
      : h('a', { class: 'btn primary', href: calendar.configured ? '/integrations/google-calendar/connect' : '#', onclick: (e) => {
        if (!calendar.configured) { e.preventDefault(); toast(isAdmin() ? 'Google Calendar needs Google OAuth and TOKEN_ENCRYPTION_KEY configured on the server.' : 'Google Calendar isn’t available yet. Ask your administrator.'); }
      } }, 'Connect Google Calendar');
    calendarBox.replaceChildren(
      h('p', { class: 'small muted' }, calendar.connected
        ? `Connected as ${calendar.email}. ContextOS can read event titles and times; it cannot create, edit, or delete events.`
        : 'Connect your Google Calendar to check your schedule from the Assistant. Access is read-only.'),
      ...(calendar.status === 'needs_reauth' ? [h('div', { class: 'banner warn' }, 'Google Calendar access expired or was revoked. Reconnect to check your schedule.')] : []),
      h('div', { class: 'row', style: 'margin-top:10px' }, calendarActions));
  }
  function connCard(c) {
    const pct = c.conversations_total ? Math.round((100 * c.conversations_done) / c.conversations_total) : 0;
    return h('div', { class: 'panel', style: 'margin:0 0 12px' }, h('div', { class: 'hd' }, h('div', {}, h('b', {}, (c.provider === 'gmail' ? '✉ ' : '# ') + (c.team_name || c.team_id)), ' ', h('span', { class: `pill ${c.status === 'connected' ? 'ok' : 'err'}` }, c.status), c.is_demo ? h('span', { class: 'pill demo', style: 'margin-left:6px' }, 'sample') : null)),
      h('div', { class: 'small muted' }, `${c.provider === 'gmail' ? 'Gmail, read-only' : 'Access: ' + (c.access_label || c.access_level)} · ${c.messages_synced} messages imported · sync ${c.sync_state}${c.last_sync_finished_at ? ' · last ' + ago(c.last_sync_finished_at) : ''}`),
      c.sync_state === 'running' || c.sync_state === 'queued' ? h('div', { class: 'bar', role: 'progressbar', 'aria-valuenow': pct }, h('i', { style: `width:${pct}%` })) : null,
      Object.keys(c.conversations_by_type).length ? h('div', { class: 'small muted', style: 'margin-top:6px' }, 'Conversations: ' + Object.entries(c.conversations_by_type).map(([k, v]) => `${v} ${k.replace('_', ' ')}`).join(', ')) : null,
      c.problems.map((p) => h('div', { class: 'banner warn' }, p.message)),
      h('div', { class: 'row', style: 'margin-top:10px' }, c.is_demo ? null : h('button', { class: 'btn sm', onclick: async () => { try { await api('/integrations/slack/sync', { method: 'POST', body: { connection_id: c.id } }); toast('Sync queued'); setTimeout(load, 1500); } catch (e) { toast(e.message); } } }, 'Sync now'),
        c.is_demo || c.provider === 'gmail' ? null : h('button', { class: 'btn sm', onclick: () => modal('Change permissions', () => { const b = h('div', {}); levelPicker(b, true, c.access_level); return b; }) }, 'Change permissions'),
        h('button', { class: 'btn sm danger', onclick: () => disconnectDialog(c, load) }, 'Disconnect')));
  }
  await load();
  if (isAdmin()) { loadSmart().catch((e) => smartBox.replaceChildren(h('div', { class: 'banner err' }, e.message))); every(30000, () => loadSmart().catch(() => {})); }
  every(10000, () => load().catch(() => {}));
}
function disconnectDialog(c, done) {
  modal('Disconnect', (close) => {
    const purge = h('input', { type: 'checkbox', id: 'purge' }), go2 = h('button', { class: 'btn danger', onclick: async () => { busy(go2, true, 'Disconnecting…'); try { const r = await api('/integrations/slack/disconnect', { method: 'POST', body: { connection_id: c.id, purge: purge.checked } }); toast(r.messages_deleted ? `Disconnected. ${r.messages_deleted} messages erased.` : 'Disconnected.'); close(); done(); } catch (e) { busy(go2, false); toast(e.message); } } }, 'Disconnect');
    return h('div', {}, h('p', {}, `ContextOS will stop reading ${c.team_name || 'this account'} and revoke its access at ${c.provider === 'gmail' ? 'Google' : 'Slack'}.`), h('label', { class: 'row', style: 'margin:10px 0' }, purge, 'Also erase the messages already imported'), go2);
  });
}
function deleteAccountDialog() {
  modal('Delete account', (close) => {
    const email = h('input', { class: 'input', autocomplete: 'off', placeholder: state.user.email, 'aria-label': 'Confirm email' }), err = h('div', { class: 'banner err', hidden: true });
    const go2 = h('button', { class: 'btn danger', onclick: async () => { busy(go2, true, 'Deleting…'); try { await api('/api/account', { method: 'DELETE', body: { confirm_email: email.value } }); close(); state.user = null; state.csrf = null; renderAuth({ notice: { kind: 'ok', text: 'Your account and all its data were deleted.' } }); } catch (e) { busy(go2, false); err.hidden = false; err.textContent = e.message; } } }, 'Delete everything');
    return h('div', {}, h('p', {}, 'This revokes Slack access and permanently erases your messages, projects, summaries and account. It cannot be undone.'), h('div', { class: 'field' }, h('label', {}, 'Type your email to confirm'), email), err, go2);
  });
}

boot();
