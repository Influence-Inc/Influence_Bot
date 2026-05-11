"""
HTTP routes for creator <-> brand chat spaces.

Mounted on the existing Flask app in app.py via `register_chat_routes`.

Routes:
  GET  /chat/invite/<token>           — magic link landing; mints a session cookie
  GET  /chat/<space_id>               — chat page (Jinja)
  GET  /chat/<space_id>/messages      — poll for new messages (?since=<id>)
  POST /chat/<space_id>/messages      — submit message (+ optional attachment)
  POST /chat/<space_id>/messages/<id>/react — toggle a reaction
  POST /chat/<space_id>/read          — mark messages read up to id
  GET  /chat/attachment/<id>          — serve a stored attachment

  GET  /admin/chats                   — admin list (token-gated)
  POST /admin/chats/login             — exchange admin token for session
  GET  /admin/chats/<space_id>        — admin read-only chat view
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    make_response,
    render_template_string,
    request,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import Config
from services import chat_service
from services.chat_service import (
    archive_for_campaign,
    archive_space,
    find_by_id,
)
from templates.chat_pages import (
    ADMIN_CHAT_PAGE,
    ADMIN_DASHBOARD,
    ADMIN_LOGIN_PAGE,
    CHAT_PAGE,
    ERROR_PAGE,
)
from utils.chat_tokens import (
    SESSION_COOKIE,
    create_session,
    load_session,
    read_invite_token,
)

logger = logging.getLogger(__name__)


bp = Blueprint("chat", __name__)

ADMIN_COOKIE = "influence_chat_admin"
_ADMIN_SALT = "influence-chat-admin-v1"


def _admin_serializer() -> URLSafeTimedSerializer:
    secret = Config.CHAT_SECRET_KEY
    if not secret:
        raise RuntimeError("CHAT_SECRET_KEY must be set for admin sessions.")
    return URLSafeTimedSerializer(secret_key=secret, salt=_ADMIN_SALT)


def _is_admin() -> bool:
    cookie = request.cookies.get(ADMIN_COOKIE)
    if not cookie:
        return False
    try:
        _admin_serializer().loads(cookie, max_age=Config.CHAT_SESSION_TTL)
        return True
    except (BadSignature, SignatureExpired):
        return False
    except Exception:
        return False


def _error_response(heading: str, message: str, status: int = 400) -> Response:
    html = render_template_string(ERROR_PAGE, heading=heading, message=message)
    return Response(html, status=status, mimetype="text/html")


def _require_session_for_space(space_id: int):
    """Returns (session_obj_or_None, error_response_or_None)."""
    cookie = request.cookies.get(SESSION_COOKIE)
    sess = load_session(cookie)
    if sess is None or sess.chat_space_id != space_id:
        return None, _error_response(
            "Sign-in required",
            "This chat link has expired or is invalid. Please open the most recent "
            "link from your email or Slack.",
            status=401,
        )
    return sess, None


# ---------------------------------------------------------------------------
# Magic-link entry
# ---------------------------------------------------------------------------

@bp.route("/chat/invite/<token>", methods=["GET"])
def chat_invite(token: str):
    payload = read_invite_token(token)
    if not payload:
        return _error_response(
            "Link expired",
            "This chat invitation has expired or is no longer valid. Ask your "
            "INFLUENCE contact for a fresh link.",
            status=400,
        )

    chat_space_id = payload.get("sid")
    party = payload.get("p")
    identifier = payload.get("i")
    space = find_by_id(chat_space_id) if chat_space_id else None
    if not space:
        return _error_response("Chat not found", "This chat space no longer exists.", status=404)

    if not identifier:
        # Brand workspace-wide invite — derive a stable identifier from the
        # chat space + the browser's session signature so messages from the
        # same browser group together.
        identifier = chat_service.brand_identifier_for(space)

    display_name = None
    if party == "creator":
        display_name = space.creator_username
    elif party == "brand":
        display_name = f"{space.brand_name or 'Brand'} Team"

    chat_service.upsert_member(
        chat_space_id=space.id,
        party=party,
        identifier=identifier,
        display_name=display_name,
    )
    _sess, cookie_value = create_session(
        chat_space_id=space.id,
        party=party,
        identifier=identifier,
        display_name=display_name,
    )

    resp = make_response(
        "", 302, {"Location": f"/chat/{space.id}"}
    )
    resp.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        max_age=Config.CHAT_SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return resp


# ---------------------------------------------------------------------------
# Chat page
# ---------------------------------------------------------------------------

@bp.route("/chat/<int:space_id>", methods=["GET"])
def chat_page(space_id: int):
    sess, err = _require_session_for_space(space_id)
    if err is not None:
        return err
    space = find_by_id(space_id)
    if space is None:
        return _error_response("Chat not found", "This chat space no longer exists.", status=404)

    chat_title = f"Chat with {space.brand_name or 'the brand'}" if sess.party == "creator" \
        else f"Chat with @{space.creator_username}"

    html = render_template_string(
        CHAT_PAGE,
        space=space,
        self_party=sess.party,
        chat_title=chat_title,
    )
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------------
# Messages (poll + post)
# ---------------------------------------------------------------------------

@bp.route("/chat/<int:space_id>/messages", methods=["GET"])
def chat_messages_poll(space_id: int):
    sess, err = _require_session_for_space(space_id)
    if err is not None:
        return err
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    msgs = chat_service.list_messages(chat_space_id=space_id, since_id=since)
    return jsonify({"messages": msgs})


# In-memory per-session rate limit: 30 messages / 60s.
_RATE_BUCKET: dict[int, list[float]] = {}
_RATE_LIMIT = 30
_RATE_WINDOW = 60.0


def _rate_limited(session_id: int) -> bool:
    now = time.time()
    bucket = _RATE_BUCKET.setdefault(session_id, [])
    cutoff = now - _RATE_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _RATE_LIMIT:
        return True
    bucket.append(now)
    return False


@bp.route("/chat/<int:space_id>/messages", methods=["POST"])
def chat_messages_post(space_id: int):
    sess, err = _require_session_for_space(space_id)
    if err is not None:
        return err
    if _rate_limited(sess.id):
        return jsonify({"error": "rate_limited"}), 429

    space = find_by_id(space_id)
    if space is None or space.status != "active":
        return jsonify({"error": "chat_closed"}), 410

    body = (request.form.get("body") or "").strip()
    file = request.files.get("attachment")

    if not body and not file:
        return jsonify({"error": "empty"}), 400

    msg = chat_service.post_message(
        chat_space_id=space_id,
        sender_party=sess.party,
        sender_identifier=sess.identifier,
        sender_display_name=sess.display_name,
        body=body or " ",
    )
    if msg is None:
        return jsonify({"error": "failed"}), 500

    if file and file.filename:
        data = file.read()
        chat_service.store_attachment(
            message_id=msg.id,
            filename=file.filename,
            content_type=(file.mimetype or "application/octet-stream").lower(),
            data=data,
        )

    # Notify the other side out-of-band (Slack/email), best-effort.
    try:
        from services.chat_notifications import notify_new_message
        notify_new_message(chat_space_id=space_id, sender_party=sess.party, message_id=msg.id)
    except Exception as exc:
        logger.warning("chat notification dispatch failed: %s", exc)

    return jsonify({"ok": True, "id": msg.id})


@bp.route("/chat/<int:space_id>/messages/<int:message_id>/react", methods=["POST"])
def chat_message_react(space_id: int, message_id: int):
    sess, err = _require_session_for_space(space_id)
    if err is not None:
        return err
    body = request.get_json(silent=True) or {}
    emoji = (body.get("emoji") or "").strip()
    if not emoji:
        return jsonify({"error": "empty"}), 400
    now_present = chat_service.toggle_reaction(
        message_id=message_id,
        party=sess.party,
        identifier=sess.identifier,
        emoji=emoji,
    )
    return jsonify({"ok": True, "active": now_present})


@bp.route("/chat/<int:space_id>/read", methods=["POST"])
def chat_messages_read(space_id: int):
    sess, err = _require_session_for_space(space_id)
    if err is not None:
        return err
    body = request.get_json(silent=True) or {}
    try:
        up_to = int(body.get("up_to") or 0)
    except (TypeError, ValueError):
        up_to = 0
    if up_to:
        chat_service.mark_read(
            chat_space_id=space_id,
            party=sess.party,
            identifier=sess.identifier,
            up_to_message_id=up_to,
        )
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@bp.route("/chat/attachment/<int:attachment_id>", methods=["GET"])
def chat_attachment(attachment_id: int):
    admin_view = request.args.get("admin") == "1"
    att = chat_service.find_attachment(attachment_id)
    if att is None:
        return _error_response("Not found", "Attachment not found.", status=404)

    # Authorize: either the requester has a session on the parent chat
    # space, or they're an authenticated admin.
    from models.models import ChatMessage, SessionLocal
    db = SessionLocal()
    try:
        msg = db.query(ChatMessage).get(att.message_id)
    finally:
        db.close()
    if msg is None:
        return _error_response("Not found", "Attachment not found.", status=404)

    if admin_view and _is_admin():
        authorized = True
    else:
        sess = load_session(request.cookies.get(SESSION_COOKIE))
        authorized = sess is not None and sess.chat_space_id == msg.chat_space_id

    if not authorized:
        return _error_response("Forbidden", "You don't have access to this file.", status=403)

    try:
        with open(att.storage_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return _error_response("Not found", "Attachment is no longer available.", status=404)

    return Response(
        data,
        mimetype=att.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{att.filename}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@bp.route("/admin/chats", methods=["GET"])
def admin_chats_list():
    if not _is_admin():
        html = render_template_string(ADMIN_LOGIN_PAGE, error=None)
        return Response(html, mimetype="text/html")

    q = (request.args.get("q") or "").strip() or None
    status = (request.args.get("status") or "").strip() or None
    spaces = chat_service.list_spaces_for_admin(status=status, search=q)
    stats = chat_service.admin_stats()
    html = render_template_string(
        ADMIN_DASHBOARD,
        spaces=spaces,
        stats=stats,
        query={"q": q, "status": status},
    )
    return Response(html, mimetype="text/html")


@bp.route("/admin/chats/login", methods=["POST"])
def admin_chats_login():
    token = (request.form.get("token") or "").strip()
    expected = Config.CHAT_ADMIN_TOKEN
    if not expected:
        html = render_template_string(
            ADMIN_LOGIN_PAGE,
            error="CHAT_ADMIN_TOKEN is not configured on the server.",
        )
        return Response(html, mimetype="text/html", status=503)
    if not token or token != expected:
        html = render_template_string(ADMIN_LOGIN_PAGE, error="Invalid token.")
        return Response(html, mimetype="text/html", status=401)
    cookie_value = _admin_serializer().dumps({"a": 1, "iat": int(time.time())})
    resp = make_response("", 302, {"Location": "/admin/chats"})
    resp.set_cookie(
        ADMIN_COOKIE, cookie_value,
        max_age=Config.CHAT_SESSION_TTL,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


@bp.route("/admin/chats/<int:space_id>", methods=["GET"])
def admin_chat_view(space_id: int):
    if not _is_admin():
        return Response(
            render_template_string(ADMIN_LOGIN_PAGE, error=None),
            mimetype="text/html",
        )
    space = find_by_id(space_id)
    if space is None:
        return _error_response("Not found", "Chat space not found.", status=404)
    messages = chat_service.list_messages(chat_space_id=space_id, limit=2000)
    html = render_template_string(ADMIN_CHAT_PAGE, space=space, messages=messages)
    return Response(html, mimetype="text/html")


@bp.route("/admin/chats/<int:space_id>/archive", methods=["POST"])
def admin_chat_archive(space_id: int):
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    ok = archive_space(space_id)
    return jsonify({"ok": ok})


def register_chat_routes(flask_app) -> None:
    flask_app.register_blueprint(bp)
