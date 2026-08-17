# Slack Bot API Integration Guide

## Overview

The ReelStats server exposes two integration mechanisms for the Slack bot:

1. **Polling endpoint** — `GET /api/bot/campaigns` returns a full snapshot of all campaigns with computed deliverables status, views, reviews, etc.
2. **Webhooks** — the server POSTs to a configurable URL when creators submit video links or review videos.

---

## Setup

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | **Yes** | Auth token for the bot polling endpoint. The bot must send this as the `x-bot-token` header. |
| `SLACK_WEBHOOK_URL` | No | If set, the server will POST webhook events to this URL. Can also be configured at runtime via the admin API. |

---

## 1. Polling Endpoint

### `GET /api/bot/campaigns`

**Auth:** `x-bot-token` header must match the `BOT_TOKEN` env var.

**Optional query params:**
- `?campaign=<campaignId>` — filter to a single campaign
- `?creator=<username>` — filter to a specific creator across all campaigns (without `@`)

### Response Shape

```json
{
  "campaigns": [
    {
      "id": "fc6cd16f226f",
      "name": "Reve Features",
      "brandName": "Reve",
      "slug": "reve/reve-features",
      "totalBudget": 25000,
      "createdAt": 1771028436450,
      "creatorCount": 5,
      "creators": [
        {
          "id": "8417c99c2070",
          "username": "tharun.fyi",
          "email": "tharunr16@gmail.com",
          "deadline": "2026-04-22",
          "deliverables": {
            "minViews": 200000,
            "minVideos": 2,
            "actualViews": 5700740,
            "actualVideos": 3,
            "viewsComplete": true,
            "videosComplete": true,
            "allComplete": true
          },
          "videos": [
            {
              "id": "0e41efb3563a",
              "title": "Post 1",
              "uploadDate": "2026-03-15",
              "estPostDate": null,
              "hasLinks": true,
              "links": {
                "instagram": "https://www.instagram.com/reel/DUB674xk2bc/",
                "tiktok": "https://vt.tiktok.com/ZSaYgLwmq/"
              },
              "views": {
                "instagram": 67173,
                "tiktok": 10100
              },
              "totalViews": 77273
            }
          ],
          "reviews": [
            {
              "id": "a27934cfd7ea",
              "videoLink": "https://drive.google.com/file/abc123",
              "notes": "Hi! Here is the first draft",
              "submittedAt": 1775922404706
            }
          ],
          "totalViews": 5700740,
          "totalVideosPosted": 3
        }
      ]
    }
  ]
}
```

### Key Fields for Bot Logic

| Field | Type | Description |
|---|---|---|
| `creator.email` | `string \| null` | Creator's email for notifications |
| `creator.deadline` | `string \| null` | ISO date string (YYYY-MM-DD) |
| `creator.deliverables.minViews` | `number \| null` | Target minimum views |
| `creator.deliverables.minVideos` | `number \| null` | Target minimum video count |
| `creator.deliverables.actualViews` | `number` | Current total views |
| `creator.deliverables.actualVideos` | `number` | Current count of posted videos |
| `creator.deliverables.allComplete` | `boolean \| null` | All deliverables met |
| `creator.totalViews` | `number` | Sum of all video views |
| `creator.totalVideosPosted` | `number` | Count of posted videos |

---

## 2. Webhooks

### Event: `video_links_submitted`

Fired when a creator submits video links.

```json
{
  "event": "video_links_submitted",
  "timestamp": 1775922404706,
  "campaign": {
    "name": "Reve Features",
    "brandName": "Reve",
    "slug": "reve/reve-features"
  },
  "creator": {
    "username": "tharun.fyi",
    "email": "tharunr16@gmail.com",
    "submissionLinks": {
      "submitForReviewUrl": "https://campaign.influence.technology/reve/reve-features/submit-for-review?username=tharun.fyi",
      "submitPostsUrl": "https://campaign.influence.technology/reve/reve-features/submit-links?username=tharun.fyi"
    }
  },
  "video": {
    "id": "abc123def456",
    "title": "Post 4",
    "instagram": "https://www.instagram.com/reel/DXyz123/",
    "tiktok": null,
    "youtube": null
  }
}
```

### Event: `review_submitted`

Fired when a creator submits a video for review.

```json
{
  "event": "review_submitted",
  "timestamp": 1775922404706,
  "campaign": {
    "name": "Reve Features",
    "brandName": "Reve",
    "slug": "reve/reve-features"
  },
  "creator": {
    "username": "tharun.fyi",
    "email": "tharunr16@gmail.com",
    "submissionLinks": {
      "submitForReviewUrl": "https://campaign.influence.technology/reve/reve-features/submit-for-review?username=tharun.fyi",
      "submitPostsUrl": "https://campaign.influence.technology/reve/reve-features/submit-links?username=tharun.fyi"
    }
  },
  "review": {
    "videoLink": "https://drive.google.com/file/abc123",
    "notes": "Hi! Here is the first draft for review"
  }
}
```

### Live-Data Events (drive scheduler checks in real time)

The four events below replace the 5-minute polling delay with zero-latency
notifications. The bot still polls `GET /api/bot/campaigns` every 60 seconds
as a safety net, so dropping any of these events only delays notification
by up to a minute — it does not lose it. Internal dedup tables ensure
duplicate webhook + poll deliveries are idempotent.

**Shared payload shape.** All four events include `campaign` (with `id` —
required for dedup — plus `name`, `brandName`, `slug`) and a `creator`
object with the full creator record from `GET /api/bot/campaigns`:

```json
{
  "campaign": {
    "id": "fc6cd16f226f",
    "name": "Reve Features",
    "brandName": "Reve",
    "slug": "reve/reve-features"
  },
  "creator": {
    "username": "tharun.fyi",
    "email": "tharunr16@gmail.com",
    "deadline": "2026-04-22",
    "deliverables": {
      "minViews": 200000,
      "minVideos": 2,
      "allComplete": true
    },
    "totalViews": 5700740,
    "totalVideosPosted": 3
  }
}
```

#### Event: `views_updated`

Fire whenever `creator.totalViews` changes. Drives the milestone check
(250K / 500K / 1M / 1.5M / 2M / 5M / 10M / 20M / 50M / 100M).

```json
{ "event": "views_updated", "timestamp": 1775922404706, "campaign": {...}, "creator": {...} }
```

#### Event: `deliverables_updated`

Fire whenever `creator.deliverables.allComplete` flips or
`creator.totalVideosPosted` changes. Drives the deliverables-complete
payment flag and the upload follow-up check.

```json
{ "event": "deliverables_updated", "timestamp": 1775922404706, "campaign": {...}, "creator": {...} }
```

#### Event: `deadline_check`

Fire once per day per active creator (suggested: between 08:00–09:00 in
the campaign's timezone). Drives the deadline-reminder Slack + email
flow and the upload follow-up check. Cheaper than polling.

```json
{ "event": "deadline_check", "timestamp": 1775922404706, "campaign": {...}, "creator": {...} }
```

#### Event: `creator_updated`

Generic fallback — fire whenever any creator field changes and you don't
want to classify the change. The bot runs all four per-creator checks
(milestones, deliverables, deadline, upload follow-up).

```json
{ "event": "creator_updated", "timestamp": 1775922404706, "campaign": {...}, "creator": {...} }
```

### Webhook Behavior

- Fire-and-forget, 10 second timeout, no retries
- Content-Type: application/json, Method: POST
- Dedup is the bot's responsibility; duplicate events are safe

---

## 3. Error Responses

| Status | Meaning |
|---|---|
| `503` | `BOT_TOKEN` env var not set on the server |
| `401` | Invalid or missing `x-bot-token` header |
| `200` | Success |
