"""
Cloud Function: Gantt Chart Alert Service  (telegram-1)
Entry point: analyze_gantt_charts_telegram

Triggered by: Cloud Scheduler (daily)

What this function does (UNCHANGED):
    1. Loads registered users from GCS (users3.json)
    2. Iterates over Gantt chart JSON files in GCS (gantt_charts/)
    3. Detects overdue or slow-progress tasks
    4. Sends Telegram alerts for flagged tasks

What changed vs the original:
    - Messaging layer replaced:
        send_telegram() → alert_dispatcher.dispatch_alert()
        Language-switch card → telegram_service.send_message() (private, not a group alert)
    - Added validate_user_record() for safe GCS JSON field access
    - config.py / telegram_service.py / alert_dispatcher.py are now separate files

Business logic, GCS structure, scheduler behavior, JSON format: UNCHANGED.
"""

import os
import json
import re
import functions_framework
from datetime import datetime
from google.cloud import storage
from google.cloud import translate_v2 as translate

# Shared modules — copied into this folder by deploy.sh before deployment
from config import GCS_BUCKET_NAME, GANTT_CHARTS_GCS_FOLDER, USERS_FILE, BOT_TOKEN
from telegram_service import send_message, escape_markdown
from alert_dispatcher import dispatch_alert

# ============================================================
# TEXTS (bilingual: en / hi)  — UNCHANGED
# ============================================================
TEXTS = {
    "en": {
        "header":    "🚨 **Daily Alert**",
        "overdue":   "🔴 **Overdue**",
        "slow":      "⚠️ **Slow Progress**",
        "update":    "📝 **Update Required**",
        "btn_work":  "✅ Working",
        "btn_delay": "⚠️ Delayed",
        "lang_offer": "🌐 Change Language / भाषा बदलें",
        "btn_hi":    "🇮🇳 हिंदी (Hindi)",
        "btn_en":    "🇬🇧 English",
    },
    "hi": {
        "header":    "🚨 **डेली रिपोर्ट**",
        "overdue":   "🔴 **समय सीमा समाप्त (Overdue)**",
        "slow":      "⚠️ **धीमी प्रगति (Slow Progress)**",
        "update":    "📝 **अपडेट भेजें**",
        "btn_work":  "✅ काम चालू है",
        "btn_delay": "⚠️ लेट है",
        "lang_offer": "🌐 Change Language / भाषा बदलें",
        "btn_hi":    "🇮🇳 हिंदी (Hindi)",
        "btn_en":    "🇬🇧 English",
    },
}


def get_text(key: str, lang: str) -> str:
    """Return translated UI text; fall back to English if key or lang is missing."""
    lang = lang if lang in TEXTS else "en"
    return TEXTS[lang].get(key, TEXTS["en"].get(key, ""))


def google_translate_text(text: str, target_lang: str) -> str:
    """
    Translate text to Hindi using Google Translate API.
    Only translates when target_lang='hi'; returns original for 'en'.
    If translation fails, returns original text (safe fallback).
    """
    if target_lang == "en" or not text:
        return text
    try:
        client = translate.Client()
        result = client.translate(text, target_language="hi")
        return result["translatedText"]
    except Exception as exc:
        print(f"Translation failed (returning original): {exc}")
        return text


def normalize_name(name: str) -> str:
    """Strip non-alphanumeric chars and lowercase — used to match assignee to user."""
    if not name:
        return ""
    return re.sub(r"[^a-zA-Z0-9]", "", str(name)).lower()


# ============================================================
# GCS USER VALIDATION HELPER
# ============================================================

def validate_user_record(email: str, raw_record: dict) -> dict:
    """
    Return a safe, validated copy of a user record loaded from GCS.

    Handles missing or None fields gracefully so the function never crashes
    on a corrupted or partially filled users3.json entry.

    Fields validated:
        telegram_chat_id — must be a non-empty string; skipped if absent
        name             — defaults to email prefix if missing
        language         — defaults to "en" if missing or unrecognised
        alerts           — always initialised to an empty list (runtime-only)

    Args:
        email:      The key used in users3.json (e.g. "john@example.com")
        raw_record: The raw dict loaded from JSON.

    Returns:
        Cleaned dict, or None if the record has no usable telegram_chat_id.
    """
    if not isinstance(raw_record, dict):
        print(f"⚠️ Skipping user {email}: record is not a dict (got {type(raw_record)})")
        return None

    chat_id = raw_record.get("telegram_chat_id")

    # telegram_chat_id must exist and be non-empty
    if not chat_id or str(chat_id).strip() == "":
        print(f"⚠️ Skipping user {email}: missing telegram_chat_id")
        return None

    # Derive a display name — fall back to the email prefix
    name = raw_record.get("name")
    if not name or str(name).strip() == "":
        name = email.split("@")[0]
        print(f"⚠️ User {email}: missing name, using '{name}' as fallback")

    # Ensure language is a recognised value
    language = raw_record.get("language", "en")
    if language not in TEXTS:
        print(f"⚠️ User {email}: unrecognised language '{language}', defaulting to 'en'")
        language = "en"

    return {
        **raw_record,           # Preserve all original fields (don't drop anything)
        "telegram_chat_id": str(chat_id).strip(),
        "name": str(name).strip(),
        "language": language,
        "alerts": [],           # Runtime list — populated during task analysis
    }


# ============================================================
# CLOUD FUNCTION ENTRY POINT  — signature UNCHANGED
# ============================================================

@functions_framework.http
def analyze_gantt_charts_telegram(request):
    """
    Main Cloud Function entry point.
    Triggered by Cloud Scheduler. Analyzes Gantt charts and sends Telegram alerts.
    """
    bot_token = BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("❌ TELEGRAM_BOT_TOKEN not set. Aborting.")
        return "Bot token missing", 500

    storage_client = storage.Client()
    bucket = storage_client.bucket(GCS_BUCKET_NAME)

    # ---- 1. LOAD & VALIDATE USERS ----
    try:
        raw_users = json.loads(bucket.blob(USERS_FILE).download_as_text())
    except Exception as exc:
        print(f"❌ Could not load {USERS_FILE}: {exc}")
        return "Users file not found", 500

    if not isinstance(raw_users, dict):
        print(f"❌ {USERS_FILE} is not a JSON object (dict). Got: {type(raw_users)}")
        return "Invalid users file format", 500

    # Build active_users: normalised name → validated record
    active_users: dict = {}
    for email, raw_record in raw_users.items():
        validated = validate_user_record(email, raw_record)
        if validated is None:
            continue  # validate_user_record already logged the reason
        key = normalize_name(validated["name"])
        active_users[key] = validated

    print(f"✅ Loaded {len(active_users)} active users with Telegram chat IDs")

    # ---- 2. PROCESS GANTT CHARTS  — logic UNCHANGED ----
    blobs = storage_client.list_blobs(GCS_BUCKET_NAME, prefix=GANTT_CHARTS_GCS_FOLDER)
    today = datetime.utcnow().date()

    for blob in blobs:
        if not blob.name.endswith(".json"):
            continue

        try:
            chart = json.loads(blob.download_as_text())
        except Exception as exc:
            print(f"⚠️ Could not parse Gantt file {blob.name}: {exc}. Skipping.")
            continue

        if not isinstance(chart, dict):
            print(f"⚠️ Gantt file {blob.name} is not a JSON object. Skipping.")
            continue

        project_name_raw = chart.get("name") or "Project"

        for task in chart.get("tasks", []):
            if not isinstance(task, dict):
                continue

            assignee = normalize_name(task.get("assignee", ""))

            # Try exact match, then partial match
            user = active_users.get(assignee)
            if not user:
                for n, u in active_users.items():
                    if assignee and (assignee in n or n in assignee):
                        user = u
                        break
            if not user:
                continue

            # Safe date parsing — skip task if dates are missing or malformed
            try:
                end_raw = task.get("endDate") or task.get("end")
                start_raw = task.get("startDate") or task.get("start")

                if not end_raw or not start_raw:
                    print(
                        f"⚠️ Task '{task.get('name', '?')}' missing start/end date. Skipping."
                    )
                    continue

                end_d = datetime.fromisoformat(str(end_raw)).date()
                start_d = datetime.fromisoformat(str(start_raw)).date()
            except (ValueError, TypeError) as exc:
                print(
                    f"⚠️ Task '{task.get('name', '?')}' has invalid date format: {exc}. Skipping."
                )
                continue

            # Safe progress parsing
            try:
                progress = int(task.get("progress", 0))
            except (ValueError, TypeError):
                progress = 0

            if progress >= 100:
                continue  # Task complete — no alert needed

            # Determine alert type — logic UNCHANGED
            alert_type = None
            if end_d <= today:
                alert_type = "overdue"
            else:
                dur = (end_d - start_d).days + 1
                ela = (today - start_d).days + 1
                if dur > 0 and (ela / dur) >= 0.5 and progress < 50:
                    alert_type = "slow"

            if alert_type:
                task_name = task.get("name") or "Task"
                user["alerts"].append({
                    "type_key": alert_type,
                    "task_raw": task_name,
                    "proj_raw": project_name_raw,
                    # Alphanumeric ref for callback_data (max 10 chars)
                    "ref": "".join(c for c in task_name if c.isalnum())[:10],
                    "date": str(end_d),
                })

    # ---- 3. SEND ALERTS ----
    for _, user in active_users.items():
        alerts = user["alerts"]
        if not alerts:
            continue

        lang = user.get("language", "en")
        chat_id = user["telegram_chat_id"]
        user_name = escape_markdown(user["name"])

        # --- A. SUMMARY LIST (sent to group + optionally individual) ---
        header = get_text("header", lang)
        msg_lines = [f"{header}: {user_name}"]

        for a in alerts[:5]:
            t_name = escape_markdown(google_translate_text(a["task_raw"], lang))
            t_type = get_text(a["type_key"], lang)
            msg_lines.append(f"{t_type}: {t_name}")

        summary_msg = "\n".join(msg_lines)

        # dispatch_alert: sends to group first, then optionally to the individual
        dispatch_alert(
            bot_token=bot_token,
            message=summary_msg,
            individual_chat_id=chat_id,
        )

        # --- B. TASK CARDS WITH INLINE BUTTONS ---
        for a in alerts[:5]:
            t_name = escape_markdown(google_translate_text(a["task_raw"], lang))
            t_proj = escape_markdown(google_translate_text(a["proj_raw"], lang))

            kb = {
                "inline_keyboard": [
                    [
                        {
                            "text": get_text("btn_work", lang),
                            "callback_data": f"UPD:WORK:{a['ref']}",
                        },
                        {
                            "text": get_text("btn_delay", lang),
                            "callback_data": f"UPD:DELAY:{a['ref']}",
                        },
                    ]
                ]
            }

            update_lbl = get_text("update", lang)
            card_txt = (
                f"{update_lbl}\n"
                f"Task: `{t_name}`\n"
                f"Proj: {t_proj}\n"
                f"Date: {a['date']}"
            )

            # Task cards also go through dispatch_alert (group + optional individual)
            dispatch_alert(
                bot_token=bot_token,
                message=card_txt,
                individual_chat_id=chat_id,
                reply_markup=kb,
            )

        # --- C. LANGUAGE SWITCH CARD ---
        # This is a personal preference control, not a system alert.
        # Always sent directly to the individual — never to the group.
        lang_kb = {
            "inline_keyboard": [
                [
                    {"text": get_text("btn_hi", lang), "callback_data": "LANG:HI"},
                    {"text": get_text("btn_en", lang), "callback_data": "LANG:EN"},
                ]
            ]
        }
        send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=get_text("lang_offer", lang),
            reply_markup=lang_kb,
        )

    return "Done", 200
