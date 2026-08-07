"""
HTML templates for the chat web UI.

Kept as Jinja strings to match the existing inline-HTML pattern used by
`/slack/oauth_redirect`. Pages:
  - chat_page: the actual chat view (creator + brand)
  - admin_login_page: simple admin-token gate
  - admin_chat_page: read-only admin view of one chat
  - error_page: shared error template
"""

CHAT_PAGE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{{ chat_title }} — {{ 'Admin' if is_admin else 'INFLUENCE' }}</title>
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
    /* iMessage-style links: blue + underlined on the light received bubble,
       white + underlined on the dark sent bubble so they stay readable. */
    .bubble a.lk{text-decoration:underline;text-underline-offset:2px;word-break:break-all}
    .bubble.recv a.lk{color:#007AFF}
    .bubble.sent a.lk{color:#fff}
    /* admin silent-edit: bubble becomes an inline editor on double-click */
    .bubble.editing{outline:2px solid #007AFF;outline-offset:1px;cursor:text;
      -webkit-user-select:text;user-select:text}

    .att-wrap{position:relative;width:fit-content;margin-top:1px}
    .att-wrap.sent{align-self:flex-end}
    .att{display:block;width:240px;max-width:70vw;border-radius:18px;box-shadow:inset 0 0 0 .5px rgba(0,0,0,.08);cursor:zoom-in}

    /* ── DRAFT CARD ──
       A new video submitted for review, rendered as an iMessage rich-link
       card: media panel on top, bubble-coloured caption band underneath. */
    .rcard-wrap{position:relative;width:fit-content;margin-top:2px}
    .rcard-wrap.sent{align-self:flex-end}
    .rcard{display:block;width:268px;max-width:74vw;border-radius:18px;overflow:hidden;
      text-decoration:none;color:inherit;background:var(--recv-bg);
      box-shadow:inset 0 0 0 .5px rgba(0,0,0,.10);
      transition:transform .14s cubic-bezier(.32,.72,0,1)}
    a.rcard:active{transform:scale(.98)}
    .rcard-media{position:relative;height:134px;display:flex;align-items:center;justify-content:center;
      background:radial-gradient(120% 130% at 26% 8%,#3A3A3C 0%,#1C1C1E 58%,#0B0B0C 100%)}
    .rcard-media:after{content:'';position:absolute;inset:0;
      background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(0,0,0,.18))}
    .rcard-play{position:relative;z-index:1;width:54px;height:54px;border-radius:99px;color:#fff;
      background:rgba(255,255,255,.18);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
      display:flex;align-items:center;justify-content:center;box-shadow:0 1px 14px rgba(0,0,0,.30)}
    .rcard-play svg{margin-left:2px}
    .rcard-badge{position:absolute;z-index:1;top:10px;left:10px;font-size:10px;font-weight:600;
      letter-spacing:.04em;text-transform:uppercase;color:#fff;background:rgba(255,255,255,.20);
      -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
      border-radius:99px;padding:4px 8px;line-height:1}
    .rcard-foot{padding:9px 14px 11px;background:var(--recv-bg);color:var(--recv-fg)}
    .rcard-wrap.sent .rcard-foot{background:var(--sent-bg);color:var(--sent-fg)}
    .rcard-title{font-size:14.5px;font-weight:600;letter-spacing:-.015em;line-height:1.25}
    .rcard-sub{font-size:12.5px;line-height:1.3;margin-top:2px;color:var(--muted);letter-spacing:-.005em}
    .rcard-wrap.sent .rcard-sub{color:rgba(255,255,255,.60)}

    /* ── SYSTEM NOTICE ── centred, unobtrusive: "Draft 2 approved". */
    .row.sys-row{align-self:center;max-width:88%;margin-top:16px}
    .sys{display:flex;align-items:center;justify-content:center;gap:5px;font-size:11.5px;
      color:var(--muted);letter-spacing:-.005em;text-align:center}
    .sys .sys-dot{display:inline-flex;align-items:center;justify-content:center;
      width:15px;height:15px;border-radius:99px;color:#fff;flex-shrink:0}
    .sys.ok .sys-dot{background:#34C759}
    .sys.chg .sys-dot{background:#FF9F0A}

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
      .bubble,.att-wrap,.rcard-wrap{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
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
      .rcard{width:244px}
      .rcard-media{height:122px}
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

    {% if is_admin %}
      {% if space.status == 'archived' %}
      <div class="banner archived">This chat is archived. Reopen it before posting; existing sessions stay revoked, so both parties will need fresh magic links.</div>
      {% elif space.status == 'approved' %}
      <div class="banner approved">This review was approved. The chat is closed for the brand and creator but stays here as a record. It will be archived automatically when the campaign ends.</div>
      {% endif %}
    {% else %}
      {% if space.status == 'archived' %}
      <div class="banner archived">This campaign has ended — chat is archived and read-only.</div>
      {% elif space.status == 'approved' %}
      <div class="banner approved">This review has been approved — chat is closed and read-only.</div>
      {% endif %}
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
               data-empty="true" data-placeholder="{{ 'Message as Influence' if is_admin else 'Message' }}" role="textbox" aria-label="Message"></div>
          <button type="button" class="send-btn" id="sendBtn" title="Send" {% if space.status != 'active' %}disabled{% endif %}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg>
          </button>
        </div>
      </div>
      <div class="notify-hint">{% if is_admin %}Posting as Influence — @{{ space.creator_username }} and {{ space.brand_name or 'the brand' }} will be notified · double-click a message to edit it silently{% elif self_party == 'brand' %}@{{ space.creator_username }} and Jennifer will be notified{% else %}{{ space.brand_name or 'The brand' }} and Jennifer will be notified{% endif %}</div>
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
  var isAdmin = selfParty === 'admin';
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

  // Admin API calls carry ?as=admin so the server acts on the admin identity
  // even when a stale creator/brand session cookie for this space is present.
  function withAs(url){
    if(!isAdmin) return url;
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'as=admin';
  }

  var lastId = 0;
  var initialLoaded = false;
  var readState = JSON.parse(document.getElementById('initial-read-state').textContent || '{}');
  var typingUsers = new Map();
  // Tracks the previously appended message so we can group clusters and
  // insert day separators without re-scanning the whole feed.
  var prevAppend = null;

  var SMILEY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M8 14s1.5 2 4 2 4-2 4-2"></path><line x1="9" y1="9" x2="9.01" y2="9"></line><line x1="15" y1="9" x2="15.01" y2="9"></line></svg>';
  var PLAY = '<svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.79-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z"></path></svg>';
  var TICK = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
  var PENCIL = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>';

  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }

  // Turn bare http(s):// URLs in a message body into iMessage-style links.
  // Works on the RAW text (not pre-escaped HTML): each non-URL span and each
  // URL is escaped independently, so the output stays XSS-safe. Only http/https
  // are matched, so javascript:/data: schemes can never become an anchor.
  var URL_RE = /(https?:\/\/[^\s]+)/gi;
  function linkify(text){
    text = String(text || '');
    var out = '', last = 0, m;
    URL_RE.lastIndex = 0;
    while((m = URL_RE.exec(text)) !== null){
      out += escapeHtml(text.slice(last, m.index));
      var url = m[0];
      // Trailing punctuation usually isn't part of the URL (end of sentence,
      // closing bracket, etc.) — peel it off so it renders outside the link.
      var trail = '';
      var tm = url.match(/[.,!?;:'")\\]}>]+$/);
      if(tm){ trail = tm[0]; url = url.slice(0, url.length - trail.length); }
      if(url){
        var safe = escapeHtml(url);
        out += '<a class="lk" href="' + safe + '" target="_blank" rel="noopener noreferrer nofollow">' + safe + '</a>';
      }
      out += escapeHtml(trail);
      last = m.index + m[0].length;
    }
    out += escapeHtml(text.slice(last));
    return out;
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

  // ── Draft card ────────────────────────────────────────────────────────
  // A `review_submission` message is a new video sent for review. It renders
  // as a rich card rather than a bubble, so a resubmission is unmistakable
  // in a conversation that already has feedback in it.
  function isHttpUrl(u){
    var s = String(u || '').trim().toLowerCase();
    return s.indexOf('http://') === 0 || s.indexOf('https://') === 0;
  }
  function hostOf(u){
    try{
      var h = new URL(u).hostname;
      return h.indexOf('www.') === 0 ? h.slice(4) : h;
    }catch(e){ return ''; }
  }
  // Name the destination the way the creator thinks of it.
  function sourceLabel(host){
    if(!host) return 'Video';
    if(host.indexOf('drive.google') === 0 || host.indexOf('docs.google') === 0) return 'Google Drive';
    if(host.indexOf('dropbox') >= 0) return 'Dropbox';
    if(host.indexOf('frame.io') >= 0) return 'Frame.io';
    if(host.indexOf('wetransfer') >= 0) return 'WeTransfer';
    if(host.indexOf('youtu') >= 0) return 'YouTube';
    if(host.indexOf('vimeo') >= 0) return 'Vimeo';
    if(host.indexOf('icloud') >= 0) return 'iCloud';
    return host;
  }
  function draftNumber(ev){
    var n = parseInt(ev.submission_number, 10);
    return (n > 0) ? n : 1;
  }
  function reviewCardHtml(m){
    var ev = m.event || {};
    var n = draftNumber(ev);
    var link = isHttpUrl(ev.video_link) ? String(ev.video_link).trim() : '';
    var title = n > 1 ? 'Revised draft submitted' : 'New draft submitted';
    var sub = link ? (sourceLabel(hostOf(link)) + ' · Tap to watch') : 'No link attached';
    var inner =
      '<div class="rcard-media">' +
        '<span class="rcard-badge">Draft ' + n + '</span>' +
        '<span class="rcard-play">' + PLAY + '</span>' +
      '</div>' +
      '<div class="rcard-foot">' +
        '<div class="rcard-title">' + escapeHtml(title) + '</div>' +
        '<div class="rcard-sub">' + escapeHtml(sub) + '</div>' +
      '</div>';
    if(!link) return '<div class="rcard">' + inner + '</div>';
    return '<a class="rcard" href="' + escapeHtml(link) +
      '" target="_blank" rel="noopener noreferrer nofollow">' + inner + '</a>';
  }

  // `review_decision` — a centred system line, the way iMessage announces
  // something that happened to the conversation rather than in it.
  function decisionHtml(m){
    var ev = m.event || {};
    var approved = ev.decision === 'approved';
    var text = 'Draft ' + draftNumber(ev) +
      (approved ? ' approved' : ' — changes requested');
    return '<div class="sys ' + (approved ? 'ok' : 'chg') + '">' +
      '<span class="sys-dot">' + (approved ? TICK : PENCIL) + '</span>' +
      '<span>' + escapeHtml(text) + '</span></div>';
  }

  // Builds the bubble(s), draft card and attachment(s) for one message. The
  // hover react button and tapback pill are attached to the "primary" element
  // (the text bubble when there is text, else the card, else the first image).
  function buildContent(m, mine){
    var bodyText = (m.body || '').trim();
    var atts = m.attachments || [];
    var hasBody = !!bodyText;
    var hasCard = m.kind === 'review_submission';
    var pill = reactionPillHtml(m);
    var rbtn = reactBtnHtml(m);
    var html = '';
    if(hasBody){
      html += '<div class="bubble ' + (mine ? 'sent' : 'recv') + '">' +
        linkify(bodyText) + rbtn + pill + '</div>';
    }
    if(hasCard){
      html += '<div class="rcard-wrap' + (mine ? ' sent' : '') + '">' +
        reviewCardHtml(m) + (hasBody ? '' : (rbtn + pill)) + '</div>';
    }
    for(var i=0;i<atts.length;i++){
      var a = atts[i];
      var primary = !hasBody && !hasCard && i === 0;
      html += '<div class="att-wrap' + (mine ? ' sent' : '') + '">' +
        '<img class="att" src="/chat/attachment/' + a.id + '" alt="' + escapeHtml(a.filename || 'attachment') + '" loading="lazy">' +
        (primary ? (rbtn + pill) : '') + '</div>';
    }
    return html;
  }

  function primaryHost(row){
    return row.querySelector('.bubble') || row.querySelector('.rcard-wrap') ||
           row.querySelector('.att-wrap');
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

  // ── Admin silent edit ──────────────────────────────────────────────────
  // Admins can double-click a text bubble to correct its wording in place.
  // The edit is quiet by design: no notification and no "edited" marker — the
  // text just changes. `editingRow` guards against re-entrancy and against a
  // live `edit` event clobbering an in-progress edit.
  var editingRow = null;

  // Re-render a bubble's body while keeping its react button + reaction pill.
  function rebuildBubble(row, body){
    var bubble = row.querySelector('.bubble');
    if(!bubble) return;
    var pill = bubble.querySelector('.react-pill');
    bubble.innerHTML = linkify(body) + reactBtnHtml({id:row.dataset.id}) +
                       (pill ? pill.outerHTML : '');
  }

  function applyEdit(msgId, body){
    if(typeof body !== 'string') return;
    var row = messagesEl.querySelector('[data-id="' + msgId + '"]');
    if(!row || row === editingRow) return;
    row.dataset.body = body;
    rebuildBubble(row, body);
  }

  function beginEdit(row){
    if(!isAdmin || !row || editingRow) return;
    var bubble = row.querySelector('.bubble');
    if(!bubble) return;  // image-only messages have no text to edit
    editingRow = row;
    var raw = row.dataset.body || '';
    var pill = bubble.querySelector('.react-pill');
    var pillHtml = pill ? pill.outerHTML : '';

    bubble.classList.add('editing');
    bubble.textContent = raw;                       // strip links/buttons while editing
    bubble.setAttribute('contenteditable', 'true');
    bubble.focus();
    try{
      var range = document.createRange(); range.selectNodeContents(bubble);
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }catch(e){}

    var done = false;
    function finish(save){
      if(done) return; done = true;
      bubble.removeEventListener('keydown', onKey);
      bubble.removeEventListener('blur', onBlur);
      bubble.removeAttribute('contenteditable');
      bubble.classList.remove('editing');
      editingRow = null;
      // innerText keeps the line breaks; normalise the empty-block artefact.
      var next = (bubble.innerText || '').replace(/\\r\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
      if(save && next && next !== raw){
        row.dataset.body = next;
        bubble.innerHTML = linkify(next) + reactBtnHtml({id:row.dataset.id}) + pillHtml;
        // If the server rejects the edit, put the original text back so the UI
        // never shows a change that didn't persist.
        function revert(){ row.dataset.body = raw; rebuildBubble(row, raw); }
        fetch(withAs('/chat/' + spaceSlug + '/messages/' + row.dataset.id + '/edit'), {
          method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({body: next}),
        }).then(function(r){ if(!r.ok) revert(); }).catch(revert);
      } else {
        bubble.innerHTML = linkify(raw) + reactBtnHtml({id:row.dataset.id}) + pillHtml;
      }
    }
    function onKey(e){
      if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); finish(true); }
      else if(e.key === 'Escape'){ e.preventDefault(); finish(false); }
    }
    function onBlur(){ finish(true); }
    bubble.addEventListener('keydown', onKey);
    bubble.addEventListener('blur', onBlur);
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
    row.dataset.body = m.body || '';  // raw text, so an admin edit can prefill it

    // System notices sit centred on their own, belonging to neither side.
    if(m.kind === 'review_decision'){
      row.className = 'row sys-row';
      row.innerHTML = decisionHtml(m);
      messagesEl.appendChild(row);
      prevAppend = {party:'system', sender:'', mine:false, day:dk};
      if(m.id > lastId) lastId = m.id;
      updateReceipts();
      return;
    }

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
    fetch(withAs('/chat/' + spaceSlug + '/read'), {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({up_to:lastId}),
    }).catch(function(){});
  }

  async function backfill(){
    try{
      var r = await fetch(withAs('/chat/' + spaceSlug + '/messages?since=' + lastId), {credentials:'same-origin'});
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
    try{ sse = new EventSource(withAs('/chat/' + spaceSlug + '/stream')); }catch(e){ return; }
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
    sse.addEventListener('edit', function(ev){
      try{ var d = JSON.parse(ev.data); applyEdit(d.message_id, d.body); }catch(e){}
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
  // Read the composer as text WITH its line breaks. `textContent` would flatten
  // the <div>/<br> nodes contenteditable creates on Enter/paste (turning a
  // multi-line note into one run-on paragraph); `innerText` keeps them. Collapse
  // the empty-block artefact (a single blank line comes back as three newlines)
  // so one blank line stays one blank line, then trim the ends.
  function getBody(){
    var t = (editable.innerText || editable.textContent || '');
    return t.replace(/\\r\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
  }
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
      var r = await fetch(withAs('/chat/' + spaceSlug + '/messages'), {method:'POST', credentials:'same-origin', body:form});
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
    fetch(withAs('/chat/' + spaceSlug + '/typing'), {method:'POST', credentials:'same-origin'}).catch(function(){});
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
    fetch(withAs('/chat/' + spaceSlug + '/messages/' + emojiTargetMsg + '/react'), {
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
    if(!t || !t.closest) return null;
    return t.closest('.bubble') || t.closest('.rcard-wrap') || t.closest('.att-wrap');
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

  // Admin-only: double-click a text bubble to silently edit it in place.
  if(isAdmin){
    messagesEl.addEventListener('dblclick', function(e){
      var bubble = e.target.closest('.bubble');
      if(!bubble) return;
      var row = bubble.closest('.row');
      if(!row) return;
      e.preventDefault();
      beginEdit(row);
    });
  }

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
  {% if next_url %}<input type="hidden" name="next" value="{{ next_url }}">{% endif %}
  <button type="submit">Enter</button>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</form></div></body></html>
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
