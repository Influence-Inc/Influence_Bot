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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{{ chat_title }} — INFLUENCE</title>
  <style>
    :root{
      color-scheme: light;
      --recv-bg:#E9E9EB; --recv-fg:#000;
      --sent-bg:#1C1C1E; --sent-fg:#fff;
      --muted:#8E8E93; --line:#D1D1D6; --line-2:#C6C6C8;
      --brand-av:#1C1C1E;
    }
    html,body{margin:0;padding:0;background:#fff;color:#000;}
    body{
      font-family:-apple-system,BlinkMacSystemFont,ui-sans-serif,"Segoe UI",Roboto,Helvetica,Arial,sans-serif,"Apple Color Emoji","Segoe UI Emoji","Segoe UI Symbol";
      -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
      min-height:100vh;min-height:100dvh;
    }
    *{box-sizing:border-box}
    button{font-family:inherit;border:0;background:none;padding:0;cursor:pointer;color:inherit}
    ::-webkit-scrollbar{width:8px}
    ::-webkit-scrollbar-thumb{background:var(--line);border-radius:20px}

    .wrap{min-height:100vh;min-height:100dvh;background:#fff;display:flex;flex-direction:column}

    /* ── HEADER ── */
    .hdr{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.85);
      backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);
      border-bottom:.5px solid var(--line)}
    .hdr-inner{max-width:820px;margin:0 auto;padding:11px 20px 13px;display:flex;flex-direction:column;align-items:center;gap:4px;width:100%}
    .stack{display:flex;align-items:center}
    .av{border-radius:99px;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;line-height:1;flex-shrink:0}
    .av.lg{width:44px;height:44px;font-size:15px;box-shadow:0 0 0 2.5px #fff}
    .av.lg + .av.lg{margin-left:-14px}
    .av-creator{background:linear-gradient(135deg,#5AC8FA,#007AFF)}
    .av-brand{background:var(--brand-av);font-weight:700}
    .av-admin{background:linear-gradient(135deg,#FFCC00,#FF9500)}
    .header-title{font-size:15px;font-weight:600;color:#000;letter-spacing:-.01em;margin-top:5px;text-align:center;word-break:break-word}
    .header-sub{font-size:12px;color:var(--muted);letter-spacing:-.005em;text-align:center}

    /* ── BANNER ── */
    .banner{max-width:820px;margin:0 auto;width:100%;padding:9px 20px;font-size:12.5px;text-align:center;letter-spacing:-.005em}
    .banner.archived{background:#FEF2F2;color:#991B1B}
    .banner.approved{background:#ECFDF5;color:#065F46}

    /* ── FEED ── */
    .feed{flex:1;padding:14px 20px 22px;max-width:820px;margin:0 auto;width:100%;display:flex;flex-direction:column}
    #messages{display:flex;flex-direction:column;width:100%}
    .day-sep{text-align:center;font-size:11px;color:var(--muted);font-weight:600;letter-spacing:-.005em;margin:14px 0 12px}
    .day-sep .t{font-weight:400}
    .empty{text-align:center;color:var(--muted);font-size:13px;padding:32px 0}

    .row{display:flex;margin-top:2px}
    .row.recv{gap:8px;align-items:flex-end;align-self:flex-start;max-width:88%}
    .row.recv.fresh{margin-top:16px}
    .row.sent{flex-direction:column;align-items:flex-end;align-self:flex-end;max-width:82%}
    .row.sent.fresh{margin-top:16px}
    .row.recv .av{width:30px;height:30px;font-size:12px}
    .row.recv.grouped .av{visibility:hidden}
    .cluster{display:flex;flex-direction:column;gap:3px;min-width:0}
    .label{font-size:11px;color:var(--muted);padding-left:14px;margin-bottom:2px}

    .bubble{position:relative;padding:9px 16px;border-radius:20px;font-size:16px;line-height:1.3;letter-spacing:-.01em;
      white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;max-width:480px;width:fit-content}
    .bubble.recv{background:var(--recv-bg);color:var(--recv-fg)}
    .bubble.sent{background:var(--sent-bg);color:var(--sent-fg)}
    .row.sent .bubble{align-self:flex-end}

    .att-wrap{position:relative;width:fit-content;margin-top:1px}
    .att-wrap.sent{align-self:flex-end}
    .att{display:block;width:240px;max-width:70vw;border-radius:18px;box-shadow:inset 0 0 0 .5px rgba(0,0,0,.08);cursor:zoom-in}

    /* hover react affordance */
    .react-btn{position:absolute;top:50%;transform:translateY(-50%) scale(.9);opacity:0;pointer-events:none;
      transition:opacity .12s ease,transform .12s ease;width:28px;height:28px;border-radius:99px;background:#fff;
      border:.5px solid var(--line);box-shadow:0 2px 6px rgba(0,0,0,.08);display:flex;align-items:center;justify-content:center;color:#3C3C43}
    .bubble.recv .react-btn,.att-wrap:not(.sent) .react-btn{right:-38px}
    .bubble.sent .react-btn,.att-wrap.sent .react-btn{left:-38px}
    .bubble:hover .react-btn,.att-wrap:hover .react-btn,.react-btn:hover{opacity:1;pointer-events:auto;transform:translateY(-50%) scale(1)}
    .react-btn:hover{background:#F2F2F7}
    /* Transparent hover "bridge" spanning the gap between a bubble and its
       floating react button, so moving the cursor to it never drops :hover
       (which would hide the button before you could click it). */
    .react-btn::before{content:'';position:absolute;top:0;bottom:0}
    .bubble.recv .react-btn::before,.att-wrap:not(.sent) .react-btn::before{left:-14px;width:16px}
    .bubble.sent .react-btn::before,.att-wrap.sent .react-btn::before{right:-14px;width:16px}
    /* Touch devices have no hover: long-press a bubble to react. Suppress the
       native selection / callout so it doesn't fight the long-press gesture. */
    @media (hover:none){
      .bubble,.att-wrap{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
    }

    /* tapback pill */
    .react-pill{position:absolute;top:-10px;background:#fff;border:.5px solid var(--line);border-radius:99px;
      padding:3px 8px;font-size:13px;box-shadow:0 2px 6px rgba(0,0,0,.08);display:flex;align-items:center;gap:3px;line-height:1;z-index:2}
    .bubble.recv .react-pill,.att-wrap:not(.sent) .react-pill{right:-6px}
    .bubble.sent .react-pill,.att-wrap.sent .react-pill{left:-6px}
    .react-pill .rx{display:inline-flex;align-items:center;gap:2px}
    .react-pill .rx b{font-size:11px;color:#3C3C43;font-weight:500}

    .status{font-size:10px;color:var(--muted);margin-top:5px;padding-right:6px;letter-spacing:.01em;align-self:flex-end}

    /* typing */
    .composer-typing{display:none;align-items:center;gap:5px;padding:0 2px;flex-shrink:0}
    .composer-typing.on{display:inline-flex}
    .composer-typing .dot{width:7px;height:7px;background:var(--muted);border-radius:99px;animation:bob 1.2s infinite}
    .composer-typing .dot:nth-child(2){animation-delay:.15s}
    .composer-typing .dot:nth-child(3){animation-delay:.3s}
    .composer-input.typing .editable[data-empty="true"]:before{content:''}
    @keyframes bob{0%,60%,100%{transform:translateY(0);opacity:.35}30%{transform:translateY(-3px);opacity:1}}

    /* ── COMPOSER ── */
    .composer{position:sticky;bottom:0;background:rgba(255,255,255,.85);
      backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-top:.5px solid var(--line)}
    .composer-row{max-width:820px;margin:0 auto;padding:10px 16px 6px;display:flex;align-items:center;gap:10px}
    .attach-btn{width:38px;height:38px;border-radius:99px;background:var(--recv-bg);color:var(--sent-bg);
      display:flex;align-items:center;justify-content:center;flex-shrink:0}
    .attach-btn:disabled{opacity:.4;cursor:not-allowed}
    .composer-input{flex:1;background:#fff;border:.5px solid var(--line-2);border-radius:22px;
      padding:6px 6px 6px 16px;display:flex;align-items:flex-end;gap:6px;min-height:38px}
    .editable{flex:1;font-size:16px;line-height:1.35;color:#000;outline:none;letter-spacing:-.01em;
      max-height:120px;overflow-y:auto;padding:5px 0;word-break:break-word}
    .editable[data-empty="true"]:before{content:attr(data-placeholder);color:var(--muted);pointer-events:none}
    .send-btn{width:28px;height:28px;border-radius:99px;background:var(--sent-bg);color:#fff;
      display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-bottom:2px}
    .send-btn:disabled{background:#C7C7CC;cursor:not-allowed}
    .notify-hint{max-width:820px;margin:0 auto;padding:2px 20px calc(10px + env(safe-area-inset-bottom));
      font-size:11px;color:var(--muted);text-align:center;letter-spacing:-.005em}

    .emoji-pop{position:fixed;background:#fff;border:.5px solid var(--line);border-radius:14px;padding:6px;
      box-shadow:0 8px 28px rgba(0,0,0,.16);display:none;z-index:50;max-width:calc(100vw - 24px)}
    .emoji-pop button{font-size:22px;padding:5px;border-radius:8px;line-height:1}
    .emoji-pop button:hover{background:#F2F2F7}

    /* image lightbox */
    .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;align-items:center;justify-content:center;z-index:100;padding:24px;cursor:zoom-out}
    .lightbox.on{display:flex}
    .lightbox img{max-width:100%;max-height:100%;border-radius:10px;box-shadow:0 8px 40px rgba(0,0,0,.5)}
    .lightbox .lb-close{position:fixed;top:14px;right:18px;width:40px;height:40px;border-radius:99px;background:rgba(255,255,255,.14);color:#fff;font-size:20px;display:flex;align-items:center;justify-content:center;line-height:1;cursor:pointer}

    @media (max-width:640px){
      .hdr-inner{padding:9px 12px 11px}
      .av.lg{width:38px;height:38px;font-size:13px}
      .header-title{font-size:14px}
      .header-sub{font-size:11px}
      .feed{padding:12px 12px 18px}
      .row.recv{max-width:92%}
      .row.sent{max-width:86%}
      .bubble{font-size:15px;padding:8px 14px;max-width:100%}
      .react-btn{display:none !important}
      .att{width:200px}
      .composer-row{padding:8px 10px 6px;gap:8px}
      .attach-btn{width:34px;height:34px}
      .attach-btn svg{width:18px;height:18px}
      .composer-input{padding:5px 5px 5px 14px;min-height:34px}
      .editable{font-size:15px}
      .send-btn{width:26px;height:26px}
      .banner{padding:9px 12px;font-size:12px}
    }
  </style>
</head>
<body
  data-space-slug="{{ space.public_slug or space.id }}"
  data-self-party="{{ self_party }}"
  data-archived="{{ 'true' if space.status != 'active' else 'false' }}"
  data-brand-name="{{ space.brand_name or '' }}"
  data-creator-username="{{ space.creator_username or '' }}">

  <div class="wrap">

    <!-- HEADER -->
    <div class="hdr">
      <div class="hdr-inner">
        <div class="stack">
          <div class="av lg av-creator">{{ (space.creator_username or 'C')[:2] | upper }}</div>
          <div class="av lg av-brand">{{ (space.brand_name or 'B')[:1] | upper }}</div>
          <div class="av lg av-admin">JP</div>
        </div>
        <div class="header-title">{{ space.brand_name or space.campaign_name or 'Chat' }} &times; @{{ space.creator_username }}</div>
        <div class="header-sub">3 people &middot; {{ space.campaign_name or space.brand_name or 'Campaign' }}</div>
      </div>
    </div>

    {% if space.status == 'archived' %}
    <div class="banner archived">This campaign has ended — chat is archived and read-only.</div>
    {% elif space.status == 'approved' %}
    <div class="banner approved">This review has been approved — chat is closed and read-only.</div>
    {% endif %}

    <!-- FEED -->
    <div class="feed">
      <div id="messages"></div>
    </div>

    <!-- COMPOSER -->
    <div class="composer">
      <div class="composer-row">
        <input type="file" id="fileInput" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none">
        <button type="button" class="attach-btn" id="fileBtn" title="Attach image" {% if space.status != 'active' %}disabled{% endif %}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><path d="M21 15l-5-5L5 21"></path></svg>
        </button>
        <div class="composer-input">
          <div class="composer-typing" id="composerTyping" aria-hidden="true"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
          <div class="editable" id="bodyInput" contenteditable="{{ 'false' if space.status != 'active' else 'true' }}"
               data-empty="true" data-placeholder="Message" role="textbox" aria-label="Message"></div>
          <button type="button" class="send-btn" id="sendBtn" title="Send" {% if space.status != 'active' %}disabled{% endif %}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg>
          </button>
        </div>
      </div>
      <div class="notify-hint">{% if self_party == 'brand' %}@{{ space.creator_username }} and Jennifer will be notified{% else %}{{ space.brand_name or 'The brand' }} and Jennifer will be notified{% endif %}</div>
    </div>

  </div>

  <div class="emoji-pop" id="emojiPop">
    <button>👍</button><button>❤️</button><button>🎉</button><button>🔥</button><button>😂</button><button>👀</button><button>🙏</button><button>✅</button>
  </div>

  <div class="lightbox" id="lightbox" aria-hidden="true">
    <button type="button" class="lb-close" id="lbClose" title="Close" aria-label="Close">✕</button>
    <img id="lbImg" src="" alt="">
  </div>

<script id="initial-read-state" type="application/json">{{ initial_read_state | tojson }}</script>
<script>
(function(){
  var bodyEl = document.body;
  var spaceSlug = bodyEl.dataset.spaceSlug;
  var selfParty = bodyEl.dataset.selfParty;
  var archived = bodyEl.dataset.archived === 'true';
  var brandName = bodyEl.dataset.brandName || 'Brand';
  var creatorUsername = bodyEl.dataset.creatorUsername || 'Creator';

  var messagesEl = document.getElementById('messages');
  var editable = document.getElementById('bodyInput');
  var sendBtn = document.getElementById('sendBtn');
  var fileBtn = document.getElementById('fileBtn');
  var fileInput = document.getElementById('fileInput');
  var emojiPop = document.getElementById('emojiPop');
  var composerTyping = document.getElementById('composerTyping');
  var composerInput = document.querySelector('.composer-input');
  var lightbox = document.getElementById('lightbox');
  var lbImg = document.getElementById('lbImg');

  var lastId = 0;
  var initialLoaded = false;
  var readState = JSON.parse(document.getElementById('initial-read-state').textContent || '{}');
  var typingUsers = new Map();
  // Tracks the previously appended message so we can group clusters and
  // insert day separators without re-scanning the whole feed.
  var prevAppend = null;

  var SMILEY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M8 14s1.5 2 4 2 4-2 4-2"></path><line x1="9" y1="9" x2="9.01" y2="9"></line><line x1="15" y1="9" x2="15.01" y2="9"></line></svg>';

  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }

  function initials(name){
    var s = String(name || '').trim().replace(/^@+/, '');
    if(!s) return 'U';
    var words = s.split(/[ ._-]+/).filter(Boolean);
    if(words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
    return s.slice(0,2).toUpperCase();
  }

  // Per-party avatar + label metadata for received bubbles.
  function roleMeta(party, sender){
    if(party === 'admin') return {label:'Jennifer · INFLUENCE', initials:'JP', cls:'av-admin'};
    if(party === 'brand'){
      return {label:(sender || (brandName + ' Team')) + ' · Brand',
              initials:(brandName || 'B').slice(0,1).toUpperCase(), cls:'av-brand'};
    }
    var cu = sender || creatorUsername;
    return {label:'@' + cu + ' · Creator', initials:initials(cu), cls:'av-creator'};
  }

  function dayKey(d){ return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); }
  function fmtTime(d){ return d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}); }
  function fmtDay(d){
    var now = new Date();
    var today = dayKey(now);
    var y = new Date(now.getTime() - 86400000);
    if(dayKey(d) === today) return 'Today';
    if(dayKey(d) === dayKey(y)) return 'Yesterday';
    return d.toLocaleDateString([], {month:'short', day:'numeric'});
  }
  function insertDaySep(d){
    var el = document.createElement('div');
    el.className = 'day-sep';
    el.innerHTML = '<span>' + escapeHtml(fmtDay(d)) + '</span> <span class="t">' + escapeHtml(fmtTime(d)) + '</span>';
    messagesEl.appendChild(el);
  }

  function reactionPillHtml(m){
    var r = m.reactions || {};
    var keys = Object.keys(r);
    if(!keys.length) return '';
    var inner = '';
    for(var i=0;i<keys.length;i++){
      var k = keys[i];
      inner += '<span class="rx">' + escapeHtml(k) + (r[k] > 1 ? '<b>' + r[k] + '</b>' : '') + '</span>';
    }
    return '<button class="react-pill" data-msg="' + m.id + '" title="React">' + inner + '</button>';
  }

  function reactBtnHtml(m){
    if(archived) return '';
    return '<button class="react-btn" data-msg="' + m.id + '" title="React">' + SMILEY + '</button>';
  }

  // Builds the bubble(s) + attachment(s) for one message. The hover react
  // button and tapback pill are attached to the "primary" element (the text
  // bubble when there is text, else the first image).
  function buildContent(m, mine){
    var bodyText = (m.body || '').trim();
    var atts = m.attachments || [];
    var hasBody = !!bodyText;
    var pill = reactionPillHtml(m);
    var rbtn = reactBtnHtml(m);
    var html = '';
    if(hasBody){
      html += '<div class="bubble ' + (mine ? 'sent' : 'recv') + '">' +
        escapeHtml(bodyText) + rbtn + pill + '</div>';
    }
    for(var i=0;i<atts.length;i++){
      var a = atts[i];
      var primary = !hasBody && i === 0;
      html += '<div class="att-wrap' + (mine ? ' sent' : '') + '">' +
        '<img class="att" src="/chat/attachment/' + a.id + '" alt="' + escapeHtml(a.filename || 'attachment') + '" loading="lazy">' +
        (primary ? (rbtn + pill) : '') + '</div>';
    }
    return html;
  }

  function primaryHost(row){
    return row.querySelector('.bubble') || row.querySelector('.att-wrap');
  }

  function applyReactions(msgId, counts){
    var row = messagesEl.querySelector('[data-id="' + msgId + '"]');
    if(!row){ backfill(); return; }
    var host = primaryHost(row);
    if(!host) return;
    var old = host.querySelector('.react-pill');
    if(old) old.remove();
    var html = reactionPillHtml({id:msgId, reactions:counts || {}});
    if(html) host.insertAdjacentHTML('beforeend', html);
  }

  function updateReceipts(){
    var sent = messagesEl.querySelectorAll('.row.sent');
    for(var i=0;i<sent.length;i++){
      var row = sent[i];
      var st = row.querySelector('.status');
      if(i !== sent.length - 1){ if(st) st.remove(); continue; }
      if(!st){ st = document.createElement('div'); st.className = 'status'; row.appendChild(st); }
      var id = parseInt(row.dataset.id, 10);
      var readByOther = false;
      var entries = Object.entries(readState);
      for(var j=0;j<entries.length;j++){
        if(entries[j][0] !== selfParty && entries[j][1] >= id){ readByOther = true; break; }
      }
      st.textContent = readByOther ? 'Read' : 'Delivered';
    }
  }

  function renderMessage(m, opts){
    var upsert = opts && opts.upsert;
    var existing = messagesEl.querySelector('[data-id="' + m.id + '"]');
    var mine = m.party === selfParty;
    if(existing){
      if(upsert) applyReactions(m.id, m.reactions || {});
      if(m.id > lastId) lastId = m.id;
      return;
    }
    var ph = messagesEl.querySelector('.empty');
    if(ph) ph.remove();
    var created = m.created_at ? new Date(m.created_at) : new Date();
    var dk = dayKey(created);
    var freshDay = !prevAppend || prevAppend.day !== dk;
    if(freshDay) insertDaySep(created);
    var grouped = !mine && prevAppend && !prevAppend.mine &&
                  prevAppend.party === m.party && prevAppend.sender === (m.sender || '') && !freshDay;

    var row = document.createElement('div');
    row.dataset.id = m.id;
    row.dataset.party = m.party;
    row.dataset.sender = m.sender || '';

    if(mine){
      row.className = 'row sent' + ((prevAppend && !freshDay && prevAppend.mine) ? '' : ' fresh');
      row.innerHTML = '<div class="cluster">' + buildContent(m, true) + '</div>';
    } else {
      var freshCluster = !grouped;
      row.className = 'row recv' + (grouped ? ' grouped' : '') + (freshCluster ? ' fresh' : '');
      var meta = roleMeta(m.party, m.sender);
      var av = '<div class="av ' + meta.cls + '">' + escapeHtml(meta.initials) + '</div>';
      var label = grouped ? '' : '<div class="label">' + escapeHtml(meta.label) + '</div>';
      row.innerHTML = av + '<div class="cluster">' + label + buildContent(m, false) + '</div>';
    }

    messagesEl.appendChild(row);
    prevAppend = {party:m.party, sender:(m.sender || ''), mine:mine, day:dk};
    if(m.id > lastId) lastId = m.id;
    updateReceipts();
  }

  function nearBottom(){
    return (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 160);
  }
  function scrollToBottom(){ window.scrollTo(0, document.body.scrollHeight); }

  function sendRead(){
    if(!lastId) return;
    fetch('/chat/' + spaceSlug + '/read', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({up_to:lastId}),
    }).catch(function(){});
  }

  async function backfill(){
    try{
      var r = await fetch('/chat/' + spaceSlug + '/messages?since=' + lastId, {credentials:'same-origin'});
      if(!r.ok) return;
      var data = await r.json();
      if(data.messages && data.messages.length){
        var stick = !initialLoaded || nearBottom();
        for(var i=0;i<data.messages.length;i++) renderMessage(data.messages[i], {upsert:true});
        if(stick) scrollToBottom();
        sendRead();
      }
    }catch(e){}
  }

  // ── Typing indicator — shown at the start of the composer input ──
  function renderTyping(){
    var now = Date.now();
    var someone = false;
    typingUsers.forEach(function(info, key){
      if(info.until < now) typingUsers.delete(key);
      else someone = true;
    });
    // Only surface the other party's dots while you're not composing, so they
    // never fight your own text for the start of the input box.
    var show = someone && getBody() === '';
    composerTyping.classList.toggle('on', show);
    composerInput.classList.toggle('typing', show);
  }
  setInterval(renderTyping, 1000);

  // A message from a party means they've stopped typing — drop their bubble.
  function clearTypingFor(party){
    var changed = false;
    typingUsers.forEach(function(info, key){ if(info.party === party){ typingUsers.delete(key); changed = true; } });
    if(changed) renderTyping();
  }

  // ── Live updates via SSE, with periodic backfill as a safety net ──
  var sse = null;
  function connectSSE(){
    if(typeof EventSource === 'undefined') return;
    try{ sse = new EventSource('/chat/' + spaceSlug + '/stream'); }catch(e){ return; }
    sse.addEventListener('hello', function(){ backfill(); });
    sse.addEventListener('message', function(ev){
      try{
        var m = JSON.parse(ev.data);
        var stick = (m.party === selfParty) || nearBottom();
        renderMessage(m, {upsert:true});
        clearTypingFor(m.party);
        if(stick) scrollToBottom();
        sendRead();
      }catch(e){}
    });
    sse.addEventListener('reaction', function(ev){
      try{ var d = JSON.parse(ev.data); applyReactions(d.message_id, d.counts || {}); }catch(e){}
    });
    sse.addEventListener('read', function(ev){
      try{
        var d = JSON.parse(ev.data);
        var current = readState[d.party] || 0;
        if(d.last_read_message_id > current){
          readState[d.party] = d.last_read_message_id;
          updateReceipts();
        }
      }catch(e){}
    });
    sse.addEventListener('typing', function(ev){
      try{
        var d = JSON.parse(ev.data);
        if(d.party === selfParty) return;
        typingUsers.set(d.party + ':' + d.identifier, {party:d.party, name:d.display_name || d.party, until:Date.now() + 5000});
        renderTyping();
      }catch(e){}
    });
    sse.onerror = function(){ /* browser auto-reconnects; backfill covers gaps */ };
  }
  connectSSE();
  setInterval(backfill, 30000);

  // ── Compose + send ──
  function getBody(){ return (editable.textContent || '').trim(); }
  function updateEmptyState(){ editable.setAttribute('data-empty', getBody() ? 'false' : 'true'); }
  function clearBody(){ editable.textContent = ''; updateEmptyState(); }

  async function sendMessage(body, file){
    if(archived) return;
    if(!body && !file) return;
    var form = new FormData();
    if(body) form.append('body', body);
    if(file) form.append('attachment', file);
    sendBtn.disabled = true;
    try{
      var r = await fetch('/chat/' + spaceSlug + '/messages', {method:'POST', credentials:'same-origin', body:form});
      if(r.ok) clearBody();
    }finally{ sendBtn.disabled = archived; }
  }

  sendBtn.addEventListener('click', function(){ sendMessage(getBody(), null); });

  var lastTypingPing = 0;
  function pingTyping(){
    if(archived) return;
    var now = Date.now();
    if(now - lastTypingPing < 2000) return;
    lastTypingPing = now;
    fetch('/chat/' + spaceSlug + '/typing', {method:'POST', credentials:'same-origin'}).catch(function(){});
  }

  editable.addEventListener('input', function(){ updateEmptyState(); renderTyping(); });
  editable.addEventListener('keydown', function(e){
    if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(getBody(), null); return; }
    pingTyping();
  });
  // Keep pasted content plain-text so the bubble body stays clean.
  editable.addEventListener('paste', function(e){
    e.preventDefault();
    var t = (e.clipboardData || window.clipboardData).getData('text');
    document.execCommand('insertText', false, t);
  });

  fileBtn.addEventListener('click', function(){ if(!archived) fileInput.click(); });
  fileInput.addEventListener('change', function(){
    if(fileInput.files && fileInput.files[0]){
      sendMessage(getBody(), fileInput.files[0]);
      fileInput.value = '';
    }
  });

  // ── Reactions via emoji popover ──
  var emojiTargetMsg = null;
  function openEmojiPop(x, y){
    emojiPop.style.display = 'block';
    var w = emojiPop.offsetWidth, h = emojiPop.offsetHeight;
    var left = Math.max(8, Math.min(x, window.innerWidth - w - 8));
    var top = Math.max(8, y - h - 8);
    emojiPop.style.left = left + 'px';
    emojiPop.style.top = top + 'px';
  }
  emojiPop.addEventListener('click', function(e){
    if(e.target.tagName !== 'BUTTON') return;
    var emoji = e.target.textContent;
    emojiPop.style.display = 'none';
    if(emojiTargetMsg == null) return;
    fetch('/chat/' + spaceSlug + '/messages/' + emojiTargetMsg + '/react', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({emoji:emoji}),
    }).catch(function(){});
  });
  document.addEventListener('click', function(e){
    if(emojiPop.style.display === 'block' && !emojiPop.contains(e.target) && !e.target.closest('.react-btn') && !e.target.closest('.react-pill')){
      emojiPop.style.display = 'none';
    }
  });

  // Reaction entry points, available to every user on every message:
  //   • desktop  — hover a bubble → the react (smiley) button
  //   • any device — tap an existing reaction pill to change/remove it
  //   • touch    — long-press a bubble to open the emoji picker
  var lpTimer = null, lpFired = false;
  function bubbleHostFrom(t){
    return (t && t.closest) ? (t.closest('.bubble') || t.closest('.att-wrap')) : null;
  }
  function openReactPickerFor(host){
    var row = host.closest('.row');
    if(!row || archived) return;
    emojiTargetMsg = row.dataset.id;
    var r = host.getBoundingClientRect();
    openEmojiPop(r.left + Math.min(44, r.width / 2), r.top);
  }
  messagesEl.addEventListener('click', function(e){
    if(lpFired){ lpFired = false; return; }  // swallow the click a long-press emits
    var img = e.target.closest('.att');
    if(img){ openLightbox(img.getAttribute('src'), img.getAttribute('alt')); return; }
    var btn = e.target.closest('.react-btn') || e.target.closest('.react-pill');
    if(!btn || archived) return;
    emojiTargetMsg = btn.dataset.msg;
    var rect = btn.getBoundingClientRect();
    openEmojiPop(rect.left, rect.top);
  });
  messagesEl.addEventListener('touchstart', function(e){
    if(archived) return;
    var host = bubbleHostFrom(e.target);
    if(!host) return;
    lpFired = false;
    clearTimeout(lpTimer);
    lpTimer = setTimeout(function(){
      lpFired = true;
      openReactPickerFor(host);
      if(navigator.vibrate){ try{ navigator.vibrate(10); }catch(_){} }
    }, 450);
  }, {passive:true});
  function cancelLongPress(){ clearTimeout(lpTimer); lpTimer = null; }
  messagesEl.addEventListener('touchmove', cancelLongPress, {passive:true});
  messagesEl.addEventListener('touchend', function(e){ if(lpFired) e.preventDefault(); cancelLongPress(); });
  messagesEl.addEventListener('touchcancel', cancelLongPress);
  messagesEl.addEventListener('contextmenu', function(e){ if(bubbleHostFrom(e.target)) e.preventDefault(); });

  // ── Image lightbox (tap image to view full-screen, tap/Esc to close) ──
  function openLightbox(src, alt){
    if(!src) return;
    lbImg.setAttribute('src', src);
    lbImg.setAttribute('alt', alt || '');
    lightbox.classList.add('on');
    lightbox.setAttribute('aria-hidden', 'false');
  }
  function closeLightbox(){
    lightbox.classList.remove('on');
    lightbox.setAttribute('aria-hidden', 'true');
    lbImg.setAttribute('src', '');
  }
  lightbox.addEventListener('click', function(){ closeLightbox(); });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape' && lightbox.classList.contains('on')) closeLightbox();
  });

  updateEmptyState();
  backfill().then(function(){
    initialLoaded = true;
    if(!lastId) messagesEl.innerHTML = '<div class="empty">No messages yet — start the conversation 👋</div>';
    scrollToBottom();
  });
})();
</script>
</body>
</html>
"""


ADMIN_LOGIN_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Admin login — INFLUENCE</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:380px; margin:14vh auto; padding:24px; background:#f4f5f7; box-sizing:border-box; }
.card { background:#fff; padding:24px; border-radius:12px; border:1px solid #e5e5ea; }
input { width:100%; padding:10px; border:1px solid #d1d5db; border-radius:8px; font-size:16px; box-sizing:border-box; }
button { width:100%; padding:12px; background:#111827; color:#fff; border:0; border-radius:8px; margin-top:10px; cursor:pointer; font-size:14px; }
.err { color:#991b1b; font-size:13px; margin-top:8px; }
@media (max-width: 480px) {
  body { margin:6vh auto; padding:16px; max-width:100%; }
  .card { padding:18px; }
}
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
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Chat spaces — INFLUENCE Admin</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f4f5f7; margin:0; }
.container { max-width:1100px; margin:0 auto; padding:24px; box-sizing:border-box; }
h1 { font-size:20px; margin:0 0 16px; }
.stats { display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:16px; }
.stat { background:#fff; border:1px solid #e5e5ea; padding:14px 16px; border-radius:10px; }
.stat .v { font-size:22px; font-weight:600; }
.stat .l { font-size:12px; color:#6b7280; text-transform:uppercase; letter-spacing:.05em; }
.panel { background:#fff; border:1px solid #e5e5ea; padding:14px 16px; border-radius:10px; margin-bottom:16px; }
.panel h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:#6b7280; margin:0 0 8px; font-weight:500; }
.panel ol { margin:0; padding-left:18px; font-size:13px; color:#1f2937; }
.panel li + li { margin-top:4px; }
form.search { display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap; }
form.search input, form.search select { padding:8px 10px; border:1px solid #d1d5db; border-radius:8px; font-size:14px; box-sizing:border-box; }
form.search input[type=text] { min-width:240px; flex:1; }
form.search button { padding:8px 14px; border-radius:8px; border:1px solid #111827; background:#111827; color:#fff; font-size:13px; cursor:pointer; }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; }
.chips a { font-size:12px; padding:6px 12px; border-radius:14px; background:#fff; border:1px solid #d1d5db; color:#1f2937; text-decoration:none; }
.chips a.on { background:#111827; color:#fff; border-color:#111827; }
.table-wrap { background:#fff; border:1px solid #e5e5ea; border-radius:10px; overflow-x:auto; -webkit-overflow-scrolling:touch; }
table { width:100%; min-width:640px; background:#fff; border-collapse:separate; border-spacing:0; }
th, td { text-align:left; padding:10px 12px; font-size:13px; border-bottom:1px solid #f1f1f3; }
th { background:#f9fafb; font-weight:500; color:#6b7280; white-space:nowrap; }
tr:last-child td { border-bottom:0; }
a.row { color:#1d4ed8; text-decoration:none; white-space:nowrap; }
.tag { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; }
.tag.active { background:#dcfce7; color:#166534; }
.tag.archived { background:#fee2e2; color:#991b1b; }
.tag.approved { background:#dbeafe; color:#1e3a8a; }
@media (max-width: 900px) {
  .stats { grid-template-columns:repeat(3,1fr); }
}
@media (max-width: 640px) {
  .container { padding:14px; }
  h1 { font-size:18px; margin-bottom:12px; }
  .stats { grid-template-columns:repeat(2,1fr); gap:8px; }
  .stat { padding:10px 12px; }
  .stat .v { font-size:18px; }
  .stat .l { font-size:11px; }
  .panel { padding:12px; }
  form.search input[type=text] { min-width:0; width:100%; }
  th, td { padding:8px 10px; font-size:12px; }
}
</style></head><body>
<div class="container">
<h1>Chat spaces</h1>
<div class="stats">
  <div class="stat"><div class="v">{{ stats.active }}</div><div class="l">Active</div></div>
  <div class="stat"><div class="v">{{ stats.approved }}</div><div class="l">Approved</div></div>
  <div class="stat"><div class="v">{{ stats.archived }}</div><div class="l">Archived</div></div>
  <div class="stat"><div class="v">{{ stats.active_creators }}</div><div class="l">Active creators</div></div>
  <div class="stat"><div class="v">{{ stats.recently_active }}</div><div class="l">Active 7d</div></div>
</div>
{% if stats.top_revisions %}
<div class="panel">
  <h2>Campaigns with most revisions</h2>
  <ol>
  {% for r in stats.top_revisions %}
    <li>{{ r.campaign_name or '—' }}{% if r.brand_name %} <span style="color:#6b7280">· {{ r.brand_name }}</span>{% endif %} — <b>{{ r.revisions }}</b></li>
  {% endfor %}
  </ol>
</div>
{% endif %}
<form class="search" method="GET">
  <input type="text" name="q" value="{{ query.q or '' }}" placeholder="Search creator / campaign / brand…">
  <input type="text" name="brand" value="{{ query.brand or '' }}" placeholder="Brand exact match (optional)">
  <button type="submit">Filter</button>
  {% if query.q or query.brand or query.status %}
    <a href="/admin/chats" style="padding:8px 10px;color:#6b7280;text-decoration:none">Clear</a>
  {% endif %}
</form>
<div class="chips">
  <a href="/admin/chats" class="{% if not query.status %}on{% endif %}">All</a>
  <a href="/admin/chats?status=active" class="{% if query.status == 'active' %}on{% endif %}">Active</a>
  <a href="/admin/chats?status=approved" class="{% if query.status == 'approved' %}on{% endif %}">Approved</a>
  <a href="/admin/chats?status=archived" class="{% if query.status == 'archived' %}on{% endif %}">Archived</a>
</div>
<div class="table-wrap">
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
</div>
</div></body></html>
"""


ADMIN_CHAT_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ space.campaign_name or 'Chat' }} — Admin</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f4f5f7; margin:0; }
.container { max-width:820px; margin:0 auto; padding:24px; box-sizing:border-box; }
.crumbs { font-size:13px; color:#6b7280; margin-bottom:8px; }
.crumbs a { color:#1d4ed8; text-decoration:none; }
h1 { font-size:18px; margin:0 0 4px; word-wrap:break-word; }
.meta { font-size:13px; color:#6b7280; margin-bottom:12px; word-wrap:break-word; }
.toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:14px; }
.toolbar a, .toolbar button { font-size:13px; padding:8px 12px; border-radius:8px; text-decoration:none; border:1px solid #d1d5db; background:#fff; color:#1f2937; cursor:pointer; min-height:36px; box-sizing:border-box; }
.toolbar form { display:inline; margin:0; }
.toolbar .danger { color:#991b1b; border-color:#fecaca; background:#fff1f2; }
.toolbar .primary { color:#fff; background:#111827; border-color:#111827; }
.archived-note { background:#fef2f2; color:#991b1b; padding:8px 12px; border-radius:8px; font-size:13px; margin-bottom:12px; }
.msg { background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:10px 14px; margin:8px 0; }
.msg.party-admin { background:#fffbeb; border-color:#fde68a; }
.msg .who { font-size:11px; color:#6b7280; margin-bottom:2px; }
.msg .body { white-space:pre-wrap; font-size:14px; word-break:break-word; }
.msg .ts { font-size:11px; color:#9ca3af; margin-top:4px; }
.msg img { max-width:min(240px, 80%); border-radius:8px; margin-top:6px; display:block; }
.reactions { margin-top:6px; font-size:12px; color:#6b7280; }
.compose { background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:10px; margin-top:14px; }
.compose textarea { width:100%; padding:10px 12px; border-radius:8px; border:1px solid #d1d5db; font-family:inherit; font-size:16px; min-height:60px; box-sizing:border-box; resize:vertical; }
.compose .row { display:flex; justify-content:space-between; align-items:center; margin-top:8px; gap:8px; flex-wrap:wrap; }
.compose .row .hint { font-size:11px; color:#6b7280; flex:1 1 60%; min-width:0; }
.compose button { background:#111827; color:#fff; border:0; padding:10px 16px; border-radius:8px; cursor:pointer; font-size:14px; min-height:40px; }
@media (max-width: 640px) {
  .container { padding:14px; }
  h1 { font-size:17px; }
  .meta { font-size:12px; }
  .toolbar a, .toolbar button { font-size:12px; padding:8px 10px; }
  .msg { padding:9px 12px; }
  .msg .body { font-size:13px; }
  .compose .row .hint { flex-basis:100%; }
}
</style></head><body>
<div class="container">
<div class="crumbs"><a href="/admin/chats">← All chat spaces</a></div>
<h1>{{ title }}</h1>
<div class="meta">Campaign {{ space.campaign_name or '—' }} · Brand {{ space.brand_name or '—' }} · Creator @{{ space.creator_username }}{% if space.creator_email %} ({{ space.creator_email }}){% endif %} · status: {{ space.status }} · created {{ space.created_at.strftime('%Y-%m-%d %H:%M') if space.created_at else '—' }}</div>

<div class="toolbar">
  <a href="/admin/chats/{{ space.id }}/export.md" download>Export Markdown</a>
  <a href="/admin/chats/{{ space.id }}/export.json" download>Export JSON</a>
  {% if space.status == 'active' %}
    <form method="POST" action="/admin/chats/{{ space.id }}/archive">
      <input type="hidden" name="redirect" value="/admin/chats/{{ space.id }}">
      <button type="submit" class="danger" onclick="return confirm('Archive this chat? Both parties will lose access until it is reopened.');">Archive</button>
    </form>
  {% else %}
    <form method="POST" action="/admin/chats/{{ space.id }}/reopen">
      <input type="hidden" name="redirect" value="/admin/chats/{{ space.id }}">
      <button type="submit" class="primary">Reopen</button>
    </form>
  {% endif %}
</div>

{% if space.status == 'archived' %}<div class="archived-note">This chat is archived. Reopen it before posting; existing sessions stay revoked, so both parties will need fresh magic links.</div>{% endif %}
{% if space.status == 'approved' %}<div class="archived-note" style="background:#ecfdf5;color:#065f46;">This review was approved. The chat is closed for the brand and creator but stays here as a record. It will be archived automatically when the campaign ends.</div>{% endif %}

{% for m in messages %}
<div class="msg party-{{ m.party }}">
  <div class="who">{{ m.sender }} · {{ m.party }}</div>
  {% if m.body and m.body.strip() %}<div class="body">{{ m.body }}</div>{% endif %}
  {% for a in m.attachments %}<img src="/chat/attachment/{{ a.id }}?admin=1" alt="{{ a.filename }}">{% endfor %}
  {% if m.reactions %}<div class="reactions">{% for emoji, count in m.reactions.items() %}{{ emoji }} {{ count }} &nbsp;{% endfor %}</div>{% endif %}
  <div class="ts">{{ m.created_at }}</div>
</div>
{% endfor %}
{% if not messages %}<div style="text-align:center;color:#6b7280;padding:24px">No messages yet.</div>{% endif %}

{% if space.status == 'active' %}
<form class="compose" method="POST" action="/admin/chats/{{ space.id }}/messages">
  <input type="hidden" name="redirect" value="/admin/chats/{{ space.id }}">
  <textarea name="body" placeholder="Post a message as Influence (visible to both creator and brand)…" required></textarea>
  <div class="row">
    <span class="hint">Sender will appear as <b>Influence</b>. The creator will get an email and the brand workspace will be pinged.</span>
    <button type="submit">Send</button>
  </div>
</form>
{% endif %}

</div></body></html>
"""


ERROR_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ heading }} — INFLUENCE</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:520px; margin:14vh auto; padding:0 24px; color:#1d1d1f; }
h1 { font-size:22px; margin-bottom:8px; }
p { color:#4b5563; line-height:1.5; }
@media (max-width: 480px) {
  body { margin:8vh auto; padding:0 18px; }
  h1 { font-size:20px; }
}
</style></head><body>
<h1>{{ heading }}</h1>
<p>{{ message }}</p>
</body></html>
"""
