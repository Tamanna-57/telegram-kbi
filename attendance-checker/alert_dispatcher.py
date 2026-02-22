"""
alert_dispatcher.py — Routes alerts to Telegram group and/or individual users.

Routing logic:
    1. Always sends to TELEGRAM_GROUP_CHAT_ID first (primary destination).
    2. If SEND_INDIVIDUAL_ALERTS=true, also sends to each individual chat_id.
    3. Prevents duplicate messages if individual_chat_id == group_chat_id.
    4. Logs success/failure for every dispatch attempt.

Exported functions:
    dispatch_alert()          Route one alert (group + optional single user)
    dispatch_admin_alert()    Route one alert (group + optional multiple admins)
    dispatch_batch_alerts()   Route a list of alerts in sequence with deduplication

HOW TO USE:
    This is the master copy in shared/. The deploy.sh script copies it into
    each Cloud Function folder before deployment. Import as:
        from alert_dispatcher import dispatch_alert, dispatch_admin_alert

COMPATIBILITY:
    Python 3.9+  |  Google Cloud Functions Gen 1 & Gen 2
"""

import logging
from typing import Optional, List, Dict, Any

# config.py and telegram_service.py must be in the same folder at deploy time
from config import (
    TELEGRAM_GROUP_CHAT_ID,
    SEND_INDIVIDUAL_ALERTS,
    DEBUG_MODE,
)
from telegram_service import send_message

# Only use an explicit group chat ID. Never fall back to a personal chat ID as a
# "group" — that causes silent failures when the personal chat owner hasn't started the bot.
EFFECTIVE_GROUP_CHAT_ID: str = TELEGRAM_GROUP_CHAT_ID

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("alert_dispatcher")


# ============================================================
# SINGLE ALERT DISPATCH
# ============================================================

def dispatch_alert(
    bot_token: str,
    message: str,
    individual_chat_id: Optional[str] = None,
    reply_markup: Optional[dict] = None,
    skip_group: bool = False,
) -> Dict[str, bool]:
    """
    Route a single alert to the Telegram group and optionally one individual user.

    Behavior:
        - Group message is always sent first (unless skip_group=True or group ID missing).
        - Individual message is sent only when BOTH of these are true:
            1. SEND_INDIVIDUAL_ALERTS=true  (env var / config flag)
            2. individual_chat_id is provided and differs from the group chat_id
        - If individual_chat_id equals the group chat_id, the individual send is
          skipped to avoid the user receiving a duplicate message.

    Args:
        bot_token:           Telegram bot token (from config.BOT_TOKEN or env var).
        message:             Alert text. Markdown v1 supported.
        individual_chat_id:  Optional. The user's personal Telegram chat_id.
                             Only used when SEND_INDIVIDUAL_ALERTS=true.
        reply_markup:        Optional. Inline keyboard dict. Sent to both group
                             and individual if applicable.
        skip_group:          If True, the group message is skipped entirely.
                             Use this for private-only interactions (e.g. bot
                             replies, language change, voice transcription).

    Returns:
        dict: {
            "group_sent":      bool,  # True if group message succeeded
            "individual_sent": bool,  # True if individual message succeeded
        }

    Examples:
        # Group-only alert (default when SEND_INDIVIDUAL_ALERTS=false):
        dispatch_alert(BOT_TOKEN, "⚠️ *Task overdue:* Foundation Work")

        # Alert to group + individual (when SEND_INDIVIDUAL_ALERTS=true):
        dispatch_alert(BOT_TOKEN, "⚠️ *Task overdue*", individual_chat_id="12345")

        # Skip group, send only to individual (for private bot replies):
        dispatch_alert(BOT_TOKEN, "✅ Logged!", individual_chat_id="12345", skip_group=True)
    """
    results: Dict[str, bool] = {
        "group_sent": False,
        "individual_sent": False,
    }

    # ---- Step 1: Send to GROUP (primary destination) ----
    if not skip_group:
        if EFFECTIVE_GROUP_CHAT_ID:
            logger.info(f"Dispatching group alert → chat_id={EFFECTIVE_GROUP_CHAT_ID}")
            result = send_message(
                bot_token,
                EFFECTIVE_GROUP_CHAT_ID,
                message,
                reply_markup=reply_markup,
            )
            results["group_sent"] = result is not None

            if results["group_sent"]:
                logger.info(f"✅ Group alert sent → chat_id={EFFECTIVE_GROUP_CHAT_ID}")
            else:
                logger.error(
                    f"❌ Group alert FAILED → chat_id={EFFECTIVE_GROUP_CHAT_ID}. "
                    "Check TELEGRAM_GROUP_CHAT_ID / ADMIN_CHAT_ID env vars and bot membership."
                )
        else:
            logger.warning(
                "⚠️ Neither TELEGRAM_GROUP_CHAT_ID nor ADMIN_CHAT_ID is set. "
                "Group alert skipped."
            )

    # ---- Step 2: Optionally send to INDIVIDUAL user ----
    if SEND_INDIVIDUAL_ALERTS and individual_chat_id:
        # Prevent duplicate: skip individual if it IS the group
        if str(individual_chat_id) == str(EFFECTIVE_GROUP_CHAT_ID):
            logger.info(
                "Individual chat_id matches group chat_id — skipping duplicate send. "
                f"chat_id={individual_chat_id}"
            )
            # Count it as sent since the group already delivered the message
            results["individual_sent"] = results["group_sent"]
        else:
            logger.info(f"Dispatching individual alert → chat_id={individual_chat_id}")
            result = send_message(
                bot_token,
                individual_chat_id,
                message,
                reply_markup=reply_markup,
            )
            results["individual_sent"] = result is not None

            if results["individual_sent"]:
                logger.info(f"✅ Individual alert sent → chat_id={individual_chat_id}")
            else:
                logger.error(
                    f"❌ Individual alert FAILED → chat_id={individual_chat_id}"
                )
    elif not SEND_INDIVIDUAL_ALERTS:
        logger.info(
            "Individual alerts are DISABLED (SEND_INDIVIDUAL_ALERTS=false). "
            "Set env var SEND_INDIVIDUAL_ALERTS=true to enable."
        )

    return results


# ============================================================
# ADMIN MULTI-RECIPIENT ALERT DISPATCH
# ============================================================

def dispatch_admin_alert(
    bot_token: str,
    message: str,
    admin_contacts: Dict[str, Dict],
    reply_markup: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Send an alert to the Telegram group, then optionally to all admin chat_ids.

    Used by the attendance-discrepancy-checker to notify multiple admins.
    The group always receives one message. Individual admins receive the message
    only when SEND_INDIVIDUAL_ALERTS=true.

    Args:
        bot_token:      Telegram bot token.
        message:        Alert text (Markdown v1 supported).
        admin_contacts: Dict mapping admin email → user record dict.
                        Each record may contain 'telegram_chat_id' and 'name'.
        reply_markup:   Optional inline keyboard dict.

    Returns:
        dict: {
            "group_sent":              bool,
            "individual_sent_count":   int,
            "individual_failed_count": int,
        }

    Example:
        admin_contacts = {
            "admin@example.com": {"name": "Admin", "telegram_chat_id": "999"},
        }
        dispatch_admin_alert(BOT_TOKEN, "🚨 Discrepancy found!", admin_contacts)
    """
    results: Dict[str, Any] = {
        "group_sent": False,
        "individual_sent_count": 0,
        "individual_failed_count": 0,
    }

    # ---- Send to GROUP first ----
    if EFFECTIVE_GROUP_CHAT_ID:
        logger.info(f"Dispatching admin group alert → chat_id={EFFECTIVE_GROUP_CHAT_ID}")
        result = send_message(
            bot_token,
            EFFECTIVE_GROUP_CHAT_ID,
            message,
            reply_markup=reply_markup,
        )
        results["group_sent"] = result is not None

        if results["group_sent"]:
            logger.info(f"✅ Admin group alert sent → chat_id={EFFECTIVE_GROUP_CHAT_ID}")
        else:
            logger.error(
                f"❌ Admin group alert FAILED → chat_id={EFFECTIVE_GROUP_CHAT_ID}"
            )
    else:
        logger.warning(
            "⚠️ TELEGRAM_GROUP_CHAT_ID is not set. Admin group alert skipped. "
            "Alerts will be sent directly to individual admin chat IDs from users.json."
        )

    # ---- Send to each admin individually ----
    # Always send to individuals when no group is configured (so alerts still reach someone).
    # When a group IS configured, respect the SEND_INDIVIDUAL_ALERTS flag.
    no_group_configured = not EFFECTIVE_GROUP_CHAT_ID
    if not SEND_INDIVIDUAL_ALERTS and not no_group_configured:
        logger.info(
            "Individual admin alerts DISABLED (SEND_INDIVIDUAL_ALERTS=false). "
            "Set env var SEND_INDIVIDUAL_ALERTS=true to also send to individuals when a group is configured."
        )
        logger.info(
            f"Admin alert summary: group_sent={results['group_sent']}, "
            f"individual_sent=0 (disabled)"
        )
        return results

    for email, contact in admin_contacts.items():
        chat_id = contact.get("telegram_chat_id")
        name = contact.get("name", email)

        if not chat_id:
            logger.warning(f"⚠️ No telegram_chat_id for admin {name} ({email}). Skipping.")
            continue

        # Skip if admin's personal chat_id is the same as the group (duplicate)
        if str(chat_id) == str(EFFECTIVE_GROUP_CHAT_ID):
            logger.info(
                f"Admin {name} ({email}) chat_id matches group — skipping duplicate."
            )
            results["individual_sent_count"] += 1
            continue

        result = send_message(bot_token, chat_id, message, reply_markup=reply_markup)

        if result:
            logger.info(f"✅ Individual admin alert sent → {name} ({email})")
            results["individual_sent_count"] += 1
        else:
            logger.error(f"❌ Individual admin alert FAILED → {name} ({email})")
            results["individual_failed_count"] += 1

    logger.info(
        f"Admin alert summary: group_sent={results['group_sent']}, "
        f"individual_sent={results['individual_sent_count']}, "
        f"individual_failed={results['individual_failed_count']}"
    )
    return results


# ============================================================
# BATCH ALERT DISPATCH
# ============================================================

def dispatch_batch_alerts(
    bot_token: str,
    alerts: List[Dict],
    deduplicate: bool = True,
) -> List[Dict[str, bool]]:
    """
    Dispatch a list of alerts in sequence with optional group deduplication.

    If deduplicate=True (default), the same message text is sent to the group
    only once, even if multiple users triggered the same alert. Each user still
    receives their individual message (if SEND_INDIVIDUAL_ALERTS=true).

    Each alert dict must have:
        "message"            (str, required)
        "individual_chat_id" (str, optional)
        "reply_markup"       (dict, optional)

    Args:
        bot_token:    Telegram bot token.
        alerts:       List of alert dicts (see above).
        deduplicate:  Whether to deduplicate group messages. Default: True.

    Returns:
        List of result dicts (same order as input alerts).

    Example:
        alerts = [
            {"message": "⚠️ Overdue: Task A", "individual_chat_id": "111"},
            {"message": "⚠️ Overdue: Task B", "individual_chat_id": "222"},
        ]
        results = dispatch_batch_alerts(BOT_TOKEN, alerts)
    """
    all_results: List[Dict[str, bool]] = []

    # Track which messages have already been sent to the group
    group_sent_messages: set = set()

    for alert in alerts:
        message = alert.get("message", "")
        chat_id = alert.get("individual_chat_id")
        markup = alert.get("reply_markup")

        # Skip group send if this exact message was already dispatched to the group
        skip_group = deduplicate and (message in group_sent_messages)

        result = dispatch_alert(
            bot_token=bot_token,
            message=message,
            individual_chat_id=chat_id,
            reply_markup=markup,
            skip_group=skip_group,
        )

        # Record group send so we can deduplicate future identical messages
        if result.get("group_sent"):
            group_sent_messages.add(message)

        all_results.append(result)

    sent_count = sum(
        1 for r in all_results if r.get("group_sent") or r.get("individual_sent")
    )
    logger.info(
        f"Batch dispatch complete: {sent_count}/{len(alerts)} alerts delivered."
    )
    return all_results
