async function load() {
  const root = document.getElementById('sessions');
  let sessions;
  try {
    const r = await fetch('/api/sessions');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    sessions = await r.json();
  } catch (e) {
    root.innerHTML = `<div class="empty">Failed to load sessions: ${escapeHtml(String(e))}</div>`;
    return;
  }
  if (!sessions.length) {
    root.innerHTML = '<div class="empty">No sessions yet. Create one with <span class="mono">converse new</span>.</div>';
    return;
  }
  sessions.sort((a, b) => (b.last_message_at ?? 0) - (a.last_message_at ?? 0));
  root.innerHTML = '';
  for (const s of sessions) {
    const card = document.createElement('a');
    card.className = 'card';
    card.href = `/session/${s.id}`;
    const members = (s.active_users ?? []).map(u =>
      `<span class="chip" style="background:${colorFor(u)}">${escapeHtml(u)}</span>`
    ).join('');
    card.innerHTML = `
      <div class="card-name">${escapeHtml(s.name || '(unnamed)')}</div>
      <div class="card-meta">
        <span class="mono">${escapeHtml(s.id)}</span>
        <span class="dot-sep">·</span>
        <span>${s.message_count ?? 0} msg${s.message_count === 1 ? '' : 's'}</span>
        <span class="dot-sep">·</span>
        <span>${formatRelative(s.last_message_at)}</span>
      </div>
      <div class="card-members">${members || '<span class="chip chip-empty">no active members</span>'}</div>`;
    root.appendChild(card);
  }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}

function colorFor(userId) {
  let h = 0;
  for (const c of userId) h = ((h * 31) + c.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 60%, 84%)`;
}

function formatRelative(ts) {
  if (!ts) return 'no activity';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

load();
