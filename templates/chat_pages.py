"""
HTML templates for the chat web UI.

Kept as Jinja strings to match the existing inline-HTML pattern used by
`/slack/oauth_redirect`. Pages:
  - chat_page: the actual chat view (creator + brand)
  - admin_login_page: simple admin-token gate
  - admin_dashboard: list of chat spaces
  - admin_chat_page: read-only admin view of one chat
  - error_page: shared error template
"""

CHAT_PAGE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ chat_title }} — INFLUENCE</title>
  <style>
    :root { color-scheme: light; }
    html, body { background:#f4f5f7; color:#1d1d1f; height:100%; margin:0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display:flex; flex-direction:column; }
    header.bar { padding:14px 20px; background:#fff; border-bottom:1px solid #e5e5ea; }
    header.bar h1 { font-size:16px; margin:0; }
    header.bar .meta { font-size:13px; color:#6b7280; margin-top:2px; }
    .banner { background:#fff7ed; color:#92400e; padding:8px 20px; font-size:13px; border-bottom:1px solid #fde7c2; }
    main { flex:1; overflow-y:auto; padding:16px; max-width:780px; width:100%; margin:0 auto; box-sizing:border-box; }
    .msg { display:flex; margin:8px 0; }
    .msg.me { justify-content:flex-end; }
    .bubble { max-width:70%; padding:10px 14px; border-radius:14px; background:#fff; border:1px solid #e5e5ea; word-wrap:break-word; }
    .msg.me .bubble { background:#dbeafe; border-color:#bfdbfe; }
    .msg .who { font-size:11px; color:#6b7280; margin-bottom:2px; }
    .msg .body { white-space:pre-wrap; font-size:14px; line-height:1.45; }
    .msg .ts { font-size:11px; color:#9ca3af; margin-top:4px; }
    .msg .attachments img { max-width:220px; max-height:220px; border-radius:8px; margin-top:6px; display:block; }
    .reactions { margin-top:4px; display:flex; gap:4px; flex-wrap:wrap; }
    .reactions .react { background:#f3f4f6; border:1px solid #e5e7eb; border-radius:12px; padding:2px 8px; font-size:12px; cursor:pointer; }
    .reactions .add { background:transparent; border:1px dashed #d1d5db; color:#6b7280; }
    footer.compose { background:#fff; border-top:1px solid #e5e5ea; padding:10px; }
    .compose-inner { max-width:780px; margin:0 auto; display:flex; gap:8px; align-items:center; }
    .compose textarea { flex:1; resize:none; padding:10px 12px; border-radius:10px; border:1px solid #d1d5db; font-family:inherit; font-size:14px; min-height:38px; max-height:120px; }
    .compose button { background:#111827; color:#fff; border:0; padding:10px 16px; border-radius:10px; cursor:pointer; font-size:14px; }
    .compose button:disabled { opacity:.5; cursor:not-allowed; }
    .compose .iconbtn { background:#f3f4f6; color:#111827; padding:8px 10px; }
    .emoji-pop { position:absolute; background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:6px; box-shadow:0 6px 24px rgba(0,0,0,.08); display:none; }
    .emoji-pop button { background:transparent; border:0; font-size:18px; cursor:pointer; padding:4px; }
    .archived { background:#fef2f2; color:#991b1b; padding:10px 20px; font-size:13px; }
    .unread-pill { display:inline-block; background:#ef4444; color:#fff; border-radius:10px; padding:1px 8px; font-size:11px; margin-left:6px; }
  </style>
</head>
<body data-space-id="{{ space.id }}" data-self-party="{{ self_party }}" data-archived="{{ 'true' if space.status == 'archived' else 'false' }}">
  <header class="bar">
    <h1>{{ chat_title }}</h1>
    <div class="meta">Campaign: {{ space.campaign_name or '—' }} · Brand: {{ space.brand_name or '—' }}</div>
  </header>
  <div class="banner">Influence staff may review this conversation for quality and compliance.</div>
  {% if space.status == 'archived' %}
  <div class="archived">This campaign has ended — chat is archived and read-only.</div>
  {% endif %}
  <main id="messages"></main>
  <footer class="compose">
    <form class="compose-inner" id="composeForm" autocomplete="off">
      <input type="file" id="fileInput" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none">
      <button type="button" class="iconbtn" id="fileBtn" title="Attach image">📎</button>
      <button type="button" class="iconbtn" id="emojiBtn" title="Emoji">😀</button>
      <textarea id="bodyInput" placeholder="Type a message…" {% if space.status == 'archived' %}disabled{% endif %}></textarea>
      <button type="submit" id="sendBtn" {% if space.status == 'archived' %}disabled{% endif %}>Send</button>
    </form>
    <div class="emoji-pop" id="emojiPop">
      <button>👍</button><button>❤️</button><button>🎉</button><button>🔥</button><button>😂</button><button>👀</button><button>🙏</button><button>✅</button>
    </div>
  </footer>

<script>
(function() {
  const bodyEl = document.body;
  const spaceId = bodyEl.dataset.spaceId;
  const selfParty = bodyEl.dataset.selfParty;
  const archived = bodyEl.dataset.archived === 'true';
  const messagesEl = document.getElementById('messages');
  const composeForm = document.getElementById('composeForm');
  const bodyInput = document.getElementById('bodyInput');
  const sendBtn = document.getElementById('sendBtn');
  const fileBtn = document.getElementById('fileBtn');
  const fileInput = document.getElementById('fileInput');
  const emojiBtn = document.getElementById('emojiBtn');
  const emojiPop = document.getElementById('emojiPop');

  let lastId = 0;
  let polling = false;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderMessage(m) {
    const mine = m.party === selfParty;
    const div = document.createElement('div');
    div.className = 'msg ' + (mine ? 'me' : 'them');
    div.dataset.id = m.id;
    let attHtml = '';
    if (m.attachments && m.attachments.length) {
      attHtml = '<div class="attachments">' + m.attachments.map(a => {
        return '<img src="/chat/attachment/' + a.id + '" alt="' + escapeHtml(a.filename) + '">';
      }).join('') + '</div>';
    }
    let reactHtml = '';
    const reactions = m.reactions || {};
    const keys = Object.keys(reactions);
    if (keys.length || !archived) {
      reactHtml = '<div class="reactions">';
      for (const k of keys) {
        reactHtml += '<button class="react" data-emoji="' + escapeHtml(k) + '" data-msg="' + m.id + '">' + escapeHtml(k) + ' ' + reactions[k] + '</button>';
      }
      if (!archived) reactHtml += '<button class="react add" data-msg="' + m.id + '">+</button>';
      reactHtml += '</div>';
    }
    const ts = m.created_at ? new Date(m.created_at).toLocaleString() : '';
    div.innerHTML =
      '<div class="bubble">' +
      '<div class="who">' + escapeHtml(m.sender || m.party) + '</div>' +
      '<div class="body">' + escapeHtml(m.body) + '</div>' +
      attHtml + reactHtml +
      '<div class="ts">' + escapeHtml(ts) + '</div>' +
      '</div>';
    messagesEl.appendChild(div);
    if (m.id > lastId) lastId = m.id;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const r = await fetch('/chat/' + spaceId + '/messages?since=' + lastId, { credentials: 'same-origin' });
      if (!r.ok) return;
      const data = await r.json();
      if (data.messages && data.messages.length) {
        for (const m of data.messages) renderMessage(m);
        scrollToBottom();
        if (lastId > 0) {
          fetch('/chat/' + spaceId + '/read', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ up_to: lastId }),
          });
        }
      }
    } catch (e) { /* swallow */ }
    polling = false;
  }

  async function sendMessage(body, file) {
    const form = new FormData();
    if (body) form.append('body', body);
    if (file) form.append('attachment', file);
    if (!body && !file) return;
    sendBtn.disabled = true;
    try {
      const r = await fetch('/chat/' + spaceId + '/messages', {
        method: 'POST', credentials: 'same-origin', body: form,
      });
      if (r.ok) { bodyInput.value = ''; await poll(); }
    } finally { sendBtn.disabled = archived; }
  }

  composeForm.addEventListener('submit', e => {
    e.preventDefault();
    sendMessage(bodyInput.value.trim(), null);
  });
  bodyInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); composeForm.requestSubmit(); }
  });
  fileBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files && fileInput.files[0]) {
      sendMessage(bodyInput.value.trim(), fileInput.files[0]);
      fileInput.value = '';
    }
  });

  let emojiTargetMsg = null;
  emojiBtn.addEventListener('click', () => {
    emojiTargetMsg = null;
    const rect = emojiBtn.getBoundingClientRect();
    emojiPop.style.left = rect.left + 'px';
    emojiPop.style.top = (rect.top - 50) + 'px';
    emojiPop.style.display = emojiPop.style.display === 'block' ? 'none' : 'block';
  });
  emojiPop.addEventListener('click', e => {
    if (e.target.tagName !== 'BUTTON') return;
    const emoji = e.target.textContent;
    emojiPop.style.display = 'none';
    if (emojiTargetMsg) {
      fetch('/chat/' + spaceId + '/messages/' + emojiTargetMsg + '/react', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ emoji }),
      }).then(poll);
    } else {
      bodyInput.value += emoji;
      bodyInput.focus();
    }
  });

  messagesEl.addEventListener('click', e => {
    const btn = e.target.closest('.react');
    if (!btn) return;
    const msgId = btn.dataset.msg;
    if (btn.classList.contains('add')) {
      emojiTargetMsg = msgId;
      const rect = btn.getBoundingClientRect();
      emojiPop.style.left = rect.left + 'px';
      emojiPop.style.top = (rect.top - 50) + 'px';
      emojiPop.style.display = 'block';
      return;
    }
    fetch('/chat/' + spaceId + '/messages/' + msgId + '/react', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emoji: btn.dataset.emoji }),
    }).then(poll);
  });

  poll().then(scrollToBottom);
  setInterval(poll, 3000);
})();
</script>
</body>
</html>
"""


ADMIN_LOGIN_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Admin login — INFLUENCE</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:380px; margin:14vh auto; padding:24px; background:#f4f5f7; }
.card { background:#fff; padding:24px; border-radius:12px; border:1px solid #e5e5ea; }
input { width:100%; padding:10px; border:1px solid #d1d5db; border-radius:8px; font-size:14px; box-sizing:border-box; }
button { width:100%; padding:10px; background:#111827; color:#fff; border:0; border-radius:8px; margin-top:10px; cursor:pointer; }
.err { color:#991b1b; font-size:13px; margin-top:8px; }
</style></head><body><div class="card">
<h2 style="margin:0 0 12px">INFLUENCE Chat Admin</h2>
<form method="POST" action="/admin/chats/login">
  <input type="password" name="token" placeholder="Admin token" required>
  <button type="submit">Enter</button>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</form></div></body></html>
"""


ADMIN_DASHBOARD = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Chat spaces — INFLUENCE Admin</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f4f5f7; margin:0; }
.container { max-width:1100px; margin:0 auto; padding:24px; }
h1 { font-size:20px; margin:0 0 16px; }
.stats { display:flex; gap:12px; margin-bottom:16px; }
.stat { flex:1; background:#fff; border:1px solid #e5e5ea; padding:14px 16px; border-radius:10px; }
.stat .v { font-size:22px; font-weight:600; }
.stat .l { font-size:12px; color:#6b7280; text-transform:uppercase; letter-spacing:.05em; }
form.search { display:flex; gap:8px; margin-bottom:12px; }
form.search input, form.search select { padding:8px 10px; border:1px solid #d1d5db; border-radius:8px; }
table { width:100%; background:#fff; border:1px solid #e5e5ea; border-radius:10px; border-collapse:separate; border-spacing:0; overflow:hidden; }
th, td { text-align:left; padding:10px 12px; font-size:13px; border-bottom:1px solid #f1f1f3; }
th { background:#f9fafb; font-weight:500; color:#6b7280; }
tr:last-child td { border-bottom:0; }
a.row { color:#1d4ed8; text-decoration:none; }
.tag { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; }
.tag.active { background:#dcfce7; color:#166534; }
.tag.archived { background:#fee2e2; color:#991b1b; }
</style></head><body>
<div class="container">
<h1>Chat spaces</h1>
<div class="stats">
  <div class="stat"><div class="v">{{ stats.active }}</div><div class="l">Active</div></div>
  <div class="stat"><div class="v">{{ stats.archived }}</div><div class="l">Archived</div></div>
  <div class="stat"><div class="v">{{ stats.active_creators }}</div><div class="l">Active creators</div></div>
</div>
<form class="search" method="GET">
  <input type="text" name="q" value="{{ query.q or '' }}" placeholder="Search creator / campaign / brand…">
  <select name="status">
    <option value="">All statuses</option>
    <option value="active" {% if query.status == 'active' %}selected{% endif %}>Active</option>
    <option value="archived" {% if query.status == 'archived' %}selected{% endif %}>Archived</option>
  </select>
  <button type="submit">Filter</button>
</form>
<table>
<thead><tr><th>Creator</th><th>Campaign</th><th>Brand</th><th>Status</th><th>Last message</th><th></th></tr></thead>
<tbody>
{% for s in spaces %}
<tr>
  <td>@{{ s.creator_username }}{% if s.creator_email %}<br><span style="color:#6b7280;font-size:11px">{{ s.creator_email }}</span>{% endif %}</td>
  <td>{{ s.campaign_name or '—' }}</td>
  <td>{{ s.brand_name or '—' }}</td>
  <td><span class="tag {{ s.status }}">{{ s.status }}</span></td>
  <td>{{ s.last_message_at.strftime('%Y-%m-%d %H:%M') if s.last_message_at else '—' }}</td>
  <td><a class="row" href="/admin/chats/{{ s.id }}">Open →</a></td>
</tr>
{% endfor %}
{% if not spaces %}<tr><td colspan="6" style="text-align:center;color:#6b7280;padding:24px">No chat spaces.</td></tr>{% endif %}
</tbody>
</table>
</div></body></html>
"""


ADMIN_CHAT_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ space.campaign_name or 'Chat' }} — Admin</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f4f5f7; margin:0; }
.container { max-width:820px; margin:0 auto; padding:24px; }
.crumbs { font-size:13px; color:#6b7280; margin-bottom:8px; }
.crumbs a { color:#1d4ed8; text-decoration:none; }
h1 { font-size:18px; margin:0 0 4px; }
.meta { font-size:13px; color:#6b7280; margin-bottom:12px; }
.archived-note { background:#fef2f2; color:#991b1b; padding:8px 12px; border-radius:8px; font-size:13px; margin-bottom:12px; }
.msg { background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:10px 14px; margin:8px 0; }
.msg .who { font-size:11px; color:#6b7280; margin-bottom:2px; }
.msg .body { white-space:pre-wrap; font-size:14px; }
.msg .ts { font-size:11px; color:#9ca3af; margin-top:4px; }
.msg img { max-width:240px; border-radius:8px; margin-top:6px; display:block; }
.reactions { margin-top:6px; font-size:12px; color:#6b7280; }
</style></head><body>
<div class="container">
<div class="crumbs"><a href="/admin/chats">← All chat spaces</a></div>
<h1>{{ space.campaign_name or '—' }} · {{ space.brand_name or '—' }}</h1>
<div class="meta">Creator @{{ space.creator_username }}{% if space.creator_email %} ({{ space.creator_email }}){% endif %} · status: {{ space.status }} · created {{ space.created_at.strftime('%Y-%m-%d %H:%M') if space.created_at else '—' }}</div>
{% if space.status == 'archived' %}<div class="archived-note">This chat is archived.</div>{% endif %}
{% for m in messages %}
<div class="msg">
  <div class="who">{{ m.sender }} · {{ m.party }}</div>
  <div class="body">{{ m.body }}</div>
  {% for a in m.attachments %}<img src="/chat/attachment/{{ a.id }}?admin=1" alt="{{ a.filename }}">{% endfor %}
  {% if m.reactions %}<div class="reactions">{% for emoji, count in m.reactions.items() %}{{ emoji }} {{ count }} &nbsp;{% endfor %}</div>{% endif %}
  <div class="ts">{{ m.created_at }}</div>
</div>
{% endfor %}
{% if not messages %}<div style="text-align:center;color:#6b7280;padding:24px">No messages yet.</div>{% endif %}
</div></body></html>
"""


ERROR_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ heading }} — INFLUENCE</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:520px; margin:14vh auto; padding:0 24px; color:#1d1d1f; }
h1 { font-size:22px; margin-bottom:8px; }
p { color:#4b5563; line-height:1.5; }
</style></head><body>
<h1>{{ heading }}</h1>
<p>{{ message }}</p>
</body></html>
"""
