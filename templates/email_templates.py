"""
Professional and friendly email templates for INFLUENCE Bot.
All emails are sent from jennifer@useinfluence.xyz.
"""


def deadline_reminder_email(
    creator_name: str,
    campaign_name: str,
    brand_name: str,
    deadline: str,
    reminder_type: str,
    days_left: int,
) -> dict:
    """Email template for deadline reminders (3 days, 1 day, overdue)."""
    if reminder_type == "overdue":
        subject = f"Urgent: {brand_name} Content — Deadline Passed"
        body = f"""Hi {creator_name},

Hope you're doing well. The deadline for the {brand_name} campaign ("{campaign_name}") was {deadline} and has now passed.

This is now quite time-sensitive, and we really want to make sure everything goes smoothly for both you and the brand.

Could you please reply to this email today with a status update? Even a quick note letting us know when we can expect the post would be really helpful.

Thank you so much - we truly appreciate your collaboration!

Best regards,

Jennifer
INFLUENCE Team
"""
    elif reminder_type == "1_day":
        subject = f"Reminder: {brand_name} Content Due Tomorrow"
        body = f"""Hi {creator_name},

Just a quick heads-up, the deadline for your {brand_name} campaign ("{campaign_name}") is tomorrow ({deadline}).

Please make sure your content is posted on time. If there's anything holding things up or if you need any support from our end, let us know and we're happy to help!

Looking forward to seeing the content go live.

Best,

Jennifer
INFLUENCE Team
"""
    else:
        subject = f"Upcoming Deadline: {brand_name} Content Due in {days_left} Days"
        body = f"""Hi {creator_name},

Just a friendly reminder that the deadline for your {brand_name} campaign ("{campaign_name}") is coming up on {deadline} - that's {days_left} days from now.

If you haven't already, please make sure everything is on track for posting by the deadline. If you have any questions about the brief or deliverables, don't hesitate to reach out.

Thanks for being such a great partner on this!

Warm regards,

Jennifer
INFLUENCE Team
"""
    return {"subject": subject, "body": body}


def video_approved(
    creator_name: str,
    brand_name: str,
    submit_posts_url: str | None = None,
) -> dict:
    """
    Email to creator when their video has been approved by the brand.

    `submit_posts_url` is the creator-specific URL from the ReelStats
    API (`creators[].submissionLinks.submitPostsUrl`). When present we
    include the "submit the post link(s) here" sentence; when missing
    (older review rows / API didn't return it) we drop that sentence
    entirely so the email doesn't ship a broken link.
    """
    subject = f"Great News! Your {brand_name} Video Has Been Approved"
    if submit_posts_url:
        submit_line = (
            "Once it's live, please submit the post link(s) here so we can "
            f"track the performance: {submit_posts_url}\n\n"
        )
    else:
        submit_line = ""
    body = f"""Hi {creator_name},

The {brand_name} team has reviewed and approved your video!

You're all set to go ahead and post it. Just a quick reminder to make sure all the required tags, hashtags, and mentions are included as per the posting guidelines in the content brief.

{submit_line}Thanks for the awesome work! :)

Cheers,

Jennifer
INFLUENCE Team
"""
    return {"subject": subject, "body": body}


def video_changes_requested(
    creator_name: str, brand_name: str, feedback: str
) -> dict:
    """Email to creator when the brand requests changes to their video."""
    subject = f"Feedback on Your {brand_name} Video — Small Changes Needed"
    body = f"""Hi {creator_name},

Thanks so much for submitting your video for {brand_name}! The brand team has reviewed it and they really liked the overall direction. They do have a few notes they'd love for you to incorporate:

---
{feedback}
---

We know revisions can be a bit of extra work, but these tweaks will really help make the final content shine. Once you've made the updates, please resubmit the revised video and we'll get it back to the brand for a quick final review.

If you have any questions about the feedback, feel free to reach out — happy to clarify anything!

Thanks for being such a great partner on this.

Warm regards,
Jennifer
INFLUENCE Team
"""
    return {"subject": subject, "body": body}


def chat_invite(
    creator_name: str, brand_name: str, campaign_name: str, chat_url: str
) -> dict:
    """Email to a creator with their magic link into a new chat space."""
    subject = f"{brand_name} wants to chat about your {campaign_name} video"
    body = f"""Hi {creator_name},

The {brand_name} team has requested changes on your {campaign_name} video and opened a chat space so you can discuss the feedback directly.

Open the chat here (this link will sign you in automatically):
{chat_url}

Inside the chat you can reply with messages and screenshots. Future review submissions on this campaign will use the same chat — no need to start over.

Talk soon,
INFLUENCE Team
"""
    return {"subject": subject, "body": body}


def chat_new_message(
    creator_name: str, brand_name: str, sender_name: str, preview: str, chat_url: str
) -> dict:
    """Email to a creator when a brand posts a new message in the chat space."""
    subject = f"New message from {sender_name} on your {brand_name} chat"
    body = f"""Hi {creator_name},

{sender_name} just sent you a message in your {brand_name} chat:

---
{preview}
---

Reply here:
{chat_url}

Best,
INFLUENCE Team
"""
    return {"subject": subject, "body": body}


def review_thread_comment(
    creator_name: str, brand_name: str, commenter: str, comment: str
) -> dict:
    """Email to creator relaying a Slack thread reply on their review submission."""
    subject = f"New Comment on Your {brand_name} Video Submission"
    body = f"""Hi {creator_name},

Someone from the {brand_name} team left a comment on your video submission:

---
{commenter}:
{comment}
---

Feel free to reach out if you'd like to discuss or have any questions.

Thanks!
Jennifer
INFLUENCE Team
"""
    return {"subject": subject, "body": body}
