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
    .rcard-media{position:relative;height:134px;overflow:hidden;
      display:flex;align-items:center;justify-content:center;
      background:radial-gradient(120% 130% at 26% 8%,#3A3A3C 0%,#1C1C1E 58%,#0B0B0C 100%)}
    .rcard-media:after{content:'';position:absolute;inset:0;
      background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(0,0,0,.18))}
    /* Fetched preview. Drafts are usually vertical, so the thumbnail is
       letterboxed over a blurred blow-up of itself rather than cropped to
       a 2:1 sliver. Both sit under the :after scrim, badge and play button. */
    .rcard-thumb,.rcard-thumb-bg{position:absolute;inset:0;width:100%;height:100%;
      opacity:0;transition:opacity .3s ease;pointer-events:none}
    .rcard-thumb{object-fit:contain}
    .rcard-thumb-bg{object-fit:cover;transform:scale(1.4);
      filter:blur(18px) saturate(1.1) brightness(.5)}
    .rcard-media.has-thumb .rcard-thumb{opacity:1}
    .rcard-media.has-thumb .rcard-thumb-bg{opacity:1}
    /* Over a real image the badge and play glyph need a firmer scrim. */
    .rcard-media.has-thumb:after{background:linear-gradient(180deg,rgba(0,0,0,.26),rgba(0,0,0,.40))}
    .rcard-play{position:relative;z-index:1;width:54px;height:54px;border-radius:99px;color:#fff;
      background:rgba(255,255,255,.18);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
      display:flex;align-items:center;justify-content:center;box-shadow:0 1px 14px rgba(0,0,0,.30)}
    /* Light glass reads well on the dark placeholder but turns milky over a
       photo — dark glass keeps the white glyph crisp whatever the frame is. */
    .rcard-media.has-thumb .rcard-play{background:rgba(0,0,0,.34)}
    /* No nudge here — PLAY is drawn with its centre of mass on the viewBox
       centre, so flex centring already puts it optically dead centre. */
    .rcard-play svg{display:block}
    .rcard-badge{position:absolute;z-index:1;top:10px;left:10px;font-size:10px;font-weight:600;
      letter-spacing:.04em;text-transform:uppercase;color:#fff;background:rgba(255,255,255,.20);
      -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
      border-radius:99px;padding:4px 8px;line-height:1}
    .rcard-foot{padding:9px 14px 11px;background:var(--recv-bg);color:var(--recv-fg)}
    .rcard-wrap.sent .rcard-foot{background:var(--sent-bg);color:var(--sent-fg)}
    .rcard-title{font-size:14.5px;font-weight:600;letter-spacing:-.015em;line-height:1.25}
    .rcard-sub{font-size:12.5px;line-height:1.3;margin-top:2px;color:var(--muted);letter-spacing:-.005em}
    .rcard-wrap.sent .rcard-sub{color:rgba(255,255,255,.60)}

    /* ── SYSTEM NOTICE ── centred, unobtrusive: "Draft 2 approved".
       Column, because a notice that opened a next step stacks the action
       underneath itself rather than beside it. */
    .row.sys-row{align-self:center;max-width:88%;margin-top:16px;flex-direction:column}
    .sys{display:flex;align-items:center;justify-content:center;gap:5px;font-size:11.5px;
      color:var(--muted);letter-spacing:-.005em;text-align:center}
    .sys .sys-dot{display:inline-flex;align-items:center;justify-content:center;
      width:15px;height:15px;border-radius:99px;color:#fff;flex-shrink:0}
    .sys.ok .sys-dot{background:#34C759}
    .sys.chg .sys-dot{background:#FF9F0A}
    .sys.post .sys-dot{background:#0A84FF}

    /* ── NEXT STEP ──
       At most one of these exists at a time, anywhere on the page: whatever
       it is the creator's turn to do. It appears twice, from one source —
       inline under the system notice that opened it, so the history reads
       in order, and as a strip above the composer so it's reachable from
       anywhere in a long scroll. */
    .sys-act{display:flex;justify-content:center;margin-top:7px}
    .sys-act a{display:inline-flex;align-items:center;gap:6px;padding:7px 15px;border-radius:99px;
      background:var(--sent-bg);color:#fff;font-size:12.5px;font-weight:600;letter-spacing:-.01em;
      text-decoration:none;transition:transform .14s cubic-bezier(.32,.72,0,1),opacity .14s ease}
    .sys-act a:active{transform:scale(.97)}
    .sys-act a .chev{opacity:.65;flex-shrink:0}
    /* The admin sees what the creator is being shown, but it isn't theirs
       to click, so it reads as a label rather than an affordance. */
    .sys-act.mirror span{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;border-radius:99px;
      background:var(--recv-bg);color:#3C3C43;font-size:11.5px;font-weight:500;letter-spacing:-.005em}

    /* The strip. Sits inside the sticky composer so it scrolls with nothing
       and never covers the last message. */
    .nextstep{max-width:820px;margin:0 auto;width:100%;padding:9px 16px 3px;
      display:flex;align-items:center;gap:11px}
    .nextstep.hidden{display:none}
    .nextstep-main{flex:1;display:flex;align-items:center;gap:11px;padding:10px 14px;border-radius:14px;
      background:var(--sent-bg);color:#fff;text-decoration:none;min-width:0;
      transition:transform .14s cubic-bezier(.32,.72,0,1)}
    .nextstep-main:active{transform:scale(.99)}
    .nextstep-icon{width:28px;height:28px;border-radius:99px;background:rgba(255,255,255,.16);
      display:flex;align-items:center;justify-content:center;flex-shrink:0}
    .nextstep-text{min-width:0;flex:1}
    .nextstep-label{display:block;font-size:14px;font-weight:600;letter-spacing:-.015em;line-height:1.25;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .nextstep-detail{display:block;font-size:11.5px;line-height:1.3;margin-top:1px;color:rgba(255,255,255,.62);
      letter-spacing:-.005em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .nextstep-go{opacity:.7;flex-shrink:0}
    /* Dismiss is deliberately outside the tap target: reading the strip and
       getting rid of it are different intents, and it comes back on reload
       because the step is still open. */
    .nextstep-x{width:28px;height:28px;border-radius:99px;color:var(--muted);flex-shrink:0;
      display:flex;align-items:center;justify-content:center}
    .nextstep-x:hover{background:var(--recv-bg);color:#3C3C43}
    /* Once a review is approved (or the campaign has ended) nobody can type,
       and a disabled input is just a dead end at the exact moment the
       creator still has something to do. Drop it and let the strip be the
       bar; with no step left either, the bar itself has no reason to exist.
       The markup stays put — only its visibility changes — so none of the
       composer's wiring has to care about the state it's in. */
    .composer.no-input .composer-row,
    .composer.no-input .notify-hint{display:none}
    .composer.no-input:not(.has-step){display:none}
    .composer.no-input .nextstep{padding:11px 16px calc(11px + env(safe-area-inset-bottom))}

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
    /* z-index keeps the sticky bar above message decorations (draft badge,
       play glyph, reaction pill) as they scroll past underneath it. Those are
       positioned with a positive z-index and would otherwise paint on top of
       the composer, which sits at the auto level — the header dodges the same
       trap with its own z-index above. Stays below the emoji popover (50) and
       lightbox (100), which are meant to cover the bar. */
    .composer{position:sticky;bottom:0;z-index:20;background:rgba(255,255,255,.85);
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

    /* ── AI DRAFTS (admin only) ──
       A sheet above the composer: say what you want to get across in the
       intent field, and the drafts come back as received-style bubbles — the
       same grey pill the other side's messages arrive in — so picking one
       reads as lifting a message out of the thread rather than operating a
       separate tool. Each bubble can be edited in place (the same
       contenteditable trick as admin silent-edit) before it's used. Tapping a
       bubble fills the composer; nothing sends until the admin presses send. */
    .ai-btn{width:38px;height:38px;border-radius:99px;background:var(--recv-bg);color:var(--sent-bg);
      display:flex;align-items:center;justify-content:center;flex-shrink:0;
      transition:background .16s ease,color .16s ease}
    .ai-btn.on{background:var(--sent-bg);color:#fff}
    .ai-btn:disabled{opacity:.4;cursor:not-allowed}
    .drafts{display:none;max-width:820px;margin:0 auto;width:100%;padding:10px 16px 0}
    .drafts.on{display:block}
    .drafts-head{display:flex;align-items:center;gap:10px;padding:0 4px 7px;
      font-size:11px;color:var(--muted);letter-spacing:-.005em}
    .drafts-head .sp{flex:1}
    .drafts-head button{font-size:11px;color:#007AFF;padding:2px 2px;letter-spacing:-.005em}
    .drafts-head button:disabled{color:var(--muted);cursor:not-allowed}

    /* Intent field — "what do you want to say?". Same pill as the composer so
       it reads as a place to type, one row up. */
    .intent{background:#fff;border:.5px solid var(--line-2);border-radius:22px;
      padding:6px 6px 6px 16px;display:flex;align-items:flex-end;gap:6px;min-height:38px;
      margin-bottom:8px}
    .intent .editable{font-size:15px;max-height:96px}
    .intent-go{width:28px;height:28px;border-radius:99px;background:var(--sent-bg);color:#fff;
      display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-bottom:2px}
    .intent-go:disabled{background:#C7C7CC;cursor:not-allowed}
    /* Real feedback messages run several paragraphs, so the sheet gets enough
       room to show one in full and scrolls for the rest. */
    .drafts-list{display:flex;flex-direction:column;align-items:flex-start;gap:6px;
      max-height:46vh;overflow-y:auto}
    /* Same measure as a message bubble, so a draft reads as the message it is
       about to become rather than as a banner across the column. */
    .draft-row{display:flex;align-items:flex-end;gap:6px;max-width:100%}
    /* A div, not a button: space is a button's activation key, so a <button>
       can't take a typed space once it's contenteditable. */
    .draft{background:var(--recv-bg);color:var(--recv-fg);border-radius:20px;cursor:pointer;
      padding:9px 16px;font-size:16px;line-height:1.3;letter-spacing:-.01em;text-align:left;
      white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;max-width:min(100%,480px);
      transition:transform .14s cubic-bezier(.32,.72,0,1),background .14s ease}
    .draft:focus-visible{outline:2px solid #007AFF;outline-offset:1px}
    .draft:active{transform:scale(.98);background:#DEDEE1}
    /* Editing a draft in place — same blue outline as an admin silent edit. */
    .draft.editing{outline:2px solid #007AFF;outline-offset:1px;cursor:text;background:var(--recv-bg);
      -webkit-user-select:text;user-select:text}
    .draft.editing:active{transform:none}
    .draft-edit{width:26px;height:26px;border-radius:99px;background:var(--recv-bg);color:#3C3C43;
      display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-bottom:3px}
    .draft-row.editing .draft-edit{background:#007AFF;color:#fff}
    .draft-note{font-size:11px;color:var(--muted);padding:2px 4px}
    .draft-note.err{color:#991B1B}
    .draft-load{display:inline-flex;align-items:center;gap:5px;background:var(--recv-bg);
      border-radius:20px;padding:12px 16px}
    .draft-load .dot{width:7px;height:7px;background:var(--muted);border-radius:99px;animation:bob 1.2s infinite}
    .draft-load .dot:nth-child(2){animation-delay:.15s}
    .draft-load .dot:nth-child(3){animation-delay:.3s}

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
      .attach-btn,.ai-btn{width:34px;height:34px}
      .attach-btn svg,.ai-btn svg{width:18px;height:18px}
      .composer-input{padding:5px 5px 5px 14px;min-height:34px}
      .editable{font-size:15px}
      .send-btn{width:26px;height:26px}
      .drafts{padding:8px 10px 0}
      .draft{font-size:15px;padding:8px 14px}
      .banner{padding:9px 12px;font-size:12px}
      .nextstep{padding:8px 10px 3px;gap:8px}
      .nextstep-main{padding:9px 12px;gap:9px}
      .nextstep-label{font-size:13.5px}
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
        <div class="header-sub" id="headerSub">3 people &middot; {{ space.campaign_name or space.brand_name or 'Campaign' }}</div>
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
    <!-- An approved review closes the composer for the creator and the brand.
         The next step outlives that: posting the live links is the whole
         point of the approval, so the strip becomes the bar's only content
         rather than the chat dead-ending on a disabled input. -->
    <div class="composer{% if space.status != 'active' %} no-input{% endif %}">
      {% if is_admin and ai_drafts_enabled %}
      <div class="drafts" id="drafts" aria-live="polite"></div>
      {% endif %}
      <div class="nextstep hidden" id="nextStep" aria-live="polite"></div>
      <div class="composer-row">
        <input type="file" id="fileInput" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none">
        <button type="button" class="attach-btn" id="fileBtn" title="Attach image" {% if space.status != 'active' %}disabled{% endif %}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><path d="M21 15l-5-5L5 21"></path></svg>
        </button>
        {% if is_admin and ai_drafts_enabled %}
        <button type="button" class="ai-btn" id="aiBtn" title="Draft with AI" aria-label="Draft with AI" {% if space.status != 'active' %}disabled{% endif %}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.6l1.5 4.4 4.4 1.5-4.4 1.5L12 14.4l-1.5-4.4L6.1 8.5l4.4-1.5L12 2.6z"></path><path d="M18.4 13.6l.85 2.45 2.45.85-2.45.85-.85 2.45-.85-2.45-2.45-.85 2.45-.85.85-2.45z"></path><path d="M6.2 14.2l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7.7-2z"></path></svg>
        </button>
        {% endif %}
        <div class="composer-input">
          <div class="composer-typing" id="composerTyping" aria-hidden="true"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
          <div class="editable" id="bodyInput" contenteditable="{{ 'false' if space.status != 'active' else 'true' }}"
               data-empty="true" data-placeholder="{{ 'Message as Influence' if is_admin else 'Message' }}" role="textbox" aria-label="Message"></div>
          <button type="button" class="send-btn" id="sendBtn" title="Send" {% if space.status != 'active' %}disabled{% endif %}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg>
          </button>
        </div>
      </div>
      <div class="notify-hint">{% if is_admin %}Posting as Influence — @{{ space.creator_username }} and {{ space.brand_name or 'the brand' }} will be notified · double-click a message to edit it silently{% if ai_drafts_enabled %} · ✨ to draft with AI — say what you want to get across{% endif %}{% elif self_party == 'brand' %}@{{ space.creator_username }} and Jennifer will be notified{% else %}{{ space.brand_name or 'The brand' }} and Jennifer will be notified{% endif %}</div>
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
<!-- `default(none)` so a caller that renders this page without resolving a
     step (tests, any future embed) gets "no step" rather than a hard failure
     on an undefined variable. -->
<script id="initial-next-step" type="application/json">{{ next_step | default(none) | tojson }}</script>
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
  var aiBtn = document.getElementById('aiBtn');       // admin + AI configured only
  var draftsEl = document.getElementById('drafts');
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
  // Play triangle. The points are chosen so the triangle's centre of mass sits
  // on the viewBox centre (12,12) — a bounding-box-centred triangle reads as
  // left-heavy, which is why play glyphs are usually nudged right by hand. The
  // rounded corners come from a stroked, round-joined outline, so the shape
  // stays symmetric instead of drifting the way hand-placed arcs do.
  var PLAY = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"><path d="M9 6.6 L18.6 12 L9 17.4 Z"></path></svg>';
  var TICK = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
  var PENCIL = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>';
  var LINKICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"></path><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"></path></svg>';
  var CHEV = '<svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>';
  var UPLOAD = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>';

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
  // The preview image is fetched from our own origin (the server resolves the
  // link and proxies the thumbnail), so nothing here depends on the video host
  // allowing hotlinks. `data-src` rather than `src` so hydrateThumbs can watch
  // the load: only a thumbnail that actually arrives is revealed, and a link
  // with no preview silently keeps the placeholder artwork.
  function thumbHtml(m, link){
    if(!link) return '';
    var src = withAs('/chat/' + spaceSlug + '/link-preview/' + m.id);
    return '<img class="rcard-thumb-bg" alt="" aria-hidden="true" decoding="async">' +
           '<img class="rcard-thumb" alt="" data-src="' + escapeHtml(src) + '" decoding="async">';
  }
  function reviewCardHtml(m){
    var ev = m.event || {};
    var n = draftNumber(ev);
    var link = isHttpUrl(ev.video_link) ? String(ev.video_link).trim() : '';
    var title = n > 1 ? 'Revised draft submitted' : 'New draft submitted';
    var sub = link ? (sourceLabel(hostOf(link)) + ' · Tap to watch') : 'No link attached';
    var inner =
      '<div class="rcard-media">' +
        thumbHtml(m, link) +
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

  // Kick off the preview fetch for any draft card just added to the feed.
  // The image is only faded in once it decodes; if the server has no preview
  // for that link (404) the <img>s are dropped and the card keeps its
  // placeholder, so a broken-image icon never appears in the conversation.
  function hydrateThumbs(root){
    var imgs = root.querySelectorAll('img.rcard-thumb[data-src]');
    for(var i=0;i<imgs.length;i++){
      (function(img){
        var src = img.getAttribute('data-src');
        img.removeAttribute('data-src');
        var media = img.parentNode;
        var bg = media ? media.querySelector('.rcard-thumb-bg') : null;
        if(!src){ if(bg) bg.remove(); img.remove(); return; }
        img.addEventListener('load', function(){
          if(media) media.classList.add('has-thumb');
        });
        img.addEventListener('error', function(){
          if(bg) bg.remove();
          img.remove();
        });
        if(bg) bg.setAttribute('src', src);
        img.setAttribute('src', src);
      })(imgs[i]);
    }
  }

  // ── Next step ─────────────────────────────────────────────────────────
  // Whatever it is the creator's turn to do, resolved server-side from the
  // state of the campaign's drafts. At most one exists at a time, so there
  // is never a menu of destinations to choose between — see
  // services/creator_next_step.py.
  var nextStep = null;
  try{
    nextStep = JSON.parse(document.getElementById('initial-next-step').textContent || 'null');
  }catch(e){ nextStep = null; }
  var nextStepEl = document.getElementById('nextStep');
  var composerEl = document.querySelector('.composer');
  var headerSub = document.getElementById('headerSub');
  var headerSubDefault = headerSub ? headerSub.textContent : '';
  // Only the creator can act on a step. The admin gets a read-only mirror so
  // the team can see what the creator is looking at; the brand gets nothing,
  // since it's never their move.
  var canAct = selfParty === 'creator';
  var showsStep = canAct || isAdmin;

  function stepUrl(step){
    return '/chat/' + spaceSlug + '/go/' + encodeURIComponent(step.route);
  }
  // The strip is a nudge, not a blocker: someone who's read it and wants to
  // get on with the conversation can put it away. Deliberately held in
  // memory only, so opening the chat again shows it — the step is still
  // open, and nothing about dismissing it made that less true. The action
  // itself is never lost either way: it stays in the feed under the notice
  // that opened it.
  var dismissed = null;
  function stepId(step){
    return step.key + ':' + (step.review_id || 0);
  }
  function isDismissed(step){
    return dismissed === stepId(step);
  }
  function dismiss(step){
    dismissed = stepId(step);
  }

  function renderNextStep(){
    if(!nextStepEl) return;
    var step = nextStep;
    var show = !!step && showsStep && !(canAct && isDismissed(step));
    if(composerEl) composerEl.classList.toggle('has-step', show);
    if(!show){
      nextStepEl.classList.add('hidden');
      nextStepEl.innerHTML = '';
      if(headerSub) headerSub.textContent = headerSubDefault;
      return;
    }
    // The header sub-line becomes live status rather than a second control:
    // it says where things stand, and tapping it scrolls to the action.
    if(headerSub) headerSub.textContent = canAct ? step.detail : ('Creator: ' + step.label);
    if(canAct){
      nextStepEl.innerHTML =
        '<a class="nextstep-main" href="' + escapeHtml(stepUrl(step)) + '">' +
          '<span class="nextstep-icon">' + (step.key === 'submit_posts' ? LINKICON : UPLOAD) + '</span>' +
          '<span class="nextstep-text">' +
            '<span class="nextstep-label">' + escapeHtml(step.label) + '</span>' +
            '<span class="nextstep-detail">' + escapeHtml(step.detail) + '</span>' +
          '</span>' +
          '<span class="nextstep-go">' + CHEV + '</span>' +
        '</a>' +
        '<button type="button" class="nextstep-x" id="nextStepX" title="Hide" aria-label="Hide">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
        '</button>';
      var x = document.getElementById('nextStepX');
      if(x) x.addEventListener('click', function(){ dismiss(step); renderNextStep(); });
    }else{
      nextStepEl.innerHTML =
        '<div class="nextstep-main" style="background:var(--recv-bg);color:#3C3C43">' +
          '<span class="nextstep-icon" style="background:rgba(0,0,0,.06)">' +
            (step.key === 'submit_posts' ? LINKICON : UPLOAD) + '</span>' +
          '<span class="nextstep-text">' +
            '<span class="nextstep-label">Waiting on @' + escapeHtml(creatorUsername) + '</span>' +
            '<span class="nextstep-detail" style="color:var(--muted)">' + escapeHtml(step.label) + '</span>' +
          '</span>' +
        '</div>';
    }
    nextStepEl.classList.remove('hidden');
  }

  // Replace the step and re-render everything that shows it. The inline
  // action under a decision is anchored to a specific draft, so a step that
  // has moved on takes its action row with it.
  function setNextStep(step){
    var before = nextStep ? (nextStep.key + ':' + nextStep.review_id) : '';
    var after = step ? (step.key + ':' + step.review_id) : '';
    nextStep = step || null;
    renderNextStep();
    if(before !== after) refreshInlineActions();
  }

  // The action row that hangs under the system notice which opened the step.
  // It only renders on the event for the draft the step actually answers, so
  // the same action never repeats down a long history.
  function inlineActionHtml(m){
    if(!nextStep || !showsStep) return '';
    var ev = m.event || {};
    if(!nextStep.review_id || ev.review_id !== nextStep.review_id) return '';
    if(!canAct){
      return '<div class="sys-act mirror"><span>' + escapeHtml(nextStep.label) + '</span></div>';
    }
    return '<div class="sys-act"><a href="' + escapeHtml(stepUrl(nextStep)) + '">' +
      escapeHtml(nextStep.label) + CHEV + '</a></div>';
  }

  function refreshInlineActions(){
    var rows = messagesEl.querySelectorAll('.row.sys-row[data-kind="review_decision"]');
    for(var i=0;i<rows.length;i++){
      var row = rows[i];
      var existing = row.querySelector('.sys-act');
      if(existing) existing.remove();
      var reviewId = parseInt(row.getAttribute('data-review'), 10);
      if(nextStep && showsStep && nextStep.review_id && reviewId === nextStep.review_id){
        row.insertAdjacentHTML('beforeend', inlineActionHtml({ event: { review_id: reviewId } }));
        var x = row.querySelector('.sys-act a');
        if(x) x.setAttribute('href', stepUrl(nextStep));
      }
    }
  }

  // `review_decision` — a centred system line, the way iMessage announces
  // something that happened to the conversation rather than in it. When the
  // decision opened the creator's current step, it carries that step: an
  // approval that says "post it" is not the end of the story.
  function decisionHtml(m){
    var ev = m.event || {};
    var approved = ev.decision === 'approved';
    var text = 'Draft ' + draftNumber(ev) +
      (approved ? ' approved' : ' — changes requested');
    return '<div class="sys ' + (approved ? 'ok' : 'chg') + '">' +
      '<span class="sys-dot">' + (approved ? TICK : PENCIL) + '</span>' +
      '<span>' + escapeHtml(text) + '</span></div>' +
      inlineActionHtml(m);
  }

  // `posts_submitted` — the creator shared their live links. The other half
  // of an approval, and what retires the "add your post links" step.
  function postsSubmittedHtml(m){
    var ev = m.event || {};
    var plats = (ev.platforms || []).map(function(p){
      return p.charAt(0).toUpperCase() + p.slice(1);
    });
    var text = 'Live post links added' + (plats.length ? ' · ' + plats.join(' · ') : '');
    return '<div class="sys post">' +
      '<span class="sys-dot">' + LINKICON + '</span>' +
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
    if(m.kind === 'review_decision' || m.kind === 'posts_submitted'){
      row.className = 'row sys-row';
      row.dataset.kind = m.kind;
      // Lets refreshInlineActions() find the decision a step belongs to
      // without re-reading the whole feed.
      if((m.event || {}).review_id) row.dataset.review = (m.event || {}).review_id;
      row.innerHTML = m.kind === 'review_decision' ? decisionHtml(m) : postsSubmittedHtml(m);
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
    hydrateThumbs(row);
    prevAppend = {party:m.party, sender:(m.sender || ''), mine:mine, day:dk};
    if(m.id > lastId) lastId = m.id;
    updateReceipts();
  }

  function pageHeight(){
    return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  }
  function nearBottom(){
    return (window.innerHeight + window.scrollY) >= (pageHeight() - 160);
  }
  function scrollToBottom(){ window.scrollTo(0, pageHeight()); }

  // ── Landing on the newest message ─────────────────────────────────────
  // Scrolling once when the messages arrive isn't enough. An attachment has
  // no height until it decodes, so the feed keeps growing *after* that
  // scroll and leaves the reader parked above the latest message — on a
  // conversation with a couple of photos in it, a whole screen above.
  //
  // So hold the view at the bottom while the page settles, re-pinning on
  // every layout change, and let go the moment the reader scrolls for
  // themselves. After that the usual nearBottom() stickiness takes over.
  var bottomPin = null;
  var PIN_SETTLE_MS = 4000;
  // Scrolling programmatically fires `scroll` too, so watch for the input
  // that means a *person* is scrolling rather than the event itself.
  var PIN_RELEASE_EVENTS = ['wheel', 'touchstart', 'keydown', 'mousedown'];

  function keepAtBottom(){ if(bottomPin) scrollToBottom(); }

  function releaseBottomPin(){
    if(!bottomPin) return;
    if(bottomPin.observer) bottomPin.observer.disconnect();
    clearTimeout(bottomPin.timer);
    for(var i=0;i<PIN_RELEASE_EVENTS.length;i++){
      window.removeEventListener(PIN_RELEASE_EVENTS[i], releaseBottomPin);
    }
    bottomPin = null;
  }

  function pinToBottom(){
    releaseBottomPin();
    bottomPin = {};
    scrollToBottom();
    if(typeof ResizeObserver !== 'undefined'){
      // Fires as each image lands and the feed grows. Re-pinning doesn't
      // change any element's size, so this can't feed itself.
      bottomPin.observer = new ResizeObserver(keepAtBottom);
      bottomPin.observer.observe(document.body);
    }
    for(var i=0;i<PIN_RELEASE_EVENTS.length;i++){
      window.addEventListener(PIN_RELEASE_EVENTS[i], releaseBottomPin, {passive:true});
    }
    bottomPin.timer = setTimeout(releaseBottomPin, PIN_SETTLE_MS);
  }

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
      // An approval, or the creator's own post links landing, changes what
      // it's their turn to do. The server sends the current step alongside
      // any new messages so the strip follows without a reload.
      if(Object.prototype.hasOwnProperty.call(data, 'next_step')) setNextStep(data.next_step);
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
        // A decision or a posts notice may have just changed whose move it
        // is. Poll for the step rather than guessing at it here — the
        // request comes back empty-handed on messages and cheap.
        if(m.kind === 'review_decision' || m.kind === 'posts_submitted') backfill();
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

  // Write text into the composer, keeping its line breaks. The composer is a
  // contenteditable div, so newlines have to become <br> — assigning them as
  // plain text would let the browser collapse a two-line draft into one line.
  function setComposerText(text){
    var parts = String(text || '').split('\\n');
    var html = '';
    for(var i=0;i<parts.length;i++){ html += (i ? '<br>' : '') + escapeHtml(parts[i]); }
    editable.innerHTML = html;
    updateEmptyState();
    editable.focus();
    try{
      var range = document.createRange(); range.selectNodeContents(editable); range.collapse(false);
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }catch(e){}
  }

  // ── AI drafts (admin) ──────────────────────────────────────────────────
  // Two ways in: say what you want to get across in the intent field and let
  // it write that, or leave the field empty and get replies read off the
  // conversation. Either way the drafts are editable in place before use, and
  // nothing is ever sent for you — picking one fills the composer.
  //
  // The sheet's shell (head + intent field + list) is built once and only the
  // list is re-rendered, so regenerating never wipes what you typed.
  if(isAdmin && aiBtn && draftsEl){
    var draftsOpen = false, draftsBusy = false, lastDrafts = [];
    var intentEl = null, goBtn = null, listEl = null;

    var SPARK = '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.6l1.5 4.4 4.4 1.5-4.4 1.5L12 14.4l-1.5-4.4L6.1 8.5l4.4-1.5L12 2.6z"></path><path d="M18.4 13.6l.85 2.45 2.45.85-2.45.85-.85 2.45-.85-2.45-2.45-.85 2.45-.85.85-2.45z"></path></svg>';
    var PENCIL_BTN = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>';
    var CHECK_BTN = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';

    function setEmpty(el){
      el.setAttribute('data-empty', (el.innerText || '').trim() ? 'false' : 'true');
    }
    function intentText(){
      if(!intentEl) return '';
      return (intentEl.innerText || '').replace(/\\r\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
    }
    function setBusy(on){
      draftsBusy = on;
      if(goBtn) goBtn.disabled = on;
    }
    function setList(html){
      if(listEl){ listEl.innerHTML = html; pinToBottom(); }
    }

    function openDrafts(){
      if(draftsOpen) return;
      draftsOpen = true;
      aiBtn.classList.add('on');
      draftsEl.classList.add('on');
      draftsEl.innerHTML =
        '<div class="drafts-head"><span>Draft with AI</span><span class="sp"></span>' +
          '<button type="button" data-act="close">Dismiss</button></div>' +
        '<div class="intent">' +
          '<div class="editable" id="intentInput" contenteditable="true" role="textbox" ' +
            'data-empty="true" aria-label="What do you want to say?" ' +
            'data-placeholder="What do you want to say?"></div>' +
          '<button type="button" class="intent-go" data-act="draft" title="Draft" ' +
            'aria-label="Draft">' + SPARK + '</button>' +
        '</div>' +
        '<div class="drafts-list" id="draftsList"></div>';
      intentEl = document.getElementById('intentInput');
      goBtn = draftsEl.querySelector('.intent-go');
      listEl = document.getElementById('draftsList');
      // Carry over anything already typed in the composer — it's usually the
      // note you were about to turn into a message anyway.
      var carried = getBody();
      if(carried) intentEl.textContent = carried;
      setEmpty(intentEl);
      intentEl.addEventListener('input', function(){ setEmpty(intentEl); });
      intentEl.addEventListener('keydown', function(e){
        if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); requestDrafts(); }
      });
      intentEl.addEventListener('paste', function(e){
        e.preventDefault();
        var t = (e.clipboardData || window.clipboardData).getData('text');
        document.execCommand('insertText', false, t);
      });
      intentEl.focus();
      pinToBottom();
    }
    function closeDrafts(){
      draftsOpen = false;
      draftsEl.classList.remove('on');
      draftsEl.innerHTML = '';
      intentEl = goBtn = listEl = null;
      aiBtn.classList.remove('on');
    }

    function loadingHtml(){
      return '<div class="draft-load">' +
        '<span class="dot"></span><span class="dot"></span><span class="dot"></span></div>';
    }
    function errorHtml(message){
      return '<div class="draft-note err">' +
        escapeHtml(message || 'Couldn\\'t draft a reply. Try again.') + '</div>';
    }
    // A full feedback reply runs several paragraphs, so the ones below the
    // fold need announcing.
    function draftsHtml(list){
      var html = '<div class="draft-note">' + list.length +
        (list.length === 1 ? ' draft' : ' drafts') + ' — tap to use, ✎ to edit</div>';
      for(var i=0;i<list.length;i++){
        html += '<div class="draft-row" data-i="' + i + '">' +
          '<div class="draft" role="button" tabindex="0">' + escapeHtml(list[i]) + '</div>' +
          '<button type="button" class="draft-edit" title="Edit" aria-label="Edit draft">' +
            PENCIL_BTN + '</button>' +
        '</div>';
      }
      return html;
    }

    // ── Edit a draft in place ──
    // Same contenteditable trick as an admin silent edit, so the gesture is
    // one the admin already knows. Enter inserts a line break (these are
    // multi-paragraph messages); the ✓ button or Esc commits.
    function isEditing(row){ return row.classList.contains('editing'); }
    function beginDraftEdit(row){
      if(isEditing(row)) return;
      var bubble = row.querySelector('.draft');
      var btn = row.querySelector('.draft-edit');
      row.classList.add('editing');
      bubble.classList.add('editing');
      bubble.setAttribute('contenteditable', 'true');
      bubble.setAttribute('role', 'textbox');
      btn.innerHTML = CHECK_BTN;
      btn.title = 'Done';
      btn.setAttribute('aria-label', 'Done editing');
      bubble.focus();
      try{
        var range = document.createRange(); range.selectNodeContents(bubble); range.collapse(false);
        var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      }catch(e){}
    }
    function endDraftEdit(row){
      if(!isEditing(row)) return;
      var i = parseInt(row.dataset.i, 10);
      var bubble = row.querySelector('.draft');
      var btn = row.querySelector('.draft-edit');
      var next = (bubble.innerText || '').replace(/\\r\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
      if(next) lastDrafts[i] = next;
      bubble.textContent = lastDrafts[i];   // back to plain text; pre-wrap keeps the breaks
      bubble.removeAttribute('contenteditable');
      bubble.setAttribute('role', 'button');
      bubble.classList.remove('editing');
      row.classList.remove('editing');
      btn.innerHTML = PENCIL_BTN;
      btn.title = 'Edit';
      btn.setAttribute('aria-label', 'Edit draft');
    }
    function editingRowIn(){ return draftsEl.querySelector('.draft-row.editing'); }

    async function requestDrafts(){
      if(archived || draftsBusy) return;
      var instruction = intentText();
      setBusy(true);
      setList(loadingHtml());
      try{
        var r = await fetch(withAs('/chat/' + spaceSlug + '/ai-draft'), {
          method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({instruction: instruction}),
        });
        var data = null;
        try{ data = await r.json(); }catch(e){}
        if(!draftsOpen) return;                       // dismissed while in flight
        if(r.ok && data && data.drafts && data.drafts.length){
          lastDrafts = data.drafts;
          setList(draftsHtml(lastDrafts));
        } else {
          setList(errorHtml(data && data.message));
        }
      }catch(e){
        if(draftsOpen) setList(errorHtml('Network error. Try again.'));
      }finally{ setBusy(false); }
    }

    aiBtn.addEventListener('click', function(){
      if(draftsOpen) closeDrafts(); else openDrafts();
    });
    // Keep focus where it is when the edit button is pressed, so committing on
    // blur doesn't fight the click that asked to commit.
    draftsEl.addEventListener('mousedown', function(e){
      if(e.target.closest('.draft-edit')) e.preventDefault();
    });
    draftsEl.addEventListener('click', function(e){
      var act = e.target.closest('[data-act]');
      if(act){
        if(act.dataset.act === 'close') closeDrafts(); else requestDrafts();
        return;
      }
      var row = e.target.closest('.draft-row');
      if(!row) return;
      if(e.target.closest('.draft-edit')){
        if(isEditing(row)) endDraftEdit(row); else beginDraftEdit(row);
        return;
      }
      if(!e.target.closest('.draft') || isEditing(row)) return;  // caret, not a pick
      var text = lastDrafts[parseInt(row.dataset.i, 10)];
      if(text == null) return;
      setComposerText(text);
      closeDrafts();
    });
    draftsEl.addEventListener('dblclick', function(e){
      var row = e.target.closest('.draft-row');
      if(row && e.target.closest('.draft')) beginDraftEdit(row);
    });
    // The bubble is a div, so Enter-to-use is ours to wire. While editing,
    // Enter belongs to the text — these messages are multi-paragraph.
    draftsEl.addEventListener('keydown', function(e){
      if(e.key !== 'Enter') return;
      var row = e.target.closest && e.target.closest('.draft-row');
      if(!row || isEditing(row) || !e.target.closest('.draft')) return;
      e.preventDefault();
      var text = lastDrafts[parseInt(row.dataset.i, 10)];
      if(text == null) return;
      setComposerText(text);
      closeDrafts();
    });
    draftsEl.addEventListener('focusout', function(e){
      var row = e.target.closest && e.target.closest('.draft-row');
      if(row && isEditing(row)) endDraftEdit(row);
    });
    document.addEventListener('keydown', function(e){
      if(e.key !== 'Escape' || !draftsOpen) return;
      var row = editingRowIn();
      if(row) endDraftEdit(row); else closeDrafts();
    });
  }

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

  // Tapping the live header status scrolls to the action rather than being a
  // second way to trigger it — the header stays status, never a control.
  if(headerSub){
    headerSub.addEventListener('click', function(){
      if(!nextStep || !showsStep) return;
      var anchor = nextStepEl && !nextStepEl.classList.contains('hidden')
        ? nextStepEl : messagesEl.querySelector('.sys-act');
      if(anchor) anchor.scrollIntoView({behavior:'smooth', block:'center'});
    });
  }

  renderNextStep();
  updateEmptyState();
  backfill().then(function(){
    initialLoaded = true;
    if(!lastId) messagesEl.innerHTML = '<div class="empty">No messages yet — start the conversation 👋</div>';
    pinToBottom();
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
