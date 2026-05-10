const sessionId = decodeURIComponent(location.pathname.split('/').pop() || '');
const ME_KEY = 'converse-gui:viewer:' + sessionId;

const messagesEl = document.getElementById('messages');
const rosterActiveEl = document.getElementById('roster-active');
const rosterOfflineEl = document.getElementById('roster-offline');
const titleEl = document.getElementById('session-title');
const idEl = document.getElementById('session-id');
const scroller = document.getElementById('scroller');
const identityEl = document.getElementById('identity');
const composeForm = document.getElementById('compose');
const composeInput = document.getElementById('compose-input');
const composeSend = document.getElementById('compose-send');
const composeError = document.getElementById('compose-error');
const rosterEl = document.getElementById('roster');
const rosterToggle = document.getElementById('roster-toggle');
const rosterBackdrop = document.getElementById('roster-backdrop');

const active = new Set();
const offline = new Set();
const seenMessageIds = new Set();
let atBottom = true;
let me = null;
let sending = false;

scroller.addEventListener('scroll', () => {
  const slack = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  atBottom = slack < 40;
});

// ---------- identity ----------

async function postJSON(path, body) {
  let r;
  try {
    r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return { ok: false, status: 0, data: { error: String(e) } };
  }
  let data = null;
  try { data = await r.json(); } catch {}
  return { ok: r.ok, status: r.status, data };
}

function loadStoredIdentity() {
  try { return JSON.parse(localStorage.getItem(ME_KEY) || 'null'); }
  catch { return null; }
}

function storeIdentity(m) {
  localStorage.setItem(ME_KEY, JSON.stringify({ user_id: m.id, session_id: m.session_id }));
}

function clearIdentity() {
  localStorage.removeItem(ME_KEY);
}

async function tryReattach() {
  const stored = loadStoredIdentity();
  if (!stored?.user_id) {
    renderJoinButton();
    return;
  }
  const r = await postJSON(
    `/api/sessions/${encodeURIComponent(sessionId)}/join`,
    { reattach: stored.user_id }
  );
  if (r.ok && r.data?.id) {
    me = r.data;
    renderJoined();
    return;
  }
  if (r.status >= 400 && r.status < 500) clearIdentity();
  renderJoinButton();
}

function renderJoinButton() {
  identityEl.innerHTML = '';
  const btn = document.createElement('button');
  btn.className = 'join-btn';
  btn.type = 'button';
  btn.textContent = 'Join chat';
  btn.addEventListener('click', () => renderJoinForm());
  identityEl.appendChild(btn);
  composeForm.classList.add('hidden');
  renderRoster();
}

function renderJoinForm(errorText) {
  identityEl.innerHTML = '';
  const form = document.createElement('form');
  form.className = 'join-form';
  form.innerHTML = `
    <input type="text" name="role" placeholder="your-role, e.g. shmuli" autocomplete="off" spellcheck="false" required>
    <button type="submit" class="primary">Join</button>
    <button type="button" class="cancel">Cancel</button>`;
  if (errorText) {
    const err = document.createElement('span');
    err.className = 'err';
    err.textContent = errorText;
    form.appendChild(err);
  }
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const role = form.role.value.trim();
    if (!role) return;
    form.role.disabled = true;
    const r = await postJSON(
      `/api/sessions/${encodeURIComponent(sessionId)}/join`,
      { name: role }
    );
    if (r.ok && r.data?.id) {
      me = r.data;
      storeIdentity(me);
      renderJoined();
    } else {
      renderJoinForm(r.data?.error || `failed (${r.status})`);
    }
  });
  form.querySelector('.cancel').addEventListener('click', renderJoinButton);
  identityEl.appendChild(form);
  setTimeout(() => form.role.focus(), 0);
}

function renderJoined() {
  identityEl.innerHTML = '';
  const pill = document.createElement('span');
  pill.className = 'user-pill';
  pill.innerHTML = `<span class="as">as</span><span class="me"></span><button type="button" class="leave-btn">leave</button>`;
  pill.querySelector('.me').textContent = me.id;
  pill.querySelector('.leave-btn').addEventListener('click', () => {
    const old = me;
    me = null;
    clearIdentity();
    renderJoinButton();
    appendSystem(`(you left as ${old.id})`);
  });
  identityEl.appendChild(pill);
  composeForm.classList.remove('hidden');
  reclassifyOwn();
  renderRoster();
  setTimeout(() => composeInput.focus(), 0);
}

function reclassifyOwn() {
  if (!me) return;
  const myId = me.id;
  for (const msg of messagesEl.querySelectorAll('.message')) {
    if (msg.classList.contains('own')) continue;
    const author = msg.querySelector('.author');
    if (author && author.textContent === myId) {
      msg.classList.add('own');
      const head = msg.querySelector('.message-head');
      if (head && !head.querySelector('.you-tag')) {
        const tag = document.createElement('span');
        tag.className = 'you-tag';
        tag.textContent = '(you)';
        head.insertBefore(tag, head.firstChild);
      }
    }
  }
}

// ---------- compose ----------

function autoresize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

composeInput.addEventListener('input', () => autoresize(composeInput));
composeInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    composeForm.requestSubmit();
  }
});

composeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (sending || !me) return;
  const text = composeInput.value;
  if (!text.trim()) return;
  sending = true;
  composeSend.disabled = true;
  composeError.textContent = '';
  const r = await postJSON(
    `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
    { user: me.id, text }
  );
  if (r.ok) {
    composeInput.value = '';
    autoresize(composeInput);
  } else {
    composeError.textContent = r.data?.error || `send failed (${r.status})`;
    setTimeout(() => { composeError.textContent = ''; }, 4000);
  }
  sending = false;
  composeSend.disabled = false;
  composeInput.focus();
});

// ---------- header ----------

async function loadHeader() {
  try {
    const r = await fetch('/api/sessions');
    const list = await r.json();
    const s = list.find(x => x.id === sessionId);
    if (s) {
      titleEl.textContent = s.name || '(unnamed)';
      idEl.textContent = s.id;
      composeInput.placeholder = `Message "${s.name || sessionId}"…`;
    } else {
      titleEl.textContent = '(unknown session)';
      idEl.textContent = sessionId;
    }
  } catch {
    titleEl.textContent = '(unknown session)';
    idEl.textContent = sessionId;
  }
}

// ---------- SSE ----------

function startStream() {
  const es = new EventSource(`/api/sessions/${encodeURIComponent(sessionId)}/stream`);
  const dispatch = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    handleEvent(m);
  };
  // Server emits all events on the default channel; the named-event listeners
  // are a safety net for any future contract that sets `event: <type>`.
  for (const ev of ['message', 'join', 'leave', 'roster']) {
    es.addEventListener(ev, dispatch);
  }
  es.onerror = () => {
    appendSystem('— connection interrupted; browser will reconnect —');
  };
}

function handleEvent(m) {
  switch (m.type) {
    case 'roster':
      active.clear();
      offline.clear();
      for (const u of (m.active || [])) active.add(u);
      renderRoster();
      break;
    case 'join': {
      offline.delete(m.user_id);
      const wasActive = active.has(m.user_id);
      active.add(m.user_id);
      if (!wasActive) appendSystem(`${m.user_id} joined`);
      renderRoster();
      break;
    }
    case 'leave':
      active.delete(m.user_id);
      offline.add(m.user_id);
      appendSystem(`${m.user_id} left`);
      renderRoster();
      break;
    case 'message':
      appendMessage(m);
      break;
  }
}

// ---------- rendering ----------

function appendMessage(m) {
  if (m.id != null) {
    if (seenMessageIds.has(m.id)) return;
    seenMessageIds.add(m.id);
  }
  const isOwn = !!(me && m.user_id === me.id);
  const el = document.createElement('div');
  el.className = 'message' + (isOwn ? ' own' : '');
  const youTag = isOwn ? '<span class="you-tag">(you)</span>' : '';
  el.innerHTML = `
    <div class="message-head">
      ${youTag}
      <span class="author" style="color:${colorFor(m.user_id, 'fg')}">${escapeHtml(m.user_id)}</span>
      <span class="time">${formatTime(m.created_at)}</span>
    </div>
    <div class="message-body" style="background:${colorFor(m.user_id, 'bg')}">${renderBody(m.text || '')}</div>`;
  messagesEl.appendChild(el);
  scrollIfPinned();
}

function appendSystem(text) {
  const el = document.createElement('div');
  el.className = 'system';
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollIfPinned();
}

function renderBody(text) {
  return escapeHtml(text)
    .replace(/\n/g, '<br>')
    .replace(/@([A-Za-z0-9_-]+)/g, '<span class="mention">@$1</span>');
}

function renderRoster() {
  // The daemon flags a user "active" only while they have a tail
  // subscriber. The human joins via /join (no tail of their own — the
  // gui-viewer tails on everyone's behalf), so without help they'd render
  // themselves in Offline. Locally promote `me` into the active set for
  // this browser's view only.
  const localActive = new Set(active);
  const localOffline = new Set(offline);
  if (me) {
    localActive.add(me.id);
    localOffline.delete(me.id);
  }
  const sorted = (set) => [...set].sort();
  const renderActive = (u) => {
    const isMe = !!(me && u === me.id);
    const tag = isMe ? '<span class="you-tag">you</span>' : '';
    return `<div class="member"><span class="dot" style="background:${colorFor(u, 'bg')}"></span><span class="uid">${escapeHtml(u)}</span>${tag}</div>`;
  };
  const renderOffline = (u) => `<div class="member offline">${escapeHtml(u)}</div>`;
  rosterActiveEl.innerHTML =
    sorted(localActive).map(renderActive).join('') ||
    '<div class="member empty">(none)</div>';
  rosterOfflineEl.innerHTML =
    sorted(localOffline).map(renderOffline).join('') ||
    '<div class="member empty">(none)</div>';
}

// ---------- helpers ----------

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}

function colorFor(userId, kind = 'bg') {
  let h = 0;
  for (const c of userId) h = ((h * 31) + c.charCodeAt(0)) >>> 0;
  const hue = h % 360;
  if (kind === 'fg') return `hsl(${hue}, 55%, 38%)`;
  return `hsl(${hue}, 60%, 90%)`;
}

function formatTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function scrollIfPinned() {
  if (atBottom) scroller.scrollTop = scroller.scrollHeight;
}

// ---------- mobile roster drawer ----------

function setRosterOpen(open) {
  rosterEl.classList.toggle('open', open);
  rosterBackdrop.classList.toggle('open', open);
}
rosterToggle?.addEventListener('click', () => {
  setRosterOpen(!rosterEl.classList.contains('open'));
});
rosterBackdrop?.addEventListener('click', () => setRosterOpen(false));
// Close the drawer if the viewport grows back to desktop width.
window.addEventListener('resize', () => {
  if (window.innerWidth > 600) setRosterOpen(false);
});

// ---------- init ----------

loadHeader();
tryReattach();
startStream();
