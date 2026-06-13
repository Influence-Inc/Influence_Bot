"""
Slack Block Kit message templates for INFLUENCE Bot.
Rich notifications for milestones, deliverables, deadlines, uploads,
payment summaries, and webhook events (review/video links).
"""

import re
from datetime import date


def _format_upload_date(iso_date: str) -> str:
    """Render an ISO date string (YYYY-MM-DD) as 'Month D, YYYY'."""
    if not iso_date:
        return ""
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', etc."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _video_ordinal_label(video_title: str) -> str:
    """
    Derive an ordinal label like '1st video' from a 'Post N' title.
    Falls back to the title itself, or 'video' if neither is usable.
    """
    if video_title:
        m = re.search(r"(\d+)", video_title)
        if m:
            return f"{_ordinal(int(m.group(1)))} video"
        return video_title
    return "video"


def build_milestone_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    milestone_label: str,
    first_posted: str = "",
    video_link: str = "",
    include_brand: bool = True,
) -> list[dict]:
    """
    Notification when a single post crosses a view milestone (250K, 500K,
    1M, 1.5M, ...). The view count refers to that one post, not the
    creator's combined views across all their posts.

    `include_brand=True` for the admin channel, `False` for the brand's
    own workspace (where the brand line is redundant).
    """
    lines = [f":rocket: *Breakout video alert - {milestone_label} views!*", ""]
    if include_brand and brand_name:
        lines.append(f"*Brand:* {brand_name}")
    if campaign_name:
        lines.append(f"*Campaign:* {campaign_name}")
    if creator_username:
        lines.append(f"*Creator:* @{creator_username}")
    if first_posted:
        lines.append(f"*1st Posted:* {first_posted}")
    if video_link:
        lines.append(f"*Link:* {video_link}")

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        },
        {"type": "divider"},
    ]


def _mark_as_paid_value(campaign_id: str, creator_username: str) -> str:
    """Encode the identifiers needed by the mark_as_paid action handler."""
    return f"{campaign_id}|{creator_username}"


def build_deliverable_complete_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    campaign_id: str = "",
) -> list[dict]:
    """Notification when all deliverables are complete — flag for payment."""
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":white_check_mark: Deliverables Complete!",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*@{creator_username}* has completed all deliverables "
                    f"for *{campaign_name}* ({brand_name}).\n\n"
                    f":moneybag: *This creator is ready to be paid.*"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"mark_paid_{campaign_id}_{creator_username}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "mark_as_paid",
                    "style": "primary",
                    "text": {
                        "type": "plain_text",
                        "text": ":moneybag: Mark as paid",
                    },
                    "value": _mark_as_paid_value(campaign_id, creator_username),
                },
            ],
        },
        {"type": "divider"},
    ]


def build_deadline_reminder_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    deadline: str,
    reminder_type: str,
    days_left: int,
) -> list[dict]:
    """Deadline reminder — 3 days, 1 day, or overdue."""
    if reminder_type == "overdue":
        emoji = ":red_circle:"
        title = "Deadline Overdue!"
        status_text = f"The deadline was *{deadline}* — now *{abs(days_left)} day(s) overdue*."
    elif reminder_type == "1_day":
        emoji = ":warning:"
        title = "Deadline Tomorrow!"
        status_text = f"The deadline is *{deadline}* — *1 day remaining*."
    else:
        emoji = ":calendar:"
        title = "Deadline Approaching"
        status_text = f"The deadline is *{deadline}* — *{days_left} days remaining*."

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {title}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Creator:*\n@{creator_username}"},
                {"type": "mrkdwn", "text": f"*Campaign:*\n{campaign_name}"},
                {"type": "mrkdwn", "text": f"*Brand:*\n{brand_name}"},
                {"type": "mrkdwn", "text": f"*Deadline:*\n{deadline}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": status_text},
        },
        {"type": "divider"},
    ]


def build_upload_followup_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    videos_posted: int,
    videos_required: int,
    deadline: str,
    days_left: int,
) -> list[dict]:
    """Reminder when a creator is behind on uploads near the deadline."""
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":film_frames: Upload Reminder",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Creator:*\n@{creator_username}"},
                {"type": "mrkdwn", "text": f"*Campaign:*\n{campaign_name}"},
                {"type": "mrkdwn", "text": f"*Brand:*\n{brand_name}"},
                {"type": "mrkdwn", "text": f"*Deadline:*\n{deadline}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"@{creator_username} has posted *{videos_posted}/{videos_required}* "
                    f"videos with *{days_left} day(s)* remaining until the deadline."
                ),
            },
        },
        {"type": "divider"},
    ]


def build_payment_summary_blocks(completed_creators: list[dict]) -> list[dict]:
    """Daily payment summary of all creators with completed deliverables."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":sunrise: Daily Payment Summary",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{len(completed_creators)} creator(s)* have completed "
                    f"deliverables and are ready for payment:"
                ),
            },
        },
        {"type": "divider"},
    ]

    for creator in completed_creators:
        username = creator.get("username", "Unknown")
        campaign_name = creator.get("campaign_name", "")
        brand_name = creator.get("brand_name", "")
        campaign_id = creator.get("campaign_id", "")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":moneybag: *@{username}* — "
                        f"{campaign_name} ({brand_name})"
                    ),
                },
                "accessory": {
                    "type": "button",
                    "action_id": "mark_as_paid",
                    "style": "primary",
                    "text": {
                        "type": "plain_text",
                        "text": "Mark as paid",
                    },
                    "value": _mark_as_paid_value(campaign_id, username),
                },
            }
        )

    return blocks


def build_review_submitted_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    video_link: str,
    notes: str,
    review_id: int | None = None,
    show_meta: bool = False,
    chat_url: str | None = None,
) -> list[dict]:
    """
    Webhook event: creator submitted a video for review.

    `show_meta=True` adds Brand + Campaign rows — used on the admin
    side where one channel sees content from many brands. Brand
    workspaces leave it off since the workspace itself identifies
    the brand.

    `chat_url`, when provided, is baked into the Request Changes button
    as a URL — clicking the button opens the brand's chat space in a
    browser AND fires the action handler on our backend simultaneously
    (Slack delivers both when a button has both `url` and `action_id`).
    """
    body_lines = [":video_camera: *Content to be reviewed*", ""]
    if show_meta:
        if brand_name:
            body_lines.append(f"*Brand:* {brand_name}")
        if campaign_name:
            body_lines.append(f"*Campaign:* {campaign_name}")
        if brand_name or campaign_name:
            body_lines.append("")
    if creator_username:
        body_lines.append("*Instagram username*")
        body_lines.append(f"@{creator_username}")
        body_lines.append("")
    if video_link:
        body_lines.append("*Link*")
        body_lines.append(video_link)
    if notes:
        body_lines.append("")
        body_lines.append(f":memo: *Notes:* {notes}")

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(body_lines).rstrip()},
        },
    ]

    if review_id is not None:
        request_changes_btn = {
            "type": "button",
            "action_id": "review_request_changes",
            "text": {"type": "plain_text", "text": "Request Changes"},
            "value": str(review_id),
        }
        if chat_url:
            request_changes_btn["url"] = chat_url
        blocks.append(
            {
                "type": "actions",
                "block_id": f"review_actions_{review_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "review_approve",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": str(review_id),
                    },
                    request_changes_btn,
                ],
            }
        )

    blocks.append({"type": "divider"})
    return blocks


def build_review_approved_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    video_link: str,
    actor_name: str,
) -> list[dict]:
    """
    Notification posted to the admin #content-reviews channel after a brand
    clicks Approve on a review. Distinct from the updated-in-place review
    message so the admin team gets a fresh ping even when the click came
    from the brand's own workspace.
    """
    body_lines = [":white_check_mark: *Review approved*", ""]
    if brand_name:
        body_lines.append(f"*Brand:* {brand_name}")
    if campaign_name:
        body_lines.append(f"*Campaign:* {campaign_name}")
    if creator_username:
        body_lines.append(f"*Creator:* @{creator_username}")
    if video_link:
        body_lines.append(f"*Link:* {video_link}")
    if actor_name:
        body_lines.append("")
        body_lines.append(f":bust_in_silhouette: Approved by *{actor_name}*")

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(body_lines).rstrip()},
        },
        {"type": "divider"},
    ]


_PLATFORM_LABELS = {
    "instagram": "Reels",
    "tiktok": "Tiktok",
    "youtube": "Shorts",
}


def build_video_links_submitted_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    video_title: str,
    links: list[dict],
    show_meta: bool = False,
) -> list[dict]:
    """
    Webhook event: creator submitted video links (posted content).

    `links` is a list of dicts with keys `platform` (raw key, e.g.
    'instagram', 'tiktok', 'youtube') and `url`.

    `show_meta=True` adds Brand + Campaign rows for the admin side.
    """
    body_lines = [":tada: *Content posted*", ""]
    if show_meta:
        if brand_name:
            body_lines.append(f"*Brand:* {brand_name}")
        if campaign_name:
            body_lines.append(f"*Campaign:* {campaign_name}")
        if brand_name or campaign_name:
            body_lines.append("")
    if creator_username:
        body_lines.append("*Instagram username*")
        body_lines.append(f"@{creator_username}")
        body_lines.append("")

    body_lines.append(f"*{_video_ordinal_label(video_title)}*")

    for link in links:
        url = link.get("url")
        if not url:
            continue
        platform_key = (link.get("platform") or "").lower()
        label = _PLATFORM_LABELS.get(platform_key) or (
            link.get("platform") or platform_key or "Link"
        )
        body_lines.append("")
        body_lines.append(f"*{label}*")
        body_lines.append(url)

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(body_lines).rstrip()},
        },
        {"type": "divider"},
    ]


def build_chat_new_message_blocks(
    *,
    creator_username: str,
    campaign_name: str,
    sender_name: str,
    preview: str,
    chat_url: str,
) -> list[dict]:
    """New-message ping into the brand workspace channel."""
    preview = (preview or "").replace("\n", " ").strip()
    if len(preview) > 200:
        preview = preview[:197] + "…"
    header = (
        f":envelope_with_arrow: *New message from {sender_name} "
        f"in chat with @{creator_username}* — _{campaign_name}_"
    )
    if preview:
        header = f"{header}\n>{preview}"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Chat"},
                    "url": chat_url,
                }
            ],
        },
    ]

