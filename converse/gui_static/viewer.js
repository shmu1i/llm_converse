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

// ---------- @-mention autocomplete ----------
// Behaves like WhatsApp/Telegram: typing `@` at a word boundary opens a
// list of every session member (active + offline), filterable as you
// type. Arrow keys navigate, Enter/Tab/Click insert, Esc closes. The
// popup re-renders whenever roster/join/leave events change the room.

const mentionPopup = document.createElement('div');
mentionPopup.className = 'mention-popup hidden';
mentionPopup.setAttribute('role', 'listbox');
composeForm.appendChild(mentionPopup);

let mentionState = null;  // { start, end, query } while popup is open

function mentionMembers() {
  const all = new Set([...active, ...offline]);
  return [...all].sort((a, b) => {
    const aA = active.has(a), bA = active.has(b);
    if (aA !== bA) return aA ? -1 : 1;     // active first
    return a.localeCompare(b);
  });
}

function detectMention() {
  const value = composeInput.value;
  const pos = composeInput.selectionStart;
  if (pos == null) return null;
  // Walk backward from cursor over valid mention chars to find the `@`.
  let i = pos - 1;
  while (i >= 0 && /[A-Za-z0-9_-]/.test(value[i])) i--;
  if (i < 0 || value[i] !== '@') return null;
  // The char before `@` must be whitespace (or `@` is at start), else
  // this is something like `foo@bar` and not a mention trigger.
  if (i > 0 && !/\s/.test(value[i - 1])) return null;
  return { start: i, end: pos, query: value.slice(i + 1, pos) };
}

function openMentionPopup(state) {
  // Preserve the user's current selection across re-renders (filter
  // refinement, roster updates) so arrow-key navigation doesn't bounce
  // back to the first item.
  const prevUid = mentionPopup.querySelector('.mention-item.selected')?.dataset.uid;
  mentionState = state;
  const q = state.query.toLowerCase();
  let list = mentionMembers();
  if (q) list = list.filter(u => u.toLowerCase().startsWith(q));
  if (!list.length) {
    mentionPopup.innerHTML = '<div class="mention-empty">no member matches</div>';
  } else {
    let selIdx = prevUid ? list.indexOf(prevUid) : -1;
    if (selIdx < 0) selIdx = 0;
    mentionPopup.innerHTML = list.map((u, idx) => {
      const isActive = active.has(u);
      const isMe = !!(me && u === me.id);
      const statusLabel = isMe ? 'you' : (isActive ? 'active' : 'offline');
      return `<div class="mention-item${idx === selIdx ? ' selected' : ''}" data-uid="${escapeHtml(u)}" role="option">
        <span class="presence-dot ${isActive ? 'online' : 'offline'}"></span>
        <span class="uid">${escapeHtml(u)}</span>
        <span class="status">${statusLabel}</span>
      </div>`;
    }).join('');
  }
  mentionPopup.classList.remove('hidden');
}

function closeMentionPopup() {
  if (!mentionState) return;
  mentionState = null;
  mentionPopup.classList.add('hidden');
  mentionPopup.innerHTML = '';
}

function refreshMentionPopupIfOpen() {
  if (!mentionState) return;
  // Re-detect from the current cursor (the underlying text hasn't moved).
  const state = detectMention();
  if (state) openMentionPopup(state);
  else closeMentionPopup();
}

function moveMentionSelection(delta) {
  const items = [...mentionPopup.querySelectorAll('.mention-item')];
  if (!items.length) return;
  let idx = items.findIndex(el => el.classList.contains('selected'));
  if (idx === -1) idx = 0;
  idx = (idx + delta + items.length) % items.length;
  items.forEach((el, i) => el.classList.toggle('selected', i === idx));
  items[idx].scrollIntoView({ block: 'nearest' });
}

function applyMentionItem(el) {
  if (!mentionState || !el) return;
  const uid = el.dataset.uid;
  if (!uid) return;
  const value = composeInput.value;
  const before = value.slice(0, mentionState.start);
  const after = value.slice(mentionState.end);
  const insertion = '@' + uid + ' ';
  composeInput.value = before + insertion + after;
  const pos = before.length + insertion.length;
  composeInput.setSelectionRange(pos, pos);
  autoresize(composeInput);
  closeMentionPopup();
  composeInput.focus();
}

composeInput.addEventListener('input', () => {
  const state = detectMention();
  if (state) openMentionPopup(state);
  else closeMentionPopup();
});

// Cursor-only moves (Left/Right/Home/End, mouse clicks inside the
// textarea) don't fire 'input', but they can move the cursor out of a
// live mention region — re-check after they fire. ArrowUp/ArrowDown are
// deliberately excluded: when the popup is open they navigate the popup
// (handled in the keydown capture below), and re-running detectMention
// here would just bounce the selection back to the first item.
composeInput.addEventListener('keyup', (e) => {
  if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) {
    if (!mentionState) {
      const state = detectMention();
      if (state) openMentionPopup(state);
    } else {
      refreshMentionPopupIfOpen();
    }
  }
});
composeInput.addEventListener('click', () => {
  const state = detectMention();
  if (state) openMentionPopup(state);
  else closeMentionPopup();
});

// Capture on the form so we run before the input's own bubble-phase
// submit-on-Enter handler.
composeForm.addEventListener('keydown', (e) => {
  if (e.target !== composeInput || !mentionState) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); moveMentionSelection(1); return; }
  if (e.key === 'ArrowUp')   { e.preventDefault(); e.stopPropagation(); moveMentionSelection(-1); return; }
  if (e.key === 'Escape')    { e.preventDefault(); e.stopPropagation(); closeMentionPopup(); return; }
  if (e.key === 'Enter' || e.key === 'Tab') {
    const sel = mentionPopup.querySelector('.mention-item.selected');
    if (sel) {
      e.preventDefault();
      e.stopPropagation();
      applyMentionItem(sel);
    } else {
      // No matches — close and let Enter fall through to form submit.
      closeMentionPopup();
      if (e.key === 'Tab') e.preventDefault();
    }
  }
}, true);

// mousedown (not click) so the action runs before the textarea blur.
mentionPopup.addEventListener('mousedown', (e) => {
  const item = e.target.closest('.mention-item');
  if (item) {
    e.preventDefault();
    applyMentionItem(item);
  }
});
mentionPopup.addEventListener('mouseover', (e) => {
  const item = e.target.closest('.mention-item');
  if (!item) return;
  for (const el of mentionPopup.querySelectorAll('.mention-item')) {
    el.classList.toggle('selected', el === item);
  }
});

composeInput.addEventListener('blur', () => {
  // Delay so a popup click can complete first.
  setTimeout(() => {
    if (document.activeElement !== composeInput) closeMentionPopup();
  }, 150);
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
      for (const u of (m.offline || [])) offline.add(u);
      renderRoster();
      refreshMentionPopupIfOpen();
      break;
    case 'join': {
      offline.delete(m.user_id);
      const wasActive = active.has(m.user_id);
      active.add(m.user_id);
      if (!wasActive) appendSystem(`${m.user_id} joined`);
      renderRoster();
      refreshMentionPopupIfOpen();
      break;
    }
    case 'leave':
      active.delete(m.user_id);
      offline.add(m.user_id);
      appendSystem(`${m.user_id} left`);
      renderRoster();
      refreshMentionPopupIfOpen();
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
