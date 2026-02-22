"""
config.py — Central configuration module for all Cloud Functions.

HOW TO USE:
    This is the master copy. Before deploying any Cloud Function, the deploy.sh
    script automatically copies this file into each function's folder.
    Do NOT import from "../shared/config" — always import from "config" directly.

ENVIRONMENT VARIABLES (set these in Google Cloud Console → Cloud Functions → Edit → Variables):
    TELEGRAM_BOT_TOKEN        — Your Telegram bot token from @BotFather (REQUIRED)
    TELEGRAM_GROUP_CHAT_ID    — The group chat ID for alert delivery (REQUIRED for group alerts)
    ADMIN_CHAT_ID             — Admin's personal Telegram chat ID (for approval requests)
    SEND_INDIVIDUAL_ALERTS    — "true" to also send to individual users. Default: "false"
    GCS_BUCKET_NAME           — GCS bucket name. Default: "kbi-first"
    DEBUG_MODE                — "true" to enable verbose logging. Default: "false"

NOTE ON TELEGRAM_GROUP_CHAT_ID:
    Group chat IDs are negative numbers, e.g. -1001234567890
    See DEPLOYMENT.md → "How to get the Group Chat ID" for step-by-step instructions.
"""

import os

# ============================================================
# GCS BUCKET & FILE PATHS
# ============================================================

# Main GCS bucket where all project data is stored
GCS_BUCKET_NAME: str = os.environ.get("GCS_BUCKET_NAME", "kbi-first")

# Alias used by attendance-checker (backward compatible with original code)
GCS_BUCKET: str = GCS_BUCKET_NAME

# Folder prefix for Gantt chart JSON files inside the bucket
GANTT_CHARTS_GCS_FOLDER: str = "gantt_charts/"

# Registered users file (telegram_chat_id, name, language, role, etc.)
USERS_FILE: str = os.environ.get("USERS_FILE", "users3.json")

# Attendance checker uses a different users file
USERS_FILE_ATTENDANCE: str = "users.json"

# Pending approval requests (users who emailed the bot but aren't registered)
PENDING_USERS_FILE: str = "pending_users.json"

# Shared settings for all cloud functions (enabled/disabled, lag_days, etc.)
CLOUD_FUNCTIONS_SETTINGS_PATH: str = "cloud_functions_settings.json"

# Email notification settings for attendance checker
EMAIL_SETTINGS_PATH: str = "email_notification_settings.json"

# Press shop master file paths (for attendance discrepancy checker)
PRESS_MASTER_MAINTENANCE: str = (
    "press_entry_report_shop/maintenance/master_maintenance_tool_trial.json"
)
PRESS_MASTER_PRODUCTION: str = (
    "press_entry_report_shop/production/master_press_entry_report.json"
)

# HR attendance folder prefix (e.g. hr_attendance_v1/2026-02-04.json)
HR_ATTENDANCE_DIR: str = "hr_attendance_v1"

# ============================================================
# TELEGRAM API
# ============================================================

TELEGRAM_API_BASE: str = "https://api.telegram.org"

# Bot token — shared across ALL three Cloud Functions
BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# PRIMARY alert destination: Telegram group or channel chat ID.
# All system alerts (Gantt, attendance) go here first.
# Must be set in Cloud Console environment variables.
# Format: negative integer string, e.g. "-1001234567890"
TELEGRAM_GROUP_CHAT_ID: str = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "")

# Admin's personal chat ID — used for private approval requests and delay reports.
# This is separate from the group and always receives private admin messages.
ADMIN_CHAT_ID: str = os.environ.get("ADMIN_CHAT_ID", "")

# Telegram bot username (without @), e.g. "KBIAttendanceBot"
# Used to generate a one-click activation deeplink in email notifications.
# Set this in Cloud Console → Cloud Functions → Edit → Variables & Secrets.
BOT_USERNAME: str = os.environ.get("BOT_USERNAME", "")

# ============================================================
# ALERT BEHAVIOR FLAGS
# ============================================================

# If True, alerts are sent to BOTH the group AND each individual user.
# If False (default), alerts go to the GROUP ONLY.
# Toggle by setting env var SEND_INDIVIDUAL_ALERTS="true" or "false".
SEND_INDIVIDUAL_ALERTS: bool = (
    os.environ.get("SEND_INDIVIDUAL_ALERTS", "false").lower() == "true"
)

# If True, extra debug information is printed to Cloud Logging.
# Useful during setup. Set to "false" in production.
DEBUG_MODE: bool = os.environ.get("DEBUG_MODE", "false").lower() == "true"

# ============================================================
# NETWORK / RETRY SETTINGS
# ============================================================

# Maximum number of retry attempts per Telegram API call (after the first try)
MAX_RETRIES: int = 3

# Wait times in seconds between retry attempts (exponential backoff)
# Attempt 1 → wait 1s, Attempt 2 → wait 2s, Attempt 3 → wait 4s
RETRY_DELAYS: list = [1, 2, 4]

# HTTP request timeout in seconds for Telegram API calls
REQUEST_TIMEOUT: int = 10
