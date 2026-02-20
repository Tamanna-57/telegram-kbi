"""
Cloud Function: Interactive Telegram Bot Service  (telegramwebhook)
Entry point: telegram_bot

Triggered by: Telegram Webhook (HTTP POST from Telegram servers)

What this function does (UNCHANGED):
    1. /start command → welcome message
    2. Email login → links Telegram chat_id to user record in GCS
    3. Callback buttons:
       - LANG:HI / LANG:EN   → change user language preference
       - approve:/reject:     → admin approves/rejects pending users
       - UPD:WORK / UPD:DELAY / UPD:RSN → task status updates
    4. Voice note → Google Speech API → transcription → forwarded to admin
    5. Force-reply text → custom delay reason → forwarded to admin

What changed vs the original:
    - Messaging layer replaced:
        send_message()  → telegram_service.send_message()
        editMessageText → telegram_service.edit_message()
        deleteMessage   → telegram_service.delete_message()
    - Added validate_user_record() for safe GCS JSON field access
    - Admin notifications still go to ADMIN_CHAT_ID (private, not group alerts)
    - All private bot↔user interactions bypass group alerts (skip_group=True in
      dispatch_alert, or direct telegram_service.send_message calls)
    - config.py / telegram_service.py imported as modules

Business logic, GCS structure, callback_data formats, JSON format: UNCHANGED.
"""

import os
import json
import functions_framework
import traceback
from google.cloud import storage
from google.cloud import speech
from google.cloud import translate_v2 as translate

# Shared modules — copied into this folder by deploy.sh before deployment
from config import (
    GCS_BUCKET_NAME,
    USERS_FILE,
    PENDING_USERS_FILE,
    ADMIN_CHAT_ID,
    BOT_TOKEN,
    TELEGRAM_API_BASE,
)
from telegram_service import send_message, edit_message, delete_message, escape_markdown

# ============================================================
# DICTIONARY (bilingual)  — UNCHANGED
# ============================================================
TEXTS = {
    "en": {
        "welcome":        "👋 **Welcome!**\nPlease reply with your **Email ID** to login.",
        "lang_set":       "✅ Language set to **English**.",
        "not_logged_in":  "⚠️ **You are not logged in.**\nPlease type your **Email ID** to connect.",
        "pending":        "⏳ Account not found. Request sent to Admin.",
        "working":        "✅ **Status Logged: Working on it**",
        "delay_ask":      "⚠️ **Why is this delayed?**",
        "custom_prompt":  "🎙️ **Voice or Text:**\nReply to this message with a **Text** or **Voice Note**.",
        "voice_received": "🎤 Voice received. Transcribing...",
        "voice_success":  "✅ **Transcribed:** {text}",
        "custom_saved":   "✅ Reason recorded.",
        "btn_mat":        "📦 Material",
        "btn_man":        "👷 Manpower",
        "btn_mac":        "⚙️ Machine",
        "btn_des":        "🖌️ Design",
        "btn_oth":        "🎙️/📝 Other (Voice/Text)",
        "lang_btn":       "🇮🇳 हिंदी में देखें",
    },
    "hi": {
        "welcome":        "👋 **स्वागत है!**\nलॉगिन करने के लिए कृपया अपना **Email ID** भेजें।",
        "lang_set":       "✅ भाषा **हिंदी** सेट कर दी गई है।",
        "not_logged_in":  "⚠️ **आप लॉग इन नहीं हैं।**\nकनेक्ट करने के लिए अपना **Email ID** लिखें।",
        "pending":        "⏳ खाता नहीं मिला। एडमिन को रिक्वेस्ट भेजी गई है।",
        "working":        "✅ **स्थिति दर्ज: काम चालू है**",
        "delay_ask":      "⚠️ **यह काम लेट क्यों है?**",
        "custom_prompt":  "🎙️ **वॉयस या टेक्स्ट:**\nकारण बताने के लिए **मैसेज लिखें** या **वॉयस नोट** भेजें।",
        "voice_received": "🎤 ऑडियो मिला। हम इसे टेक्स्ट में बदल रहे हैं...",
        "voice_success":  "✅ **अनुवाद:** {text}",
        "custom_saved":   "✅ कारण नोट कर लिया गया है।",
        "btn_mat":        "📦 मटेरियल",
        "btn_man":        "👷 मैनपावर",
        "btn_mac":        "⚙️ मशीन",
        "btn_des":        "🖌️ डिजाइन",
        "btn_oth":        "🎙️/📝 अन्य (वॉयस/टेक्स्ट)",
        "lang_btn":       "🇬🇧 Switch to English",
    },
}


def get_text(key: str, lang: str = "en", **kwargs) -> str:
    """Return localised UI string; safely falls back to English."""
    lang = lang if lang in TEXTS else "en"
    text = TEXTS[lang].get(key, TEXTS["en"].get(key, ""))
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text


def google_translate_text(text: str, target_lang: str) -> str:
    """Translate text to target language using Google Translate (hi only)."""
    if target_lang == "en" or not text:
        return text
    try:
        client = translate.Client()
        result = client.translate(text, target_language="hi")
        return result["translatedText"]
    except Exception as exc:
        print(f"Translate Error: {exc}")
        return text


# ============================================================
# GCS HELPERS  — UNCHANGED (load_json / save_json)
# ============================================================

def load_json(bucket_name: str, file_name: str) -> dict:
    """
    Load a JSON file from GCS. Returns an empty dict on any error.
    Logs warnings for missing files or malformed JSON — never raises.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(file_name)

        if not blob.exists():
            print(f"⚠️ File {file_name} does not exist in bucket {bucket_name}. Returning {{}}.")
            return {}

        raw = blob.download_as_text()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"❌ {file_name} contains invalid JSON: {exc}. Returning {{}}.")
            return {}

        if not isinstance(data, dict):
            print(f"⚠️ {file_name} is not a JSON object (got {type(data)}). Returning {{}}.")
            return {}

        return data

    except Exception as exc:
        print(f"❌ Error loading {file_name}: {exc}")
        traceback.print_exc()
        return {}


def save_json(bucket_name: str, file_name: str, data: dict) -> bool:
    """
    Save a dict as JSON to GCS. Returns True on success, False on failure.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(file_name)
        blob.upload_from_string(json.dumps(data, indent=4))
        print(f"✅ Saved {file_name}")
        return True
    except Exception as exc:
        print(f"❌ Error saving {file_name}: {exc}")
        traceback.print_exc()
        return False


# ============================================================
# GCS USER VALIDATION HELPER
# ============================================================

def validate_user_record(email: str, raw_record: dict) -> dict:
    """
    Return a safe, validated copy of a user record loaded from GCS.

    Handles missing or None fields in users3.json without crashing the function.

    Fields validated:
        telegram_chat_id — must be a non-empty string
        name             — defaults to email prefix if missing
        language         — defaults to "en" if missing or unrecognised
        role             — defaults to "user" if missing

    Returns None if the record is not a dict (corrupted entry).
    """
    if not isinstance(raw_record, dict):
        print(f"⚠️ Skipping user {email}: record is not a dict (got {type(raw_record)})")
        return None

    chat_id = raw_record.get("telegram_chat_id")
    # chat_id may be legitimately absent for users who haven't logged in yet
    chat_id_clean = str(chat_id).strip() if chat_id else ""

    name = raw_record.get("name")
    if not name or str(name).strip() == "":
        name = email.split("@")[0]

    language = raw_record.get("language", "en")
    if language not in TEXTS:
        language = "en"

    return {
        **raw_record,
        "telegram_chat_id": chat_id_clean,
        "name": str(name).strip(),
        "language": language,
        "role": raw_record.get("role", "user"),
    }


# ============================================================
# VOICE TRANSCRIPTION HELPER  — UNCHANGED
# ============================================================

def transcribe_voice(file_id: str, bot_token: str, lang_code: str = "en"):
    """
    Download a Telegram voice note and transcribe it with Google Speech API.
    Returns the transcript string, or None on failure.
    """
    try:
        import requests  # local import keeps top-level imports minimal

        # 1. Get file path from Telegram
        file_info = requests.get(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile?file_id={file_id}"
        ).json()
        file_path = file_info["result"]["file_path"]

        # 2. Download voice content
        voice_data = requests.get(
            f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        ).content

        # 3. Transcribe with Google Speech API
        client = speech.SpeechClient()
        audio = speech.RecognitionAudio(content=voice_data)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=48000,
            language_code="hi-IN" if lang_code == "hi" else "en-US",
        )
        response = client.recognize(config=config, audio=audio)

        if response.results:
            return response.results[0].alternatives[0].transcript
        return None

    except Exception as exc:
        print(f"Voice transcription error: {exc}")
        traceback.print_exc()
        return None


# ============================================================
# CLOUD FUNCTION ENTRY POINT  — signature UNCHANGED
# ============================================================

@functions_framework.http
def telegram_bot(request):
    """
    Main Cloud Function entry point.
    Receives Telegram webhook POST requests and dispatches them.
    """
    try:
        print("=" * 60)
        print("FUNCTION STARTED")
        print("=" * 60)

        bot_token = BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            print("❌ ERROR: TELEGRAM_BOT_TOKEN not set!")
            return "Bot token missing", 500

        print(f"✅ Bot token found: {bot_token[:20]}...")

        if request.method != "POST":
            return "Only POST allowed", 405

        try:
            update = request.get_json()
        except Exception as exc:
            print(f"JSON parsing error: {exc}")
            return "Invalid JSON", 400

        if not update:
            return "Empty update", 200

        print(f"Received update keys: {list(update.keys())}")

        # ================================================================
        # 1. CALLBACK QUERY (button clicks)
        # ================================================================
        if "callback_query" in update:
            print("Processing callback_query")
            cb = update["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            data = cb.get("data", "")

            print(f"Callback data: {data}")

            # Load users and find current user's language
            users = load_json(GCS_BUCKET_NAME, USERS_FILE)
            user_email = None
            user_lang = "en"

            for email, raw_u in users.items():
                validated = validate_user_record(email, raw_u)
                if validated and str(validated.get("telegram_chat_id")) == str(chat_id):
                    user_email = email
                    user_lang = validated.get("language", "en")
                    break

            # ---- CHANGE LANGUAGE ----
            if data.startswith("LANG:"):
                new_lang = data.split(":")[1].lower()
                if user_email:
                    users[user_email]["language"] = new_lang
                    save_json(GCS_BUCKET_NAME, USERS_FILE, users)

                msg = (
                    "✅ **Language changed to Hindi**"
                    if new_lang == "hi"
                    else "✅ **Language changed to English**"
                )
                edit_message(bot_token, chat_id, msg_id, msg)

            # ---- ADMIN APPROVAL / REJECTION ----
            elif data.startswith("approve:") or data.startswith("reject:"):
                parts = data.split(":", 1)
                action = parts[0]
                email = parts[1] if len(parts) > 1 else ""

                pending = load_json(GCS_BUCKET_NAME, PENDING_USERS_FILE)

                if email in pending:
                    u_data = pending.pop(email)
                    pending_chat_id = u_data.get("telegram_chat_id", "")

                    if action == "approve":
                        # Validate before saving into users file
                        if not pending_chat_id:
                            print(f"⚠️ Pending user {email} has no telegram_chat_id. Approving anyway.")
                        users[email] = u_data
                        save_json(GCS_BUCKET_NAME, USERS_FILE, users)
                        if pending_chat_id:
                            send_message(bot_token, pending_chat_id, "✅ **Access Granted!**")
                    else:
                        if pending_chat_id:
                            send_message(bot_token, pending_chat_id, "❌ **Access Denied.**")

                    save_json(GCS_BUCKET_NAME, PENDING_USERS_FILE, pending)
                    delete_message(bot_token, chat_id, msg_id)
                else:
                    print(f"⚠️ Email {email} not found in pending_users. Already processed?")

            # ---- TASK STATUS UPDATES ----
            elif data.startswith("UPD:"):
                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "WORK":
                    task_ref = parts[2] if len(parts) > 2 else "?"
                    msg = get_text("working", user_lang) + f"\nRef: {escape_markdown(task_ref)}"
                    edit_message(bot_token, chat_id, msg_id, msg)

                elif action == "DELAY":
                    task_ref = parts[2] if len(parts) > 2 else "?"
                    kb = {
                        "inline_keyboard": [
                            [{"text": get_text("btn_mat", user_lang), "callback_data": f"UPD:RSN:MAT:{task_ref}"}],
                            [{"text": get_text("btn_man", user_lang), "callback_data": f"UPD:RSN:MAN:{task_ref}"}],
                            [{"text": get_text("btn_mac", user_lang), "callback_data": f"UPD:RSN:MAC:{task_ref}"}],
                            [{"text": get_text("btn_oth", user_lang), "callback_data": f"UPD:RSN:OTH:{task_ref}"}],
                        ]
                    }
                    edit_message(
                        bot_token, chat_id, msg_id,
                        get_text("delay_ask", user_lang),
                        reply_markup=kb,
                    )

                elif action == "RSN":
                    code = parts[2] if len(parts) > 2 else "?"
                    task_ref = parts[3] if len(parts) > 3 else "?"

                    if code == "OTH":
                        # Ask user to type/speak a custom reason (force_reply)
                        send_message(
                            bot_token,
                            chat_id,
                            get_text("custom_prompt", user_lang) + f"\nTask: {escape_markdown(task_ref)}",
                            reply_markup={"force_reply": True},
                        )
                    else:
                        send_message(bot_token, chat_id, get_text("custom_saved", user_lang))
                        if ADMIN_CHAT_ID:
                            send_message(
                                bot_token,
                                ADMIN_CHAT_ID,
                                f"⚠️ Delay: `{escape_markdown(task_ref)}` | Code: {code}",
                            )

            return "Callback OK", 200

        # ================================================================
        # 2. MESSAGES (text and voice)
        # ================================================================
        if "message" in update:
            print("Processing message")

            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "").strip()
            voice = msg.get("voice")

            print(f"Chat ID: {chat_id} | Text: '{text[:50]}' | Voice: {voice is not None}")

            # Load and validate users
            print("Loading users from GCS...")
            raw_users = load_json(GCS_BUCKET_NAME, USERS_FILE)
            print(f"Loaded {len(raw_users)} raw user records")

            user_lang = "en"
            user_name = "Unknown"
            is_logged_in = False

            for email, raw_u in raw_users.items():
                validated = validate_user_record(email, raw_u)
                if validated and str(validated.get("telegram_chat_id")) == str(chat_id):
                    is_logged_in = True
                    user_lang = validated.get("language", "en")
                    user_name = validated.get("name", "Unknown")
                    print(f"✅ User found: {email} | Lang: {user_lang}")
                    break

            print(f"Is logged in: {is_logged_in}")

            # ---- /START COMMAND ----
            if text.lower() == "/start":
                print("🎯 /START COMMAND DETECTED!")
                try:
                    if is_logged_in:
                        response_text = (
                            f"👋 Welcome back, {escape_markdown(user_name)}!\n\n"
                            "You're already logged in."
                        )
                    else:
                        response_text = get_text("welcome", "en")

                    result = send_message(bot_token, chat_id, response_text)
                    if result:
                        print("✅ /start processed successfully")
                    else:
                        print("❌ send_message returned None for /start")

                    return "Start OK", 200

                except Exception as exc:
                    print(f"❌ EXCEPTION in /start handler: {exc}")
                    traceback.print_exc()
                    return "Start Error", 500

            # ---- HANDLE FORCE-REPLY (custom delay reason) ----
            if msg.get("reply_to_message"):
                reply_blob = msg["reply_to_message"].get("text", "")

                if "Task: " in reply_blob:
                    task_ref = reply_blob.split("Task: ")[1].strip()
                    final_reason = ""

                    # A. Voice note
                    if voice:
                        send_message(bot_token, chat_id, get_text("voice_received", user_lang))
                        transcript = transcribe_voice(voice["file_id"], bot_token, user_lang)
                        if transcript:
                            final_reason = transcript
                            send_message(
                                bot_token,
                                chat_id,
                                get_text("voice_success", user_lang, text=escape_markdown(transcript)),
                            )
                        else:
                            send_message(bot_token, chat_id, "❌ Audio Error. Please type.")

                    # B. Text reply
                    elif text:
                        final_reason = text

                    # C. Forward final reason to admin
                    if final_reason:
                        send_message(bot_token, chat_id, get_text("custom_saved", user_lang))
                        if ADMIN_CHAT_ID:
                            admin_msg = (
                                f"⚠️ **DELAY REPORT**\n"
                                f"👤 **User:** {escape_markdown(user_name)}\n"
                                f"📌 **Task Ref:** `{escape_markdown(task_ref)}`\n"
                                f"📝 **Reason:** {escape_markdown(final_reason)}"
                            )
                            send_message(bot_token, ADMIN_CHAT_ID, admin_msg)

                    return "Reason Processed", 200

            # ---- EMAIL LOGIN ----
            if "@" in text and "." in text:
                print(f"Email detected: {text}")
                users = load_json(GCS_BUCKET_NAME, USERS_FILE)

                for email, raw_u in users.items():
                    if email.lower() == text.lower():
                        # Validate record before updating
                        validated = validate_user_record(email, raw_u)
                        if validated is None:
                            print(f"⚠️ User record for {email} is corrupted. Allowing login anyway.")

                        users[email]["telegram_chat_id"] = str(chat_id)
                        save_json(GCS_BUCKET_NAME, USERS_FILE, users)

                        name = (validated or raw_u).get("name", email.split("@")[0])
                        send_message(
                            bot_token,
                            chat_id,
                            f"✅ **Login Successful!**\nWelcome {escape_markdown(name)}",
                        )
                        print(f"✅ User logged in: {email}")
                        return "Login OK", 200

                # User not found → send to pending approval queue
                pending = load_json(GCS_BUCKET_NAME, PENDING_USERS_FILE)
                first_name = msg.get("from", {}).get("first_name", "Unknown")
                pending[text] = {
                    "email": text,
                    "telegram_chat_id": str(chat_id),
                    "name": first_name,
                }
                save_json(GCS_BUCKET_NAME, PENDING_USERS_FILE, pending)
                send_message(bot_token, chat_id, get_text("pending", "en"))

                # Notify admin with approval buttons
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Approve", "callback_data": f"approve:{text}"},
                            {"text": "❌ Reject",  "callback_data": f"reject:{text}"},
                        ]
                    ]
                }
                if ADMIN_CHAT_ID:
                    send_message(
                        bot_token,
                        ADMIN_CHAT_ID,
                        f"🔔 **New User Request:** `{escape_markdown(text)}`",
                        reply_markup=kb,
                    )
                print(f"New user pending approval: {text}")
                return "Pending OK", 200

            # ---- NOT LOGGED IN ----
            if not is_logged_in:
                send_message(bot_token, chat_id, get_text("not_logged_in", "en"))
                return "Not logged in", 200

        print("No message or callback_query found in update")
        return "OK", 200

    except Exception as exc:
        print(f"❌ CRITICAL ERROR in telegram_bot: {exc}")
        traceback.print_exc()
        return "Internal Error", 500
