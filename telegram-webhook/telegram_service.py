"""
telegram_service.py — Reusable Telegram messaging module.

Provides:
    send_message()      Send a text message (with optional inline keyboard or force_reply)
    edit_message()      Edit an existing message in place
    delete_message()    Delete a message
    escape_markdown()   Safely escape Telegram Markdown v1 special characters

Features:
    - Retry logic with exponential backoff (configurable in config.py)
    - Distinguishes permanent errors (don't retry) from transient errors (retry)
    - Structured logging with timestamps and log levels
    - Empty chat_id / empty text guards to prevent silent failures

HOW TO USE:
    This is the master copy in shared/. The deploy.sh script copies it into
    each Cloud Function folder before deployment. Import as:
        from telegram_service import send_message, escape_markdown

COMPATIBILITY:
    Python 3.9+  |  Google Cloud Functions Gen 1 & Gen 2
"""

import re
import time
import logging
from typing import Optional

import requests

# config.py must be in the same folder as this file at deploy time
from config import (
    TELEGRAM_API_BASE,
    MAX_RETRIES,
    RETRY_DELAYS,
    REQUEST_TIMEOUT,
    DEBUG_MODE,
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_service")


# ============================================================
# MARKDOWN HELPER
# ============================================================

def escape_markdown(text: str) -> str:
    """
    Escape Telegram Markdown v1 special characters in a string.

    Telegram Markdown v1 treats these characters as formatting:
        _  (italic)
        *  (bold)
        `  (code)
        [  (link start)

    If any of these appear unintentionally in task names, project names, or user
    names, they break the message formatting. Call this function before embedding
    any user-supplied or GCS-loaded string inside a Markdown message.

    Args:
        text: The raw string to escape.

    Returns:
        A string safe to embed in Telegram Markdown v1 messages.

    Example:
        escape_markdown("Task_Name [URGENT]")  →  "Task\\_Name \\[URGENT\\]"
        escape_markdown("John's *Project*")    →  "John's \\*Project\\*"
    """
    if not text:
        return ""
    # Only the four characters that Markdown v1 uses for formatting
    escape_chars = r"_*`["
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))


# ============================================================
# INTERNAL REQUEST HELPER
# ============================================================

def _make_request(
    bot_token: str,
    method: str,
    payload: dict,
    retries: int = MAX_RETRIES,
) -> Optional[dict]:
    """
    POST to the Telegram Bot API with retry logic and structured logging.

    Retry policy:
        - Permanent errors (HTTP 400, 401, 403, 404): stop immediately, no retry.
          These indicate configuration problems (bad token, bot blocked, chat not found).
        - Transient errors (HTTP 429 rate-limit, 5xx server errors, timeouts,
          network failures): retry with exponential backoff.

    Args:
        bot_token: Telegram bot token string.
        method:    Telegram API method name (e.g. "sendMessage", "deleteMessage").
        payload:   JSON-serializable dict of API parameters.
        retries:   Number of additional attempts after the first try.

    Returns:
        Parsed JSON response dict on success, or None on all failures.
    """
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"

    # HTTP status codes that mean a permanent, non-retryable error
    PERMANENT_HTTP_CODES = {400, 401, 403, 404}

    for attempt in range(retries + 1):
        try:
            if DEBUG_MODE:
                logger.debug(
                    f"[Attempt {attempt + 1}/{retries + 1}] "
                    f"POST {method} | chat_id={payload.get('chat_id', 'N/A')}"
                )

            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            logger.info(f"Telegram {method} → HTTP {response.status_code}")

            # Success
            if response.status_code == 200:
                return response.json()

            # Parse Telegram error details from response body
            try:
                error_body = response.json()
                error_code = error_body.get("error_code", response.status_code)
                error_desc = error_body.get("description", "Unknown error")
            except Exception:
                error_code = response.status_code
                error_desc = response.text[:300]  # Limit length for readability

            # Permanent error — do not retry
            if response.status_code in PERMANENT_HTTP_CODES:
                logger.error(
                    f"Permanent Telegram error [{error_code}]: {error_desc} "
                    f"| method={method}, chat_id={payload.get('chat_id', 'N/A')}"
                )
                return None

            # Transient error — log and retry
            if attempt < retries:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(
                    f"Transient error [{error_code}]: {error_desc}. "
                    f"Retrying in {delay}s (attempt {attempt + 1}/{retries})..."
                )
                time.sleep(delay)

        except requests.exceptions.Timeout:
            logger.error(
                f"Request timed out on attempt {attempt + 1}/{retries + 1} "
                f"for method={method}"
            )
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])

        except requests.exceptions.ConnectionError as conn_err:
            logger.error(
                f"Connection error on attempt {attempt + 1}/{retries + 1} "
                f"for method={method}: {conn_err}"
            )
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])

        except Exception as exc:
            # Catch-all: log and abort (don't retry on unknown exceptions)
            logger.error(
                f"Unexpected error in _make_request (method={method}): {exc}"
            )
            return None

    logger.error(
        f"All {retries + 1} attempts failed for Telegram method: {method} "
        f"| chat_id={payload.get('chat_id', 'N/A')}"
    )
    return None


# ============================================================
# PUBLIC FUNCTIONS
# ============================================================

def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: str = "Markdown",
) -> Optional[dict]:
    """
    Send a text message to a Telegram chat (user, group, or channel).

    Args:
        bot_token:    Telegram bot token.
        chat_id:      Destination chat ID (string or int; groups are negative numbers).
        text:         Message text. Supports Telegram Markdown v1 by default.
                      Use escape_markdown() on any user-provided content embedded here.
        reply_markup: Optional dict — inline keyboard or force_reply.
                      Pass None to send a plain message.
        parse_mode:   "Markdown" (default) or "HTML".

    Returns:
        Telegram API response dict on success, or None on failure.

    Example:
        send_message(BOT_TOKEN, "-1001234567890", "⚠️ *Overdue task* detected!")
    """
    if not chat_id:
        logger.warning("send_message: chat_id is empty or None. Skipping.")
        return None

    if not text:
        logger.warning(
            f"send_message: text is empty for chat_id={chat_id}. Skipping."
        )
        return None

    payload: dict = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    result = _make_request(bot_token, "sendMessage", payload)

    if result:
        logger.info(f"✅ Message sent to chat_id={chat_id}")
    else:
        logger.error(f"❌ Failed to send message to chat_id={chat_id}")

    return result


def edit_message(
    bot_token: str,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: str = "Markdown",
) -> Optional[dict]:
    """
    Edit an existing Telegram message.

    Used to update inline-keyboard messages after a button is clicked,
    e.g. replacing "✅ Working / ⚠️ Delayed" buttons with a confirmation text.

    Args:
        bot_token:    Telegram bot token.
        chat_id:      Chat ID where the message is located.
        message_id:   ID of the message to edit (from the callback_query payload).
        text:         New text for the message.
        reply_markup: Optional new inline keyboard (pass None to remove keyboard).
        parse_mode:   "Markdown" or "HTML".

    Returns:
        Telegram API response dict or None on failure.
    """
    payload: dict = {
        "chat_id": str(chat_id),
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return _make_request(bot_token, "editMessageText", payload)


def delete_message(
    bot_token: str,
    chat_id: str,
    message_id: int,
) -> Optional[dict]:
    """
    Delete a Telegram message.

    Used after admin approves/rejects a pending user — deletes the approval
    keyboard message from the admin's chat.

    Args:
        bot_token:  Telegram bot token.
        chat_id:    Chat ID of the message to delete.
        message_id: ID of the message to delete.

    Returns:
        Telegram API response dict or None on failure.
    """
    payload: dict = {
        "chat_id": str(chat_id),
        "message_id": message_id,
    }
    return _make_request(bot_token, "deleteMessage", payload)
