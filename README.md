# Telegram to WhatsApp Forwarder (Baileys & Multi-Channel Delivery Engine)

A high-throughput, fault-tolerant forwarder bot designed to relay trading signals, news, photos, audio, and documents from Telegram channels to WhatsApp groups and newsletters (`@newsletter`) with enterprise-grade deliverability and anti-ban protections.

---

## Architecture Overview

```
 Telegram Channels (65+)
         │
         ▼
 ┌──────────────────────────────────────┐
 │  Telegram Bot Service (Python)       │
 │  • Aiogram & Telethon MTProto Engine │
 │  • Delivery Rate Controller          │
 │  • DLQ & Retry Manager               │
 │  • Watchdog Auto-Reconnection Loop   │
 └──────────────────┬───────────────────┘
                    │ HTTP REST API (Protected by API Secret)
                    ▼
 ┌──────────────────────────────────────┐
 │  WhatsApp Microservice (Node.js/TS)  │
 │  • @whiskeysockets/baileys Socket    │
 │  • Direct WebSocket & Binary Protobuf│
 │  • Socket Transmission Rate Queues   │
 │  • MEX GraphQL Newsletter Discovery  │
 └──────────────────┬───────────────────┘
                    │ Direct WhatsApp WebSocket Frame
                    ▼
 WhatsApp Groups & Channels (Newsletters)
```

---

## Core Capabilities

### 1. Direct Socket Protocol (Baileys Migration)
- Powered by `@whiskeysockets/baileys` direct WebSocket socket connection.
- 95% less RAM consumption (~40 MB vs 1.2 GB under old Chromium engines).
- Sub-200ms frame dispatch latency.
- Full native support for WhatsApp Newsletters (`@newsletter`) and community groups (`@g.us`).

### 2. WhatsApp Anti-Ban & Deliverability Protection Suite
- **Token Bucket Limiter**: Per-recipient rate limiting with burst allowance and token refills.
- **Adaptive Queue Pacing**: Dynamically adjusts delays based on queue depth with $\pm 15\%$ Gaussian jitter to break bot periodicity.
- **Sliding-Window Volume Tracker**: Continuous 1-hour and 24-hour delivery monitoring with automated 80% warning and 100% throttling.

### 3. WhatsApp Session Watchdog (`/watchdog`)
- Background monitor probing `/health` every 5 minutes.
- Auto-detects socket disconnects and triggers `/createsession` self-healing.
- Logs outage durations and alerts admins when connection is restored.

### 4. Video Forwarding Policy
- Complete ban on video forwarding (pure videos, videos with captions, video round notes, animations/GIFs, and video files sent as documents) to protect bandwidth and WhatsApp channels.

### 5. Smart Newsletter Discovery (`/get_chat_id`)
- MEX GraphQL subscriber query (`executeWMexQuery`) discovering all subscribed channels.
- Multi-tier fuzzy keyword search, invite link resolution (`https://whatsapp.com/channel/...`), and numeric JID lookup.
- Persistent disk-backed registry (`known_newsletters.json`).

### 6. Interactive Admin Controls (`/channels`, `/map`, `/failed`, `/analytics`)
- Mobile-friendly inline keyboards with pagination.
- One-tap channel pause/resume toggles.
- Zero-typing group mapper wizard.
- Persistent Dead-Letter Queue (DLQ) with one-tap batch retries.
- High-resolution visual analytics infographic card and 24-hour traffic heatmap.

---

## Administrator Command Reference

| Command | Description |
| :--- | :--- |
| `📱 /channels` | Interactive paginated channel browser with one-tap pause/resume. |
| `🪄 /map [ch_id]` | Interactive WhatsApp Group Mapper Wizard to discover and bind chats. |
| `🔍 /get_chat_id <query>` | Smart search for WhatsApp groups and newsletters, or resolve invite links. |
| `🐕 /watchdog` | WhatsApp socket watchdog status, outage tracking, and self-heal stats. |
| `📬 /failed` (or `/dlq`) | Dead-Letter Queue inspector to review failed posts or retry all. |
| `🚨 /pause_all` | Emergency Kill Switch: Freeze all monitored channels immediately. |
| `▶️ /resume_all` | Resume All: Re-activate all channels and resume forwarding. |
| `📊 /analytics` | Visual Audience & Channel Intelligence infographic card and traffic heatmap. |
| `📈 /report [YYYY-MM-DD]` | Daily Delivery Summary report (live day-to-date or historical). |
| `⚠️ /stale [hours]` | Stale Channel Detector: Lists channels with zero activity (>72h). |
| `🖥️ /telemetry` | Server & Engine Telemetry: Host RAM, CPU, disk, Baileys RAM, bot RAM. |
| `📊 /status` | Complete system health overview. |
| `🛡️ /health_stats` | Real-time deliverability rates, token bucket capacity, and counters. |
| `🔑 /login` | Generates a WhatsApp QR pairing code for new sessions. |
| `🚪 /logout` | Severs socket connection and wipes session credentials safely. |
| `➕ /add_channel <ch_id>` | Register a new Telegram channel manually. |
| `🗑️ /delete_channel <ch_id>` | Delete a channel and cascade unbind its mapped destinations. |
| `➕ /add_group <ch_id> <grp_id>` | Map a channel to a WhatsApp group or newsletter JID manually. |
| `➖ /delete_group <ch_id> <grp_id>` | Remove a mapped group from a channel manually. |
| `📋 /view_groups` | Textual list of all channel-to-group mappings. |

---

## Environment Configuration

Configuration is managed via `Telegram/config.yml` (template in `Telegram/config.example.yml`) and `Whatsapp/.env`:

```yaml
# Telegram/config.yml
bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
admin_ids:
  - 123456789
api_id: 12345678
api_hash: "YOUR_API_HASH"
string_session: "YOUR_TELETHON_SESSION_STRING"
whatsapp_service: "http://whatsapp-bot:5426"
api_secret: "YOUR_SHARED_SECRET_KEY"

# Anti-Ban Settings
max_msgs_per_hour: 300
max_msgs_per_day: 2500
alert_threshold_percent: 80.0
ban_video_forwarding: true
```

---

## Deployment (Docker Compose)

```bash
# Build and run containers
docker compose build
docker compose up -d

# Inspect running services
docker compose ps

# View service logs
docker compose logs -f telegram-bot
docker compose logs -f whatsapp-bot
```
