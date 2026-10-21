(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const S = { csrf: '', user: null, cfg: {}, view: 'today', prio: 'ALL', project: '', q: '', sel: null, projectMatchKey: '', msgs: {}, conns: [], contacts: [], personFilter: '', peopleSearch: '', feedAfter: 0, feed: [], paused: false, lock: 0, authMode: 'login', reply: null, sendTimer: null, pending: null, ask: [] };
const ERR = {
  token_revoked: 'Slack access expired or was revoked. Reconnect Slack in Settings.', needs_reauth: 'Slack access expired or was revoked. Reconnect Slack.',
  revoked: 'Slack access was revoked. Reconnect Slack.', missing_scope: 'A Slack permission is missing. Enable it in Settings, then try again.',
  ratelimited: 'Too many requests right now. Please wait a moment.', ambiguous: 'Slack did not answer in time. Check the thread before resending; the audit trail will update.',
  not_authenticated: 'Please sign in.', not_in_channel: 'You are no longer in that conversation.', is_archived: 'That conversation is archived.', csrf: 'Your session changed. Reload the page.',
  bad_origin: 'Request blocked. Open ContextOS from its official address.', server_error: 'Something went wrong on our side. Please try again.',
  bad_credentials: 'That email/password combination was not accepted. If account creation just failed, correct the signup issue and create the account first.',
  email_taken: 'An account with that email already exists. Switch to Sign in, or use Google if you created it with Google.',
  weak_password: 'Choose a password with at least 10 characters, including letters and numbers, and do not include your email name.' };
const LOGIN_ERR = { cancelled: 'Google sign-in was cancelled.', rate_limited: 'Too many attempts. Try again in a few minutes.', invalid_state: 'That sign-in link expired. Please try again.',
  email_not_verified: 'Your Google email is not verified, so we cannot sign you in with it.', google_not_configured: 'Google sign-in is not set up on this server.', token_exchange_failed: 'Google could not confirm the sign-in. Please try again.', account_disabled: 'This account is disabled.', network: 'Could not reach Google. Try again.' };
const SLACK_FLASH = { connected: ['ok', 'Slack connected. We are importing your recent history now; keep the worker running.'], denied: ['warn', 'Slack authorization was cancelled. Nothing was connected.'], error: ['err', 'Slack connection failed'] };
const STAGE_LABEL = { auth: 'Sign-in', slack_oauth: 'Slack OAuth', event_received: 'Ingress', sync: 'History sync', fanout: 'Fan-out', ingest: 'Store', score: 'Score', extract: 'Extract', project: 'Projects', index_qdrant: 'Qdrant', index_neo4j: 'Neo4j', summary: 'Summary', llm: 'LLM', retention: 'Retention' };

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const human = e => ERR[e.code] || e.message || 'Request failed';
const VALIDATION_FIELDS = { email: 'Email', password: 'Password', name: 'Name' };
function validationMessage(details) {
  if (!Array.isArray(details)) return '';
  const messages = details.map(item => {
    const field = VALIDATION_FIELDS[(item.loc || []).at(-1)];
    if (!field) return '';
    if (field === 'Password') return 'Password must be at least 10 characters.';
    if (field === 'Email') return 'Enter a valid email address.';
    return 'Check the name field.';
  }).filter(Boolean);
  return [...new Set(messages)].join(' ');
}
const relt = s => { if (!s) return 'never'; const d = (Date.now() - new Date(s)) / 1000; if (d < 0) return 'soon'; if (d < 60) return 'just now'; if (d < 3600) return Math.floor(d / 60) + ' min ago'; if (d < 86400) return Math.floor(d / 3600) + ' h ago'; return new Date(s).toLocaleDateString(); };
const when = s => new Date(s).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' });
const safeUrl = u => (typeof u === 'string' && /^slack:\/\/channel\?team=[A-Z0-9]+&id=[A-Z0-9]+&message=[\d.]+$/.test(u)) ? u : '';
const cites = t => esc(t).replace(/\[#(\d+)\]/g, '<button class="cite" data-act="trace" data-id="$1">#$1</button>');
const EMAIL_RE = /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$/;
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

async function api(path, opts = {}) {
  const method = opts.method || 'GET', h = { 'Content-Type': 'application/json' };
  if (S.csrf && method !== 'GET') h['X-CSRF-Token'] = S.csrf;
  let r;
  try { r = await fetch(path, { ...opts, headers: h, credentials: 'same-origin' }); }
  catch { const e = new Error('Cannot reach the server. Check your connection.'); e.code = 'network'; throw e; }
  const t = await r.text(); let d; try { d = JSON.parse(t); } catch { d = t; }
  if (!r.ok) {
    const det = (d && d.detail) || d; const o = typeof det === 'object' && det ? det : { message: String(det) };
    const e = new Error(validationMessage(det) || o.message || 'Request failed'); Object.assign(e, { code: o.code || '', status: r.status, detail: o });
    if (r.status === 401 && o.code === 'not_authenticated') showAuth();
    throw e;
  }
  return d;
}
const flash = (kind, msg) => { $('#flash').innerHTML = msg ? `<div class="banner ${kind}">${esc(msg)}</div>` : ''; };
let toastT;
function toast(msg, undo) { $('#toastMsg').textContent = msg; $('#toastUndo').hidden = !undo; $('#toast').classList.add('show'); clearTimeout(toastT); if (!undo) toastT = setTimeout(() => $('#toast').classList.remove('show'), 4500); }

/* ----------------------------------------------------------------- auth -- */
function showAuth(msg) { $('#authOverlay').classList.add('show'); $('#apiStatus').textContent = '● signed out'; if (msg) authErr(msg); }
function authErr(msg) { const b = $('#authErr'); b.textContent = msg; b.hidden = !msg; }
function setAuthMode(m) {
  S.authMode = m; const reg = m === 'register';
  $('#nameRow').hidden = !reg; $('#authGo').textContent = reg ? 'Create account' : 'Sign in'; $('#authToggle').textContent = reg ? 'I have an account' : 'Create account';
  $('#authTitle').textContent = reg ? 'Create your account' : 'Sign in to ContextOS'; $('#aPass').autocomplete = reg ? 'new-password' : 'current-password';
  $('#passHint').textContent = reg ? 'At least 10 characters, including letters and numbers. Do not include your email name.' : ''; authErr('');
}
function validateRegisterPassword(show) {
  const password = $('#aPass').value, emailName = $('#aEmail').value.trim().toLowerCase().split('@')[0];
  const problems = [];
  if (password.length < 10) problems.push('at least 10 characters');
  if (!/[A-Za-z]/.test(password) || !/\d/.test(password)) problems.push('both letters and numbers');
  if (emailName.length >= 4 && password.toLowerCase().includes(emailName)) problems.push('not include your email name');
  const hint = $('#passHint');
  hint.className = 'hint ' + (password ? (problems.length ? 'bad' : 'good') : '');
  hint.textContent = password ? (problems.length ? 'Password needs ' + problems.join(', ') + '.' : '✓ Password meets the signup requirements.') :
    'At least 10 characters, including letters and numbers. Do not include your email name.';
  return problems.length === 0 || !show;
}
function validateEmail(show) {
  const v = $('#aEmail').value.trim(), ok = EMAIL_RE.test(v) && v.length <= 254;
  const h = $('#emailHint'); h.className = 'hint ' + (v ? (ok ? 'good' : 'bad') : '');
  h.textContent = v ? (ok ? '✓ Looks like a valid email' : (show ? 'Enter a valid email, like name@example.com' : '')) : '';
  return ok;
}
function tickLock() {
  if (S.lock > 0) { const m = Math.floor(S.lock / 60), s = S.lock % 60; $('#authGo').disabled = true; authErr(`Too many failed attempts. Try again in ${m}:${String(s).padStart(2, '0')}.`); S.lock--; setTimeout(tickLock, 1000); }
  else { $('#authGo').disabled = false; if (S.lock === 0) authErr(''); }
}
async function submitAuth(e) {
  e.preventDefault(); if (S.lock > 0) return; authErr('');
  if (!validateEmail(true)) { authErr('Please enter a valid email address.'); return; }
  if (S.authMode === 'register' && !validateRegisterPassword(true)) {
    authErr('Please fix the password requirements shown below.');
    return;
  }
  const body = { email: $('#aEmail').value.trim(), password: $('#aPass').value }; if (S.authMode === 'register') body.name = $('#aName').value;
  $('#authGo').disabled = true;
  try {
    const d = await api('/auth/' + (S.authMode === 'register' ? 'register' : 'login'), { method: 'POST', body: JSON.stringify(body) });
    S.csrf = d.csrf_token; $('#aPass').value = ''; $('#authOverlay').classList.remove('show'); start(d.user, true);
  } catch (err) {
    if (err.status === 429 && err.detail && err.detail.retry_after) { S.lock = Math.min(err.detail.retry_after, 86400); tickLock(); }
    else { authErr(human(err)); $('#authGo').disabled = false; }
  }
}

/* --------------------------------------------------------------- shell --- */
async function boot() {
  S.cfg = await fetch('/api/config').then(r => r.json()).catch(() => ({}));
  $('#googleBtn').hidden = !S.cfg.google_enabled; $('#orDiv').hidden = !S.cfg.google_enabled; $('#authToggle').hidden = !S.cfg.registration_open;
  const q = new URLSearchParams(location.search);
  if (q.get('slack')) { const f = SLACK_FLASH[q.get('slack')] || ['warn', 'Slack: ' + q.get('slack')]; flash(f[0], f[1] + (q.get('reason') ? ': ' + q.get('reason') : '')); }
  const loginErr = q.get('login') === 'error' ? (LOGIN_ERR[q.get('reason')] || 'Sign-in failed. Please try again.') : '';
  if (q.toString()) history.replaceState(null, '', '/');
  const me = await fetch('/auth/me', { credentials: 'same-origin' }).then(r => r.json()).catch(() => ({}));
  if (me.authenticated) { S.csrf = me.csrf_token; start(me.user, q.get('welcome') === '1'); } else showAuth(loginErr);
}
function start(user, fresh) {
  S.user = user; $('#who').textContent = user.email; $('#logoutBtn').hidden = false; $('#apiStatus').textContent = '● connected';
  $('#adminNav').hidden = !user.is_admin;
  if (!user.is_admin && S.view === 'pipeline') S.view = 'today';
  go(fresh ? 'today' : S.view);
}
function go(view) {
  if (view === 'pipeline' && !(S.user && S.user.is_admin)) view = 'settings';
  S.view = view; document.querySelectorAll('#nav .item').forEach(b => b.classList.toggle('active', b.dataset.view === view)); render();
}
async function render() {
  const v = $('#view'); clearTimeout(S.poll);
  try {
    ({ today: viewToday, inbox: viewInbox, projects: viewProjects, commitments: viewCommitments, ask: viewAsk, pipeline: viewPipeline, settings: viewSettings })[S.view](v);
    await (S.renderP);
  } catch (e) { if (e.status !== 401) v.innerHTML = `<div class="banner err">${esc(human(e))}</div>`; }
}
function schedule(ms) { clearTimeout(S.poll); S.poll = setTimeout(() => { if (!$('#authOverlay').classList.contains('show') && !document.hidden) render(); else schedule(ms); }, ms); }

/* ------------------------------------------------------------- shared UI -- */
function msgCard(m, opts = {}) {
  S.msgs[m.id] = m;
  const place = m.conv_type === 'im' || m.conv_type === 'mpim' ? m.channel_name : '#' + m.channel_name;
  const link = safeUrl(m.open_url);
  return `<article class="message ${esc(m.priority_label)}"><div class="msgrow"><strong><span class="pill ${esc(m.priority_label)}">${esc(m.priority_label)} · ${m.priority_score}</span> &nbsp;${esc(m.text || '(empty)')}</strong><button class="cite" data-act="trace" data-id="${m.id}" title="See how this was processed">#${m.id}</button></div>
  <div class="meta">${esc(place)} · ${esc(m.author_name || m.user_id || 'unknown')} · ${esc(relt(m.message_created_at))}${m.project_name ? `<button class="tag proj" data-act="open-project" data-id="${m.project_id}">${esc(m.project_name)}</button>` : ''}${m.mentions_me ? '<span class="tag">mentions you</span>' : ''}${m.thread_ts && m.thread_ts !== m.slack_ts ? '<span class="tag">thread reply</span>' : ''}${m.reply_count ? `<span class="tag">${m.reply_count} replies</span>` : ''}${m.edited_at ? '<span class="tag">edited</span>' : ''}${m.is_bot ? '<span class="tag">bot</span>' : ''}${m.is_demo ? '<span class="pill demo">demo</span>' : ''}</div>
  ${opts.noWhy ? '' : `<div class="reason">Why: ${esc(m.reason || '')}</div>`}
  <div class="row"><button class="btn sm" data-act="reply" data-id="${m.id}">Reply</button>${link ? `<a class="btn sm" href="${esc(link)}">Open in Slack</a>` : ''}${opts.actions ? `<button class="btn sm" data-act="snooze" data-id="${m.id}">Snooze 4h</button><button class="btn sm" data-act="handled" data-id="${m.id}">Done</button>` : ''}<button class="btn sm" data-act="remind" data-id="${m.id}">Remind me</button></div></article>`;
}
const section = (title, items, render, empty) => `<div class="col"><h3>${title}<span class="count">${items.length}</span></h3>${items.length ? items.map(render).join('') : `<div class="empty">${empty}</div>`}</div>`;
function connectCard() {
  return `<div class="panel"><h2>Connect your Slack</h2><div class="meta">Connect your own Slack account. Choose what ContextOS may read; change or disconnect it any time in Account settings.</div>
  <label class="perm"><input type="radio" name="preset" value="essentials" checked><b>Read-only: channels you're in + direct messages <span class="pill ok">Recommended</span></b><span>Reads messages to rank what needs you. It cannot post or change anything in Slack.</span></label>
  <label class="perm"><input type="radio" name="preset" value="everything"><b>Read-only + private channels and group DMs</b><span>Everything above, plus private channels and group messages you belong to.</span></label>
  <label class="perm"><input type="checkbox" id="replyOpt"><b>Also let me reply from ContextOS</b><span>Optional. Lets you send replies you've approved. You can skip this and use "Open in Slack" instead.</span></label>
  <div class="hint">On Slack's authorization page, check the workspace selector and choose your own workspace before allowing access. Slack may already be signed in to another account in this browser. We never post without your confirmation, never read channels you're not in, and you can disconnect and delete imported data at any time.</div>
  <div class="row"><button class="btn primary" data-act="connect">Connect Slack</button><button class="btn" data-act="seed-demo">Explore with demo data instead</button></div></div>`;
}

/* ----------------------------------------------------------------- today -- */
function viewToday(v) {
  v.innerHTML = '<div class="empty">Loading your day…</div>';
  S.renderP = (async () => {
    const [t, b, c] = await Promise.all([api('/api/today'), api('/api/briefing').catch(() => null), api('/integrations/slack/connection')]);
    if (S.view !== 'today') return;
    S.conns = c.connections; const has = c.connections.some(x => x.status === 'connected');
    v.innerHTML = `<div class="top"><div><h1>Good day, ${esc((S.user.name || '').split(' ')[0] || 'there')}</h1><div class="sub">${esc(t.date)}</div></div></div>
    ${has ? '' : connectCard()}
    ${b ? `<div class="panel brief"><div class="panelhead"><h2>Daily briefing</h2><button class="btn sm" data-act="brief-refresh">Refresh</button></div><p>${esc(b.text)}</p><div class="meta">${esc(b.provider === 'extractive' ? 'Summary of your lists below' : 'Written by ' + b.provider)} · ${esc(relt(b.generated_at))}</div></div>` : ''}
    <div class="cols"><div class="panel">${section('Needs you now', t.needs_now, m => msgCard(m, { actions: true }), 'Nothing urgent. Nice.')}</div>
    <div class="panel">${section('Due today', t.due_today, r => r.kind === 'event'
      ? `<div class="job"><div>📅</div><div><b>${esc(r.text)}</b><div class="meta">meeting today · ${esc(when(r.due_at))}${r.priority_label ? ` · ${esc(r.priority_label)} priority` : ''}${r.provisional ? ' · time guessed, please check' : ''}</div></div><button class="btn sm" data-act="ev-approve" data-id="${r.id}">Review</button></div>`
      : `<div class="job"><div>${r.overdue ? '⚠' : '⏰'}</div><div><b>${esc(r.text)}</b><div class="meta">${r.overdue ? 'overdue · ' : ''}due ${esc(when(r.due_at))}</div></div><button class="btn sm" data-act="rem-done" data-id="${r.id}">Done</button></div>`, 'No deadlines or meetings today.')}</div>
    <div class="panel">${section('To schedule', t.to_schedule, e => `<div class="job"><div>📅</div><div>${e.priority_label ? `<span class="pill ${esc(e.priority_label)}">${esc(e.priority_label)} · ${e.priority_score}</span> ` : ''}<b>${esc(e.text)}</b><div class="meta">${esc(when(e.proposed_start))}${e.provisional ? ' · time guessed, please check' : ''}</div></div><button class="btn sm" data-act="ev-approve" data-id="${e.id}">Approve</button></div>`, 'No meetings waiting.')}</div>
    <div class="panel">${section('Waiting on others', t.waiting_on_others, m => msgCard(m, { noWhy: true, actions: true }), 'You are not waiting on anyone.')}</div></div>`;
  })();
  schedule(20000);
}

/* ----------------------------------------------------------------- inbox -- */
function viewInbox(v) {
  S.renderP = (async () => {
    const [rows, projs] = await Promise.all([api(`/messages?limit=100${S.prio === 'ALL' ? '' : '&priority=' + S.prio}${S.project ? '&project_id=' + S.project : ''}${S.q ? '&q=' + encodeURIComponent(S.q) : ''}`), api('/api/projects')]);
    if (S.view !== 'inbox') return;
    const focus = document.activeElement && document.activeElement.id === 'qBox';
    v.innerHTML = `<div class="top"><div><h1>Priority inbox</h1><div class="sub">Everything scored and ranked. Your own messages are kept out.</div></div></div>
    <div class="panel"><div class="row" style="margin:0 0 12px"><div class="filters">${['ALL', 'P0', 'P1', 'P2', 'P3'].map(p => `<button class="filter ${S.prio === p ? 'selected' : ''}" data-act="prio" data-p="${p}">${p === 'ALL' ? 'All' : p}</button>`).join('')}</div>
    <select id="projSel" aria-label="Filter by project"><option value="">All projects</option>${projs.map(p => `<option value="${p.id}" ${String(p.id) === String(S.project) ? 'selected' : ''}>${esc(p.name)} (${p.message_count})</option>`).join('')}</select>
    <input id="qBox" type="search" placeholder="Search messages" maxlength="100" value="${esc(S.q)}"></div>
    ${rows.length ? rows.map(m => msgCard(m)).join('') : '<div class="empty">No messages match. Connect Slack in Settings, or load demo data.</div>'}</div>`;
    if (focus) { const q = $('#qBox'); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  })();
  schedule(20000);
}

/* -------------------------------------------------------------- projects -- */
function viewProjects(v) {
  S.renderP = (async () => {
    const list = await api('/api/projects'); const max = Math.max(1, ...list.map(p => p.message_count));
    if (S.view !== 'projects') return;
    if (!S.sel && list.length) S.sel = list[0].id;
    v.innerHTML = `<div class="top"><div><h1>Projects</h1><div class="sub">Projects can be discovered automatically or created from keywords in your synced Slack messages.</div></div><div class="row" style="margin:0"><button class="btn primary" data-act="show-project-form">Add project</button><button class="btn" data-act="rediscover">Re-scan messages</button></div></div>
    <div class="panel" id="projectAddPanel" hidden><h2>Add project from message keywords</h2><div class="meta">Searches your imported Slack messages. A created project is immediately tagged on matching messages and becomes available in Priority inbox filters.</div>
    <div class="row"><label>Project name <input id="projectName" maxlength="120" placeholder="e.g. Nebula launch"></label>
    <label>Keywords <input id="projectKeywords" maxlength="500" placeholder="nebula, nebula launch"></label>
    <button class="btn" type="button" data-act="project-search">Search messages</button><button class="btn primary" type="button" data-act="project-create">Create project</button></div>
    <div id="projectMatchResults" class="meta" aria-live="polite">Search keywords to preview matching messages before creating the project.</div></div>
    <div class="list"><div class="panel"><h2>${list.length} project${list.length === 1 ? '' : 's'}</h2>${list.map(p => `<button class="proj ${p.id === S.sel ? 'sel' : ''}" data-act="select-project" data-id="${p.id}"><b>${esc(p.name)}</b> ${p.p0_count ? `<span class="pill P0">${p.p0_count} critical</span>` : ''}<div class="meta" style="margin:4px 0 0">${p.message_count} messages · last ${esc(relt(p.last_activity_at))}</div><div class="meter"><i style="width:${Math.round(100 * p.message_count / max)}%"></i></div></button>`).join('') || '<div class="empty">No projects yet. They appear once a name is mentioned across several messages.</div>'}</div>
    <div class="panel" id="projDetail"><div class="empty">${list.length ? 'Loading…' : 'Select a project'}</div></div></div>`;
    if (S.sel && list.some(p => p.id === S.sel)) await loadProject(S.sel);
  })();
}
async function loadProject(id) {
  const p = await api('/api/projects/' + id), max = Math.max(1, ...p.timeline.map(t => t.n));
  $('#projDetail').innerHTML = `<div class="panelhead"><h2>${esc(p.name)}</h2><div class="row" style="margin:0"><button class="btn sm" data-act="proj-sum" data-id="${p.id}">Regenerate summary</button><button class="btn sm" data-act="proj-rename" data-id="${p.id}">Rename</button><button class="btn sm danger" data-act="proj-archive" data-id="${p.id}">Archive</button></div></div>
  <div class="meta">Matched keywords: ${p.keywords.map(k => `<span class="tag">${esc(k)}</span>`).join('')} <button class="btn sm" data-act="proj-kw" data-id="${p.id}">+ keyword</button></div>
  <div class="sum">${p.summary ? cites(p.summary) : 'No summary yet.'}</div><div class="meta">${p.summary_at ? `Summary ${esc(relt(p.summary_at))} · ${esc(p.summary_source)}` : ''}</div>
  <h3>Activity, last 14 days</h3><div class="spark" aria-label="Messages per day">${p.timeline.map(t => `<i title="${esc(t.day)}: ${t.n}" style="height:${Math.max(4, Math.round(100 * t.n / max))}%"></i>`).join('')}</div>
  <div class="cols"><div><h3>People <span class="tag">${esc(p.graph.source)}</span></h3>${p.graph.people.map(x => `<div class="meta">${esc(x.name)} · ${x.n} messages</div>`).join('') || '<div class="meta">n/a</div>'}</div>
  <div><h3>Also talked about</h3>${p.topics.map(t => `<span class="tag">${esc(t)}</span>`).join('') || '<div class="meta">n/a</div>'}</div></div>
  <h3>Key messages</h3>${p.messages.map(m => msgCard(m, { noWhy: true })).join('')}`;
}

/* ----------------------------------------------------------- commitments -- */
const personLabel = p => `${p.name || p.username || p.email || 'Slack user'}${p.username ? ` · @${p.username}` : ''}${p.email ? ` · ${p.email}` : ''}`;
const personSelectOptions = selected => S.contacts.map(p =>
  `<option value="${esc(p.id)}" ${selected.includes(p.id) ? 'selected' : ''}>${esc(personLabel(p))}</option>`).join('');
const assigneeTags = people => people.length ? people.map(p =>
  `<span class="tag">${esc(p.name || p.username || p.email)}${p.email ? ` · ${esc(p.email)}` : p.username ? ` · @${esc(p.username)}` : ''}</span>`).join('') : '<span class="meta">Unassigned</span>';
const selectedPeople = selector => [...$(selector).selectedOptions].map(o => o.value);
async function searchSlackPeople(query) {
  const current = S.contacts;
  const selected = new Set([...document.querySelectorAll('select[multiple]')].flatMap(select => [...select.selectedOptions].map(o => o.value)));
  if (S.personFilter) selected.add(S.personFilter);
  try {
    const results = await api('/integrations/slack/people?q=' + encodeURIComponent(query));
    if (S.view !== 'commitments' || !$('#peopleFeedback')) return;
    const resultIds = new Set(results.map(person => person.id));
    S.contacts = [...results, ...current.filter(person => selected.has(person.id) && !resultIds.has(person.id))];
    const controls = [...document.querySelectorAll('#personFilter, select[multiple]')];
    for (const control of controls) {
      const chosen = control.multiple ? [...control.selectedOptions].map(option => option.value) : [S.personFilter];
      control.innerHTML = `${control.id === 'personFilter' ? '<option value="">All people</option>' : ''}${personSelectOptions(chosen)}`;
    }
    $('#peopleFeedback').textContent = `${results.length} matching Slack people. Select one or more people in each assignment list.`;
  } catch (e) {
    if (S.view === 'commitments' && $('#peopleFeedback')) $('#peopleFeedback').textContent = `Slack people could not be loaded: ${human(e)}`;
  }
}

function viewCommitments(v) {
  S.renderP = (async () => {
    const filter = S.personFilter ? `&person_id=${encodeURIComponent(S.personFilter)}` : '';
    const oldContacts = S.contacts;
    const preserveIds = new Set([S.personFilter, ...[...document.querySelectorAll('select[multiple]')].flatMap(select => [...select.selectedOptions].map(o => o.value))]);
    const [rem, ev] = await Promise.all([api('/reminders?status=all' + filter), api('/schedule/suggestions?status=all' + filter)]);
    let peopleWarning = '';
    try { S.contacts = await api('/integrations/slack/people?q=' + encodeURIComponent(S.peopleSearch)); }
    catch (e) { S.contacts = []; peopleWarning = human(e); }
    const foundIds = new Set(S.contacts.map(person => person.id));
    S.contacts.push(...oldContacts.filter(person => preserveIds.has(person.id) && !foundIds.has(person.id)));
    if (S.view !== 'commitments') return;
    v.innerHTML = `<div class="top"><div><h1>Commitments &amp; schedule</h1><div class="sub">Assign and filter reminders and events using people from your own connected Slack workspaces.</div></div>
    <select id="personFilter" aria-label="Filter by person"><option value="">All people</option>${personSelectOptions(S.personFilter ? [S.personFilter] : [])}</select></div>
    ${peopleWarning ? `<div class="banner warn">Slack people could not be loaded: ${esc(peopleWarning)}</div>` : S.contacts.length ? '' : '<div class="banner info">Connect and sync your Slack account in Account settings to select workspace people.</div>'}
    <div class="panel"><label for="peopleSearch">Find Slack people by name, username, or email</label><input id="peopleSearch" type="search" value="${esc(S.peopleSearch)}" placeholder="Search your connected workspace directory"><div id="peopleFeedback" class="hint">Select people below to tag them or filter the lists.</div></div>
    <div class="cols"><div class="panel"><h2>Reminders</h2>
    ${rem.map(r => `<div class="conn"><b>${esc(r.title)}</b><div class="meta">due ${esc(when(r.due_at))} · ${esc(r.status)}</div>${assigneeTags(r.assignees)}
    <label for="remAssignees-${r.id}">Tagged people</label><select id="remAssignees-${r.id}" multiple size="3" data-reminder-assignees="${r.id}">${personSelectOptions(r.assignees.map(p => p.id))}</select>
    <div class="row"><button class="btn sm" data-act="rem-save-assignees" data-id="${r.id}">Save people</button>${r.status === 'pending' ? `<button class="btn sm" data-act="rem-done" data-id="${r.id}">Done</button>` : ''}</div></div>`).join('') || '<div class="empty">No reminders yet.</div>'}
    <h3>Add a reminder</h3><label for="remTitle">Title</label><input id="remTitle" class="field" maxlength="300" placeholder="What needs follow-up?">
    <label for="remDue">Due date and time</label><input id="remDue" type="datetime-local">
    <label for="remAssignees">Tag Slack people</label><select id="remAssignees" multiple size="4">${personSelectOptions([])}</select>
    <div class="row"><button class="btn primary" data-act="rem-create">Add reminder</button></div></div>
    <div class="panel"><h2>Meeting suggestions</h2>
    ${ev.map(e => `<div class="conn">${e.priority_label ? `<span class="pill ${esc(e.priority_label)}">${esc(e.priority_label)} · ${e.priority_score}</span> ` : ''}<b>${esc(e.title)}</b><div class="meta">${esc(when(e.proposed_start))} · ${e.duration_minutes} min · ${esc(e.status)}${e.is_provisional ? ' · time guessed' : ''}</div>${assigneeTags(e.assignees)}
    <label for="eventAssignees-${e.id}">Tagged people</label><select id="eventAssignees-${e.id}" multiple size="3" data-event-assignees="${e.id}">${personSelectOptions(e.assignees.map(p => p.id))}</select>
    <div class="row"><button class="btn sm" data-act="event-save-assignees" data-id="${e.id}">Save people</button>${e.status === 'suggested' ? `<button class="btn sm primary" data-act="ev-approve" data-id="${e.id}">Approve</button><button class="btn sm" data-act="ev-calendar" data-id="${e.id}">+ Calendar</button>` : ''}</div></div>`).join('') || '<div class="empty">No suggestions yet.</div>'}
    <h3>Add a meeting</h3><label for="evTitle">Title</label><input id="evTitle" class="field" placeholder="Meeting title" maxlength="200">
    <label for="evStart">Start date and time</label><input id="evStart" type="datetime-local">
    <label for="evAssignees">Tag Slack people</label><select id="evAssignees" multiple size="4">${personSelectOptions([])}</select>
    <div class="row"><button class="btn primary" data-act="ev-create">Add meeting</button></div></div></div>`;
  })();
  schedule(30000);
}

/* ------------------------------------------------------------------- ask -- */
function viewAsk(v) {
  S.renderP = (async () => {
    const projs = await api('/api/projects');
    if (S.view !== 'ask') return;
    const chips = [...projs.slice(0, 3).map(p => `What's the status of ${p.name}?`), 'What needs my attention today?', 'What decisions were made this week?'];
    v.innerHTML = `<div class="top"><div><h1>Ask ContextOS</h1><div class="sub">Answers come only from your Slack messages and cite their sources.</div></div></div>
    <div class="panel"><form id="askForm"><div class="row" style="margin:0"><input id="askQ" class="field" style="flex:1" maxlength="1000" placeholder="e.g. What's blocking ${esc(projs[0] ? projs[0].name : 'the launch')}?"><button class="btn primary" type="submit" id="askGo">Ask</button></div></form>
    <div class="row">${chips.map(c => `<button class="filter" data-act="ask-chip" data-q="${esc(c)}">${esc(c)}</button>`).join('')}</div></div><div id="askOut">${S.ask.map(askCard).join('')}</div>`;
  })();
}
const askCard = a => `<div class="panel"><b>${esc(a.q)}</b><div class="ans">${cites(a.answer)}</div><div class="meta">${a.mode.startsWith('llm') ? 'AI-written from your messages' : 'Matching messages (AI not used)'}${a.projects.length ? ' · project: ' + esc(a.projects.join(', ')) : ''}</div>
  ${a.sources.map(s => `<div class="src"><b>#${s.message_id}</b> ${esc(s.channel)} · ${esc(s.author || '')} · ${esc(relt(s.when))}<br>${esc(s.text)} ${safeUrl(s.open_url) ? `<a href="${esc(safeUrl(s.open_url))}">Open in Slack</a>` : ''}</div>`).join('')}</div>`;

/* -------------------------------------------------------------- pipeline -- */
function viewPipeline(v) {
  S.renderP = (async () => {
    const [o, admin] = await Promise.all([api('/api/pipeline/overview'), api('/api/admin/settings')]);
    if (S.view !== 'pipeline') return;
    if (!$('#feed')) {
      v.innerHTML = `<div class="top"><div><h1>Data pipeline</h1><div class="sub">Every layer your Slack data passes through, live. Sign-in → Slack → ingress → queue → worker → store → score → extract → projects → search/graph → LLM.</div></div>
      <div class="row" style="margin:0"><button class="btn" data-act="feed-pause" id="pauseBtn">Pause feed</button><button class="btn" data-act="seed-demo">Load demo data</button></div></div>
      <div class="panel"><div class="panelhead"><h2>App settings</h2><span class="meta">Owner-only · change secrets in the server environment</span></div>
      <div class="meta">Owner: ${esc(admin.admin_email)} · environment: ${esc(admin.app_env)} · public URL: ${esc(admin.public_base_url)}</div>
      <div class="meta">Slack OAuth: ${admin.slack_oauth_configured ? 'configured' : 'not configured'} · OAuth callback: <code>${esc(admin.slack_redirect_uri)}</code></div>
      <div class="meta">Google sign-in: ${admin.google_enabled ? 'configured' : 'not configured'} · Redis: ${admin.redis_configured ? 'configured' : 'off'} · Qdrant: ${admin.qdrant_configured ? 'configured' : 'off'} · Neo4j: ${admin.neo4j_configured ? 'configured' : 'off'} · LLM providers: ${esc(admin.llm_providers.join(', ') || 'none')}</div></div>
      <div id="demoMsg"></div><div class="layers" id="layers"></div>
      <div class="panel" style="margin-top:16px"><div class="panelhead"><h2>Live event feed</h2><select id="stageSel" aria-label="Filter stage"><option value="">All stages</option>${Object.entries(STAGE_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select></div><div class="feed" id="feed"></div></div>`;
      S.feed = []; S.feedAfter = 0;
    }
    $('#layers').innerHTML = o.layers.map(l => `<div class="layer"><h4><span class="dot ${esc(l.status)}"></span>${esc(l.name)}</h4><div class="hl">${esc(l.headline)}</div><div class="kv">${Object.entries(l.metrics).filter(([, x]) => x !== null && x !== undefined && typeof x !== 'object').map(([k, x]) => `${esc(k.replaceAll('_', ' '))}: <b>${esc(x)}</b>`).join(' · ')}${l.metrics.top ? '<br>' + l.metrics.top.map(t => `${esc(t.name)} ${t.messages}`).join(' · ') : ''}${l.metrics.by_status ? '<br>' + Object.entries(l.metrics.by_status).map(([k, x]) => `${esc(k)} ${x}`).join(' · ') : ''}${l.metrics.usage && Object.keys(l.metrics.usage).length ? '<br>' + Object.entries(l.metrics.usage).map(([k, x]) => `${esc(k)}: ${x.calls_today}/${x.daily_budget} calls today`).join(' · ') : ''}</div></div>`).join('');
    await pullFeed();
  })();
  schedule(3000);
}
async function pullFeed() {
  if (S.paused || !$('#feed')) return;
  const st = $('#stageSel') ? $('#stageSel').value : '';
  const rows = await api(`/api/pipeline/events?limit=80&after_id=${S.feedAfter}${st ? '&stage=' + st : ''}`);
  if (rows.length) { S.feedAfter = Math.max(S.feedAfter, ...rows.map(r => r.id)); S.feed = [...rows, ...S.feed].slice(0, 200); }
  $('#feed').innerHTML = S.feed.map(e => `<div class="ev ${esc(e.status)}"><span class="st">${esc(e.status)}</span><span><b>${esc(STAGE_LABEL[e.stage] || e.stage)}</b></span><span class="d">${esc(e.detail)}</span><span class="meta" style="margin:0">${e.message_id ? `<button class="cite" data-act="trace" data-id="${e.message_id}">#${e.message_id}</button> ` : ''}${esc(relt(e.at))}</span></div>`).join('') || '<div class="empty">No events yet. Load demo data or connect Slack.</div>';
}

/* -------------------------------------------------------------- settings -- */
function connHtml(c) {
  const pct = c.conversations_total ? Math.round(100 * c.conversations_done / c.conversations_total) : (c.sync_state === 'idle' ? 100 : 0);
  const st = { connected: 'ok', needs_reauth: 'err', revoked: 'err' }[c.status] || 'warn';
  const probs = (c.problems || []).map(p => `<div class="banner ${p.code === 'ratelimited' ? 'warn' : 'err'}">${esc(ERR[p.code] && p.code !== 'missing_scope' ? ERR[p.code] : p.message)}</div>`).join('');
  const mode = c.can_reply_as_user ? '<span class="pill ok">Replies from ContextOS enabled</span>' : '<span class="pill idle">Read-only (reply via “Open in Slack”)</span>';
  return `<div class="conn"><div class="msgrow"><b>${esc(c.team_name || c.team_id)}</b><span><span class="pill ${st}">${esc(c.status)}</span>${c.is_demo ? ' <span class="pill demo">demo</span>' : ''}</span></div>
  <div class="meta">Slack user ${esc(c.slack_user_id)}${c.slack_email ? ' · ' + esc(c.slack_email) : ''}${c.slack_email && S.user && c.slack_email.toLowerCase() !== S.user.email.toLowerCase() ? ' <span class="tag">different email than your ContextOS account</span>' : ''}</div>
  <div class="meta">${c.status === 'connected' ? mode : 'Slack is disconnected. Your imported messages are kept unless you chose to delete them.'} ${c.status === 'connected' && c.can_read_private ? '<span class="pill ok">Private channels</span>' : ''}</div>
  <div class="meta">${c.messages_synced} messages synced · last sync ${esc(relt(c.last_sync_finished_at))} · sync <b>${esc(c.sync_state)}</b>${c.backfill_pending ? ` · ${c.backfill_pending} conversations still backfilling` : ''} · ${c.conversations_done}/${c.conversations_total} conversations</div><div class="bar"><i style="width:${pct}%"></i></div>${probs}
  <div class="row"><button class="btn sm" data-act="sync" data-id="${c.id}" ${c.status !== 'connected' ? 'disabled' : ''}>Sync now</button>
  ${!c.is_demo && c.status === 'connected' && !c.can_reply_as_user ? '<button class="btn sm" data-act="upgrade" data-kind="reply">Enable replying</button>' : ''}
  ${!c.is_demo && c.status === 'connected' && !c.can_read_private ? '<button class="btn sm" data-act="upgrade" data-kind="private">Add private channels</button>' : ''}
  ${!c.is_demo && c.status !== 'connected' ? `<button class="btn sm primary" data-act="upgrade" data-kind="reconnect" data-id="${c.id}">Reconnect</button>` : ''}
  ${c.is_demo && c.status !== 'connected' ? '<button class="btn sm primary" data-act="restore-demo">Restore demo connection</button>' : ''}
  ${c.status === 'connected' ? `<button class="btn sm danger" data-act="disconnect" data-id="${c.id}">Disconnect</button>` : ''}</div></div>`;
}
function viewSettings(v) {
  S.renderP = (async () => {
    const [d, sessions] = await Promise.all([api('/integrations/slack/connection'), api('/auth/sessions')]); S.conns = d.connections;
    if (S.view !== 'settings') return;
    v.innerHTML = `<div class="top"><div><h1>Account settings</h1><div class="sub">Manage your ContextOS profile, sessions, and personal Slack connections.</div></div></div>
    <div class="cols"><div class="panel"><h2>Profile</h2><label for="profileEmail">Account email</label><input id="profileEmail" type="email" value="${esc(S.user.email)}" disabled>
    <label for="profileName">Display name</label><input id="profileName" value="${esc(S.user.name || '')}" maxlength="200">
    <div class="row"><button class="btn primary" data-act="save-profile">Save profile</button></div></div>
    <div class="panel"><h2>Password</h2>${S.user.has_password ? `<label for="currentPassword">Current password</label><input id="currentPassword" type="password" autocomplete="current-password">
    <label for="newPassword">New password</label><input id="newPassword" type="password" autocomplete="new-password">
    <div class="hint">At least 10 characters, with letters and numbers.</div><div class="row"><button class="btn primary" data-act="change-password">Update password</button></div>` : '<div class="meta">This account uses Google sign-in and does not have a ContextOS password.</div>'}</div></div>
    <div class="panel"><div class="panelhead"><h2>Active sessions</h2><span class="meta">${sessions.length} session(s)</span></div>
    ${sessions.map(s => `<div class="job"><div>🔐</div><div><b>${s.current ? 'This session' : 'Signed-in session'}</b><div class="meta">Started ${esc(when(s.created_at))} · expires ${esc(when(s.expires_at))}</div></div>${s.current ? '<span class="pill ok">Current</span>' : `<button class="btn sm danger" data-act="revoke-session" data-id="${s.id}">Revoke</button>`}</div>`).join('')}</div>
    <div class="panel"><div class="panelhead"><h2>Your Slack connections</h2><button class="btn primary" data-act="connect">Add a Slack connection</button></div>${d.connections.map(connHtml).join('') || '<div class="empty">No workspace connected yet.</div>'}${d.connections.length ? '' : connectCard()}</div>`;
  })();
  schedule(30000);
}

/* ---------------------------------------------------------------- reply --- */
function openReply(id) {
  const m = S.msgs[id]; if (!m) return; const c = S.conns.find(x => x.id === m.connection_id);
  S.reply = { m, key: uuid(), tone: 'professional' }; $('#replyErr').hidden = true;
  $('#replyCtx').textContent = `Replying to ${m.author_name || m.user_id || 'unknown'} in ${m.channel_name}: “${m.text.slice(0, 160)}”`;
  const canSend = !!c && c.status === 'connected' && c.can_reply_as_user; const link = safeUrl(m.open_url);
  $('#replyBody').innerHTML = `${canSend ? '' : `<div class="banner info">Your Slack connection is read-only or disconnected. Draft a reply here, copy it, and finish in Slack. You can reconnect or enable sending from Account settings.</div>`}
  <div class="row" style="margin-top:0"><select id="tone" aria-label="Tone">${['professional', 'friendly', 'concise', 'assertive', 'empathetic'].map(t => `<option>${t}</option>`).join('')}</select><button class="btn sm" data-act="suggest">✦ Suggest a reply</button><span class="hint" id="sugHint"></span></div>
  <label for="replyText">Your reply${canSend ? ' (posted into the thread)' : ''}</label><textarea id="replyText" maxlength="4000"></textarea>
  ${canSend ? `<label class="perm" style="margin-top:12px"><input type="checkbox" id="replyOk"><b>I reviewed this reply and want to send it${m.is_demo ? ' (demo: nothing is sent)' : ''}</b><span>It is sent as you, ${m.is_demo ? '' : 'after a 10-second window where you can undo.'}</span></label>` : ''}
  <div class="row">${canSend ? '<button class="btn primary" id="replySend" data-act="send-reply" disabled>Send reply</button>' : '<button class="btn" data-act="copy-draft">Copy draft</button>'}${link ? `<a class="btn" href="${esc(link)}">Open in Slack</a>` : ''}<button class="btn" data-act="close-reply">Cancel</button></div>`;
  $('#replyOverlay').classList.add('show'); $('#replyText').focus();
}
const syncSend = () => { const b = $('#replySend'); if (b) b.disabled = !($('#replyOk') && $('#replyOk').checked) || !$('#replyText').value.trim(); };
function queueSend() {
  const r = S.reply, text = $('#replyText').value; $('#replyOverlay').classList.remove('show'); let n = 10;
  S.pending = { r, text }; const tick = () => { if (!S.pending) return; if (n <= 0) { fireSend(); return; } toast(`Sending in ${n}s…`, true); n--; S.sendTimer = setTimeout(tick, 1000); }; tick();
}
async function fireSend() {
  const { r, text } = S.pending; S.pending = null; $('#toastUndo').hidden = true;
  try { const d = await api(`/messages/${r.m.id}/reply`, { method: 'POST', body: JSON.stringify({ text, approved: true, send_as: 'user', client_request_id: r.key }) }); toast(d.simulated ? 'Demo reply recorded (nothing sent to Slack).' : 'Reply sent to Slack.'); }
  catch (e) { toast(human(e)); if (e.code !== 'ambiguous') flash('err', 'Reply not sent: ' + human(e)); else flash('warn', human(e)); }
}

/* ---------------------------------------------------------------- trace --- */
async function openTrace(id) {
  $('#traceOverlay').classList.add('show'); $('#traceBody').innerHTML = '<div class="empty">Loading…</div>';
  try {
    const t = await api('/api/pipeline/trace/' + id);
    $('#traceBody').innerHTML = `<div class="sum">${esc(t.message.text)}</div><div class="meta">${esc(t.message.channel_name)} · ${esc(t.message.author_name || '')} · ${esc(relt(t.message.message_created_at))}</div>
    ${t.events.length ? t.events.map(e => `<div class="ev ${esc(e.status)}"><span class="st">${esc(e.status)}</span><span><b>${esc(STAGE_LABEL[e.stage] || e.stage)}</b></span><span class="d">${esc(e.detail)}</span><span class="meta" style="margin:0">${esc(relt(e.at))}</span></div>`).join('') : '<div class="empty">No trace rows (older than the retention window).</div>'}`;
  } catch (e) { $('#traceBody').innerHTML = `<div class="banner err">${esc(human(e))}</div>`; }
}

/* ------------------------------------------------------------- actions ---- */
const A = {
  refresh: () => render(), logout: async () => { try { await api('/auth/logout', { method: 'POST' }); } catch { /* ignore */ } S.csrf = ''; location.href = '/'; },
  'save-profile': async () => {
    try { S.user = { ...S.user, ...await api('/auth/account', { method: 'PATCH', body: JSON.stringify({ name: $('#profileName').value }) }) }; toast('Profile updated.'); render(); }
    catch (e) { toast(human(e)); }
  },
  'change-password': async () => {
    try {
      await api('/auth/password', { method: 'POST', body: JSON.stringify({ current_password: $('#currentPassword').value, new_password: $('#newPassword').value }) });
      $('#currentPassword').value = ''; $('#newPassword').value = ''; toast('Password updated.');
    } catch (e) { toast(human(e)); }
  },
  'revoke-session': async el => {
    if (!confirm('Sign out this session on its device?')) return;
    try { await api(`/auth/sessions/${el.dataset.id}`, { method: 'DELETE' }); toast('Session revoked.'); render(); }
    catch (e) { toast(human(e)); }
  },
  'toggle-auth': () => setAuthMode(S.authMode === 'login' ? 'register' : 'login'),
  connect: () => { const p = ($('input[name=preset]:checked') || {}).value || 'essentials'; location.href = `/integrations/slack/connect?preset=${encodeURIComponent(p)}&reply=${$('#replyOpt') && $('#replyOpt').checked ? 'true' : 'false'}`; },
  upgrade: el => { const k = el.dataset.kind, c = S.conns.find(x => x.id === +el.dataset.id) || S.conns.find(x => x.status === 'connected') || S.conns[0]; const priv = k === 'private' || (c && c.can_read_private); location.href = `/integrations/slack/connect?preset=${priv ? 'everything' : 'essentials'}&reply=${k === 'reply' || (c && c.can_reply_as_user) ? 'true' : 'false'}&keep=true`; },
  'restore-demo': async () => { try { await api('/api/demo/seed', { method: 'POST' }); toast('Demo connection restored.'); render(); } catch (e) { toast(human(e)); } },
  sync: async el => { try { await api('/integrations/slack/sync', { method: 'POST', body: JSON.stringify({ connection_id: +el.dataset.id, mode: 'reconcile' }) }); toast('Recent Slack history refresh queued.'); render(); } catch (e) { toast(human(e)); } },
  disconnect: async el => { if (!confirm('Disconnect this Slack workspace? Syncing and replying stop immediately.')) return; const purge = confirm('Also delete all messages imported from it?\nOK = delete them, Cancel = keep them.'); try { await api('/integrations/slack/disconnect', { method: 'POST', body: JSON.stringify({ connection_id: +el.dataset.id, purge }) }); toast('Disconnected.'); render(); } catch (e) { toast(human(e)); } },
  'seed-demo': async () => { const m = $('#demoMsg'); if (m) m.innerHTML = '<div class="banner info">Feeding ~600 demo events through the pipeline…</div>'; try { const d = await api('/api/demo/seed', { method: 'POST' }); const msg = d.worker_needed ? `${d.events_accepted} events queued; ${d.jobs_remaining} jobs left. Make sure the worker is running (python -m app.worker); this page updates as it works.` : `Done: ${d.events_accepted} events, ${d.duplicate_events_dropped} replays dropped.`; if (m) m.innerHTML = `<div class="banner ok">${esc(msg)}</div>`; toast('Demo data loaded'); go(S.user.is_admin ? 'pipeline' : 'today'); } catch (e) { if (m) m.innerHTML = `<div class="banner err">${esc(human(e))}</div>`; } },
  'reset-demo': async () => { if (!confirm('Remove the demo workspace and its data?')) return; try { await api('/api/demo/reset', { method: 'POST' }); toast('Demo data removed.'); render(); } catch (e) { toast(human(e)); } },
  prio: el => { S.prio = el.dataset.p; render(); },
  reply: el => openReply(+el.dataset.id), 'close-reply': () => $('#replyOverlay').classList.remove('show'),
  suggest: async () => { const h = $('#sugHint'); h.textContent = 'Thinking…'; try { const d = await api(`/messages/${S.reply.m.id}/suggest-reply`, { method: 'POST', body: JSON.stringify({ tone: $('#tone').value }) }); $('#replyText').value = d.draft; h.textContent = `${d.response_type.replace('_', ' ')} · ${d.source === 'template' ? 'template (AI unavailable)' : 'drafted by ' + d.source}`; syncSend(); } catch (e) { h.textContent = human(e); } },
  'copy-draft': async () => { try { await navigator.clipboard.writeText($('#replyText').value); toast('Draft copied.'); } catch { toast('Select the text and copy it manually.'); } },
  'send-reply': () => queueSend(),
  'undo-send': () => { clearTimeout(S.sendTimer); S.pending = null; $('#toast').classList.remove('show'); toast('Send cancelled. Nothing was posted.'); },
  handled: async el => { try { await api(`/messages/${el.dataset.id}/handled`, { method: 'POST' }); render(); } catch (e) { toast(human(e)); } },
  snooze: async el => { try { await api(`/messages/${el.dataset.id}/snooze`, { method: 'POST', body: JSON.stringify({ hours: 4 }) }); toast('Snoozed for 4 hours.'); render(); } catch (e) { toast(human(e)); } },
  remind: async el => { const m = S.msgs[el.dataset.id]; const due = prompt('Remind me when? (e.g. 2026-10-23 17:00)'); if (!due) return; const dt = new Date(due.replace(' ', 'T')); if (isNaN(dt)) { toast('Could not read that date.'); return; } try { await api('/reminders', { method: 'POST', body: JSON.stringify({ title: (m.text || 'Follow up').slice(0, 280), due_at: dt.toISOString(), message_id: m.id }) }); toast('Reminder added.'); } catch (e) { toast(human(e)); } },
  'rem-create': async () => {
    const title = $('#remTitle').value.trim(), due = $('#remDue').value;
    if (!title || !due) { toast('Enter a reminder title and due date.'); return; }
    try {
      await api('/reminders', { method: 'POST', body: JSON.stringify({ title, due_at: new Date(due).toISOString(), assignees: selectedPeople('#remAssignees') }) });
      toast('Reminder added.'); render();
    } catch (e) { toast(human(e)); }
  },
  'rem-save-assignees': async el => {
    try {
      await api(`/reminders/${el.dataset.id}/assignees`, { method: 'PATCH', body: JSON.stringify({ assignees: selectedPeople(`#remAssignees-${el.dataset.id}`) }) });
      toast('Reminder people updated.'); render();
    } catch (e) { toast(human(e)); }
  },
  'rem-done': async el => { try { await api(`/reminders/${el.dataset.id}/complete`, { method: 'POST' }); render(); } catch (e) { toast(human(e)); } },
  'ev-approve': async el => { try { await api(`/schedule/suggestions/${el.dataset.id}/approve`, { method: 'POST' }); render(); } catch (e) { toast(human(e)); } },
  'ev-calendar': async el => { try { await api(`/schedule/suggestions/${el.dataset.id}/approve-and-create`, { method: 'POST' }); toast('Added to calendar.'); render(); } catch (e) { toast(human(e)); } },
  'ev-create': async () => { const t = $('#evTitle').value.trim(), s = $('#evStart').value; if (!t || !s) { toast('Enter a title and a start time.'); return; } try { await api('/schedule/suggestions', { method: 'POST', body: JSON.stringify({ title: t, proposed_start: new Date(s).toISOString(), duration_minutes: 30, source_text: 'Created manually', assignees: selectedPeople('#evAssignees') }) }); toast('Meeting added.'); render(); } catch (e) { toast(human(e)); } },
  'event-save-assignees': async el => {
    try {
      await api(`/schedule/suggestions/${el.dataset.id}/assignees`, { method: 'PATCH', body: JSON.stringify({ assignees: selectedPeople(`#eventAssignees-${el.dataset.id}`) }) });
      toast('Meeting people updated.'); render();
    } catch (e) { toast(human(e)); }
  },
  'brief-refresh': async () => { try { await api('/api/briefing?refresh=true'); render(); } catch (e) { toast(human(e)); } },
  'select-project': el => { S.sel = +el.dataset.id; document.querySelectorAll('.proj').forEach(b => b.classList.toggle('sel', b === el)); loadProject(S.sel).catch(e => toast(human(e))); },
  'open-project': el => { S.sel = +el.dataset.id; go('projects'); },
  rediscover: async () => { try { toast('Scanning…'); await api('/api/projects/discover', { method: 'POST' }); render(); } catch (e) { toast(human(e)); } },
  'show-project-form': () => { const panel = $('#projectAddPanel'); panel.hidden = !panel.hidden; if (!panel.hidden) $('#projectName').focus(); },
  'project-search': async () => {
    const raw = $('#projectKeywords').value.trim();
    if (!raw) { toast('Enter one or more keywords to search.'); return; }
    try {
      const result = await api('/api/projects/matches?keywords=' + encodeURIComponent(raw));
      S.projectMatchKey = raw.toLowerCase().split(',').map(k => k.trim()).filter(Boolean).sort().join(',');
      const samples = result.messages.map(m => `<div class="message ${esc(m.priority_label || '')}">${esc(m.text)}<div class="meta">${esc(relt(m.created_at))}</div></div>`).join('');
      $('#projectMatchResults').innerHTML = `<b>${result.count} matching message${result.count === 1 ? '' : 's'} found</b>${samples || '<div class="meta">No saved Slack messages match these keywords yet. Sync Slack history first, or create this project for future messages.</div>'}`;
    } catch (e) { $('#projectMatchResults').textContent = human(e); }
  },
  'project-create': async () => {
    const name = $('#projectName').value.trim(), keywords = $('#projectKeywords').value.trim();
    if (!name || !keywords) { toast('Enter a project name and at least one keyword.'); return; }
    const key = keywords.toLowerCase().split(',').map(k => k.trim()).filter(Boolean).sort().join(',');
    if (key !== S.projectMatchKey) { toast('Search these keywords before creating the project.'); return; }
    try {
      const project = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name, keywords }) });
      S.sel = project.id; S.projectMatchKey = ''; toast(`Project created; ${project.matched} matching message(s), ${project.tagged} newly tagged.`);
      render();
    } catch (e) { toast(human(e)); }
  },
  'proj-sum': async el => { try { toast('Summarising…'); await api(`/api/projects/${el.dataset.id}/summarize`, { method: 'POST' }); loadProject(+el.dataset.id); } catch (e) { toast(human(e)); } },
  'proj-rename': async el => { const n = prompt('New project name:'); if (!n || !n.trim()) return; try { await api(`/api/projects/${el.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ name: n.trim() }) }); render(); } catch (e) { toast(human(e)); } },
  'proj-kw': async el => { const k = prompt('Another word that means this project:'); if (!k || k.trim().length < 2) return; try { await api(`/api/projects/${el.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ add_keyword: k.trim() }) }); await api('/api/projects/discover', { method: 'POST' }); render(); } catch (e) { toast(human(e)); } },
  'proj-archive': async el => { if (!confirm('Archive this project? It stops matching new messages.')) return; try { await api(`/api/projects/${el.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ status: 'archived' }) }); S.sel = null; render(); } catch (e) { toast(human(e)); } },
  'ask-chip': el => { $('#askQ').value = el.dataset.q; $('#askForm').requestSubmit(); },
  trace: el => openTrace(el.dataset.id), 'close-trace': () => $('#traceOverlay').classList.remove('show'),
  'feed-pause': () => { S.paused = !S.paused; $('#pauseBtn').textContent = S.paused ? 'Resume feed' : 'Pause feed'; if (!S.paused) pullFeed(); },
};

document.addEventListener('click', e => {
  const nav = e.target.closest('[data-view]'); if (nav) { go(nav.dataset.view); return; }
  const el = e.target.closest('[data-act]'); if (el && A[el.dataset.act]) { e.preventDefault(); A[el.dataset.act](el); }
});
document.addEventListener('change', e => {
  if (e.target.id === 'projSel') { S.project = e.target.value; render(); }
  if (e.target.id === 'personFilter') { S.personFilter = e.target.value; render(); }
  if (e.target.id === 'stageSel') { S.feedAfter = 0; S.feed = []; pullFeed(); }
  if (e.target.id === 'replyOk') syncSend();
});
let qT; let peopleTimer;
document.addEventListener('input', e => {
  if (e.target.id === 'aEmail') validateEmail(false);
  if (e.target.id === 'aPass' && S.authMode === 'register') validateRegisterPassword(false);
  if (e.target.id === 'replyText') syncSend();
  if (e.target.id === 'qBox') { clearTimeout(qT); qT = setTimeout(() => { S.q = e.target.value.trim(); render(); }, 350); }
  if (e.target.id === 'peopleSearch') {
    S.peopleSearch = e.target.value;
    clearTimeout(peopleTimer);
    peopleTimer = setTimeout(() => searchSlackPeople(S.peopleSearch), 300);
  }
});
document.addEventListener('blur', e => { if (e.target.id === 'aEmail') validateEmail(true); }, true);
document.addEventListener('submit', async e => {
  if (e.target.id === 'authForm') return submitAuth(e);
  if (e.target.id === 'askForm') {
    e.preventDefault(); const q = $('#askQ').value.trim(); if (!q) return; $('#askGo').disabled = true; $('#askGo').textContent = 'Thinking…';
    try { const d = await api('/assistant/ask', { method: 'POST', body: JSON.stringify({ question: q }) }); S.ask.unshift({ q, ...d }); S.ask = S.ask.slice(0, 8); $('#askOut').innerHTML = S.ask.map(askCard).join(''); $('#askQ').value = ''; }
    catch (err) { toast(human(err)); } finally { $('#askGo').disabled = false; $('#askGo').textContent = 'Ask'; }
  }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') { $('#replyOverlay').classList.remove('show'); $('#traceOverlay').classList.remove('show'); } });
boot();
})();
