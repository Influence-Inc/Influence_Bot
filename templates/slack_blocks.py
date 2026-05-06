"""
Slack Block Kit message templates for INFLUENCE Bot.
Rich notifications for milestones, deliverables, deadlines, uploads,
payment summaries, and webhook events (review/video links).
"""


def build_milestone_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    milestone_label: str,
    current_views: str,
    video_title: str = "",
    video_link: str = "",
) -> list[dict]:
    """
    Notification when a single post crosses a view milestone (250K, 500K,
    1M, 1.5M, ...). The view count refers to that one post, not the
    creator's combined views across all their posts.
    """
    if video_link and video_title:
        post_field = f"<{video_link}|{video_title}>"
    elif video_link:
        post_field = f"<{video_link}|View post>"
    elif video_title:
        post_field = video_title
    else:
        post_field = "—"

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":trophy: Post Milestone Reached!",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Creator:*\n@{creator_username}"},
                {"type": "mrkdwn", "text": f"*Campaign:*\n{campaign_name}"},
                {"type": "mrkdwn", "text": f"*Brand:*\n{brand_name}"},
                {"type": "mrkdwn", "text": f"*Post:*\n{post_field}"},
                {"type": "mrkdwn", "text": f"*Milestone:*\n{milestone_label} views"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":chart_with_upwards_trend: This post now has *{current_views}* views",
            },
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
) -> list[dict]:
    """Webhook event: creator submitted a video for review."""
    text_parts = [
        f"*@{creator_username}* submitted a video for review "
        f"on *{campaign_name}* ({brand_name})."
    ]
    if video_link:
        text_parts.append(f":link: <{video_link}|Watch Video>")
    if notes:
        text_parts.append(f":memo: *Notes:* {notes}")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":film_frames: Video Submitted for Review",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(text_parts),
            },
        },
    ]

    if review_id is not None:
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
                    {
                        "type": "button",
                        "action_id": "review_request_changes",
                        "text": {"type": "plain_text", "text": "Request Changes"},
                        "value": str(review_id),
                    },
                ],
            }
        )

    blocks.append({"type": "divider"})
    return blocks


def build_video_links_submitted_blocks(
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    video_title: str,
    links: list[dict],
) -> list[dict]:
    """Webhook event: creator submitted video links (posted content)."""
    link_lines = []
    for link in links:
        link_lines.append(f"• *{link['platform']}:* <{link['url']}|View>")

    body = (
        f"*@{creator_username}* submitted video links "
        f"for *{campaign_name}* ({brand_name})."
    )
    if video_title:
        body += f"\n:clapper: *Title:* {video_title}"
    if link_lines:
        body += "\n\n" + "\n".join(link_lines)

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":link: Video Links Submitted",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": body,
            },
        },
        {"type": "divider"},
    ]
