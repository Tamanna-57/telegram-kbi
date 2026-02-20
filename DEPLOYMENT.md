# Deployment Guide — KBI Telegram Alert System

## Final Folder Structure

```
telegram-kbi/
├── shared/                          ← Master copies of shared modules (DO NOT delete)
│   ├── config.py                    ← All configuration + env variable keys
│   ├── telegram_service.py          ← Messaging: send, edit, delete, escape_markdown
│   └── alert_dispatcher.py         ← Alert routing: group-first, optional individual
│
├── gantt-alerts/                    ← Cloud Function: telegram-1 (Gantt Alert Service)
│   ├── main.py                      ← analyze_gantt_charts_telegram (entry point)
│   ├── config.py                    ← Copied from shared/ by deploy.sh
│   ├── telegram_service.py          ← Copied from shared/ by deploy.sh
│   ├── alert_dispatcher.py          ← Copied from shared/ by deploy.sh
│   └── requirements.txt
│
├── telegram-webhook/                ← Cloud Function: telegramwebhook (Bot)
│   ├── main.py                      ← telegram_bot (entry point)
│   ├── config.py                    ← Copied from shared/ by deploy.sh
│   ├── telegram_service.py          ← Copied from shared/ by deploy.sh
│   ├── alert_dispatcher.py          ← Copied from shared/ by deploy.sh
│   └── requirements.txt
│
├── attendance-checker/              ← Cloud Function: attendance discrepancy checker
│   ├── main.py                      ← check_attendance_discrepancies (entry point)
│   ├── config.py                    ← Copied from shared/ by deploy.sh
│   ├── telegram_service.py          ← Copied from shared/ by deploy.sh
│   ├── alert_dispatcher.py          ← Copied from shared/ by deploy.sh
│   └── requirements.txt
│
├── deploy.sh                        ← Deployment helper script
└── DEPLOYMENT.md                    ← This file
```

**Rule:** Always edit the files in `shared/`. Never directly edit the copies inside function folders — they get overwritten on every deploy.

---

## How Alert Routing Works

```
SEND_INDIVIDUAL_ALERTS = false (default)     SEND_INDIVIDUAL_ALERTS = true
────────────────────────────────────────     ──────────────────────────────────────
      Gantt Alert fires                             Gantt Alert fires
            │                                             │
            ▼                                             ▼
    Group Chat only                             Group Chat   +   Each user's DM
```

Toggle by setting the `SEND_INDIVIDUAL_ALERTS` environment variable in Cloud Console.

---

## Step 1: Prerequisites

```bash
# 1. Install Google Cloud SDK
#    https://cloud.google.com/sdk/docs/install

# 2. Login and set your project
gcloud auth login
gcloud config set project arched-elixir-464218-j8
gcloud config set functions/region asia-south2
```

---

## Step 2: Get Your Telegram Group Chat ID

You need this for `TELEGRAM_GROUP_CHAT_ID`. Group IDs are **negative numbers** (e.g. `-1001234567890`).

**Method (takes ~2 minutes):**

1. Create a Telegram group (or use an existing one).
2. Add your bot to the group:
   - Open the group → tap the name → Add Members → search for your bot's username.
3. Make the bot an **Admin** of the group:
   - Tap the bot in the member list → Promote to Admin → enable "Post Messages" → Save.
4. Send **any message** in the group (e.g. "hello").
5. Open this URL in your browser (replace `BOT_TOKEN` with your actual token):
   ```
   https://api.telegram.org/botBOT_TOKEN/getUpdates
   ```
6. Look for `"chat"` → `"id"` in the response. The group ID is the negative number, e.g.:
   ```json
   "chat": { "id": -1001234567890, "title": "KBI Alerts", "type": "supergroup" }
   ```
7. Copy that number (including the minus sign).

---

## Step 3: Set Environment Variables in Cloud Console

For **each** Cloud Function:

1. Go to **Cloud Console → Cloud Functions**
2. Click the function name → **Edit**
3. Click **"Variables & Secrets"** tab
4. Add these variables:

| Variable | Value | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `123456:ABCdef...` (from @BotFather) | ✅ Yes |
| `TELEGRAM_GROUP_CHAT_ID` | `-1001234567890` (from Step 2) | ✅ Yes |
| `ADMIN_CHAT_ID` | Your personal Telegram chat ID | ✅ Yes |
| `SEND_INDIVIDUAL_ALERTS` | `false` | Optional (default: false) |
| `GCS_BUCKET_NAME` | `kbi-first` | Optional (already defaulted) |
| `DEBUG_MODE` | `false` | Optional |

> **How to get your personal chat ID:** Message @userinfobot on Telegram. It replies with your chat ID.

---

## Step 4: Deploy

```bash
# From the project root (telegram-kbi/):
chmod +x deploy.sh

# Deploy all 3 functions at once:
./deploy.sh

# Or deploy one at a time:
./deploy.sh gantt        # Gantt alert service
./deploy.sh webhook      # Telegram bot webhook
./deploy.sh attendance   # Attendance checker

# Only copy shared files (no deploy) — useful for local testing:
./deploy.sh copy
```

What `deploy.sh` does automatically:
1. Copies `shared/config.py`, `shared/telegram_service.py`, `shared/alert_dispatcher.py` into each function folder.
2. Runs `gcloud functions deploy` for each selected function.

---

## Step 5: Re-register the Webhook (after redeploying telegramwebhook)

After deploying `telegramwebhook`, get its URL and register it with Telegram:

```bash
# Get the function URL
gcloud functions describe telegramwebhook --region=asia-south2 --format="value(serviceConfig.uri)"

# Register the webhook (replace placeholders)
curl -X POST "https://api.telegram.org/botYOUR_BOT_TOKEN/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{"url": "YOUR_FUNCTION_URL"}'

# Verify
curl "https://api.telegram.org/botYOUR_BOT_TOKEN/getWebhookInfo"
```

---

## Testing Checklist

### Test 1: Group alert delivery
- [ ] Trigger the Gantt alert function manually (Cloud Console → Test tab)
- [ ] Verify alert appears in your Telegram group
- [ ] Verify it does NOT appear in individual DMs (when `SEND_INDIVIDUAL_ALERTS=false`)

### Test 2: Individual alerts (optional mode)
- [ ] Set `SEND_INDIVIDUAL_ALERTS=true` in Cloud Console for gantt-alerts
- [ ] Trigger again — alert should appear in BOTH the group and the user's DM
- [ ] Set back to `false` after testing

### Test 3: Webhook bot
- [ ] Open your bot in Telegram and send `/start`
- [ ] Send your email address → should confirm login
- [ ] Send an unknown email → should appear in pending_users.json
- [ ] Admin should receive approval buttons

### Test 4: Attendance checker
- [ ] Trigger the attendance function with a test date:
  ```
  GET https://FUNCTION_URL?date=2026-02-04
  ```
- [ ] Verify the group receives the discrepancy alert
- [ ] Verify email is sent to admin emails

### Test 5: Missing/corrupted GCS file
- [ ] Temporarily rename `users3.json` to `users3_bak.json` in GCS
- [ ] Trigger gantt-alerts — function should return 500 with a clear error message
- [ ] Restore the file

### Test 6: Bot not in group
- [ ] Temporarily remove the bot from the group
- [ ] Trigger an alert — Cloud Logs should show `Permanent Telegram error [403]`
- [ ] Re-add the bot

---

## Rollback Instructions

If anything breaks after deploying, roll back to the previous version:

```bash
# List recent versions of a function
gcloud functions list --filter="name:telegram1"

# Rollback by redeploying from the GCS source ZIP (your original version)
# The original ZIPs are still at:
#   gs://run-sources-arched-elixir-464218-j8-asia-south2/services/telegram1/...
#   gs://run-sources-arched-elixir-464218-j8-asia-south2/services/telegramwebhook/...

# Option A: Use Cloud Console
#   Cloud Functions → function name → Revisions tab → select previous revision → Route traffic

# Option B: Re-upload your original main.py manually via Cloud Console inline editor
#   Cloud Functions → Edit → Inline Editor → paste original code → Deploy
```

**If only the group alert is broken but individual alerts worked before:**
- Set `SEND_INDIVIDUAL_ALERTS=true` temporarily to restore individual delivery.
- Then fix the `TELEGRAM_GROUP_CHAT_ID` env variable or bot group membership.

---

## Optional Future Improvements

- **Retry queue using Cloud Tasks:** If group alert fails, push to a Cloud Tasks queue for later retry instead of relying on in-process retry.
- **Alert deduplication using Firestore:** Store a hash of each sent alert with a TTL to prevent sending duplicate alerts if the scheduler fires twice.
- **Telegram channel instead of group:** Use a private channel for alerts (cleaner history, no member can accidentally click buttons meant for others).
- **Per-user alert preferences in GCS:** Store a `receive_individual_alerts: true/false` flag per user instead of a global toggle.
- **Cloud Monitoring alert on function failures:** Set up a Cloud Monitoring alert to notify you when any Cloud Function has a non-200 response rate above a threshold.
