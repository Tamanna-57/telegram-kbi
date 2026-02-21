"""
Cloud Function: Attendance Discrepancy Detector
Entry point: check_attendance_discrepancies

Triggered by: Cloud Scheduler (daily at 7:00 PM IST)

What this function does (UNCHANGED):
    1. Reads function config from cloud_functions_settings.json
    2. Compares Press Shop operations data vs HR attendance records
    3. Detects two types of discrepancies:
       - Press PRESENT, HR ABSENT
       - Press ABSENT, HR PRESENT
    4. Sends HTML email to all admin/super_admin users
    5. Sends Telegram alerts to admins

What changed vs the original:
    - Telegram messaging layer replaced:
        send_telegram_message()  → telegram_service.send_message()
        send_telegram_alerts()   → alert_dispatcher.dispatch_admin_alert()
    - Added validate_admin_contacts() for safe GCS user JSON field access
    - Business logic, email logic, GCS paths, JSON formats: UNCHANGED

GCP Deployment:
    Trigger=HTTP, Runtime=Python 3.9+, Entry point=check_attendance_discrepancies
    Environment variables: MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD,
                           MAIL_DEFAULT_SENDER, TELEGRAM_BOT_TOKEN,
                           TELEGRAM_GROUP_CHAT_ID, SEND_INDIVIDUAL_ALERTS
"""

import json
import os
import smtplib
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Set, Tuple

import functions_framework
from google.cloud import storage

# Shared modules — copied into this folder by deploy.sh before deployment
from config import (
    GCS_BUCKET,
    EMAIL_SETTINGS_PATH,
    USERS_FILE_ATTENDANCE,
    HR_ATTENDANCE_DIR,
    PRESS_MASTER_MAINTENANCE,
    PRESS_MASTER_PRODUCTION,
    CLOUD_FUNCTIONS_SETTINGS_PATH,
    BOT_TOKEN,
)
from telegram_service import send_message  # for direct one-off sends
from alert_dispatcher import dispatch_admin_alert

# ============================================================
# EMAIL CONFIGURATION  — UNCHANGED
# ============================================================
MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER")

# Telegram bot token (same token across all services)
TELEGRAM_BOT_TOKEN = BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN")

# Cloud Functions shared settings key for this function
FUNCTION_KEY = "check_attendance_discrepancy"


# ============================================================
# GCS HELPERS  — UNCHANGED
# ============================================================

def get_gcs_client():
    return storage.Client()


def read_json_from_gcs(bucket_name: str, blob_path: str) -> dict:
    """Read JSON file from GCS. Raises FileNotFoundError if missing."""
    client = get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    if not blob.exists():
        raise FileNotFoundError(f"File not found: gs://{bucket_name}/{blob_path}")

    content = blob.download_as_text()
    return json.loads(content)


# ============================================================
# FUNCTION CONFIG  — UNCHANGED
# ============================================================

def get_function_config() -> dict:
    """
    Read this function's config from cloud_functions_settings.json.
    Returns safe defaults if file is missing or key is absent.
    """
    defaults = {"enabled": True, "settings": {"lag_days": 5}}
    try:
        config = read_json_from_gcs(GCS_BUCKET, CLOUD_FUNCTIONS_SETTINGS_PATH)

        if not isinstance(config, dict):
            print(f"⚠️ {CLOUD_FUNCTIONS_SETTINGS_PATH} is not a dict. Using defaults.")
            return defaults

        func_config = config.get(FUNCTION_KEY, {})

        if not isinstance(func_config, dict):
            print(f"⚠️ Config for '{FUNCTION_KEY}' is not a dict. Using defaults.")
            return defaults

        return {
            "enabled": func_config.get("enabled", defaults["enabled"]),
            "settings": func_config.get("settings", defaults["settings"]),
        }
    except FileNotFoundError:
        print(f"⚠️ {CLOUD_FUNCTIONS_SETTINGS_PATH} not found. Using defaults.")
        return defaults
    except json.JSONDecodeError as exc:
        print(f"⚠️ {CLOUD_FUNCTIONS_SETTINGS_PATH} has invalid JSON: {exc}. Using defaults.")
        return defaults
    except Exception as exc:
        print(f"⚠️ Error reading function config: {exc}. Using defaults.")
        return defaults


def get_lag_days() -> int:
    """
    Get lag_days from cloud_functions_settings.json,
    falling back to email_notification_settings.json.
    """
    default_lag = 5

    try:
        config = get_function_config()
        lag = config.get("settings", {}).get("lag_days")
        if lag is not None:
            return int(lag)
    except Exception:
        pass

    try:
        settings = read_json_from_gcs(GCS_BUCKET, EMAIL_SETTINGS_PATH)
        return int(settings.get("hr_backdated_new_entry", {}).get("lag_days", default_lag))
    except Exception:
        return default_lag


# ============================================================
# ADMIN CONTACTS  — with validation
# ============================================================

def get_admin_contacts() -> Dict[str, Dict]:
    """
    Load all admin and super_admin users from users.json.
    Validates each record for missing fields before returning.

    Returns:
        Dict mapping email → {email, telegram_chat_id, name, role}
    """
    try:
        users = read_json_from_gcs(GCS_BUCKET, USERS_FILE_ATTENDANCE)
    except FileNotFoundError:
        print(f"⚠️ {USERS_FILE_ATTENDANCE} not found. No admin contacts available.")
        return {}
    except json.JSONDecodeError as exc:
        print(f"❌ {USERS_FILE_ATTENDANCE} has invalid JSON: {exc}. No admin contacts.")
        return {}
    except Exception as exc:
        print(f"❌ Error loading {USERS_FILE_ATTENDANCE}: {exc}")
        return {}

    if not isinstance(users, dict):
        print(f"⚠️ {USERS_FILE_ATTENDANCE} is not a dict. No admin contacts.")
        return {}

    admin_contacts: Dict[str, Dict] = {}

    for email, user_data in users.items():
        # Skip corrupted records
        if not isinstance(user_data, dict):
            print(f"⚠️ Skipping user {email}: record is not a dict")
            continue

        role = user_data.get("role", "")
        is_active = user_data.get("is_active", True)

        if not is_active or role not in {"admin", "super_admin"}:
            continue

        # Safe field extraction with fallbacks
        name = user_data.get("name")
        if not name or str(name).strip() == "":
            name = email.split("@")[0]
            print(f"⚠️ Admin {email}: missing name, using '{name}'")

        chat_id = user_data.get("telegram_chat_id")
        chat_id_clean = str(chat_id).strip() if chat_id else ""

        if not chat_id_clean:
            print(f"⚠️ Admin {email} ({name}): no telegram_chat_id — Telegram alerts will be skipped for this user")

        admin_contacts[email] = {
            "email": email,
            "telegram_chat_id": chat_id_clean,
            "name": str(name).strip(),
            "role": role,
        }

    print(f"✅ Found {len(admin_contacts)} admin/super_admin contact(s)")
    return admin_contacts


def get_admin_emails() -> List[str]:
    """Get admin email list (backward compatibility wrapper)."""
    return list(get_admin_contacts().keys())


# ============================================================
# ATTENDANCE DATA  — UNCHANGED
# ============================================================

def normalize_name(name: str) -> str:
    """Normalize name for comparison: uppercase, single spaces, trimmed."""
    if not name:
        return ""
    return " ".join(name.upper().split())


def get_press_present_operators(target_date: str) -> Set[str]:
    """
    Get set of normalized names who were PRESENT in press shop operations.
    Reads master files. Raises FileNotFoundError if no data found.
    """
    present_operators: Set[str] = set()
    master_files = [PRESS_MASTER_MAINTENANCE, PRESS_MASTER_PRODUCTION]

    for master_file_path in master_files:
        try:
            master_data = read_json_from_gcs(GCS_BUCKET, master_file_path)
        except FileNotFoundError:
            print(f"⚠️ Master file not found: {master_file_path}")
            continue
        except json.JSONDecodeError as exc:
            print(f"⚠️ Master file {master_file_path} has invalid JSON: {exc}. Skipping.")
            continue
        except Exception as exc:
            print(f"⚠️ Error reading {master_file_path}: {exc}")
            continue

        if not isinstance(master_data, list):
            print(f"⚠️ {master_file_path} is not a JSON array. Skipping.")
            continue

        for record in master_data:
            if not isinstance(record, dict):
                continue

            if record.get("plan_date") != target_date:
                continue

            operator_names_raw = record.get("operator_names", "")

            if isinstance(operator_names_raw, str):
                operator_names = [n.strip() for n in operator_names_raw.split(",")]
            elif isinstance(operator_names_raw, list):
                operator_names = operator_names_raw
            else:
                operator_names = []

            for operator_name in operator_names:
                normalized = normalize_name(operator_name)
                if normalized:
                    present_operators.add(normalized)

    if not present_operators:
        raise FileNotFoundError(
            f"No press shop operations found for {target_date} in master files: "
            f"{PRESS_MASTER_MAINTENANCE}, {PRESS_MASTER_PRODUCTION}"
        )

    return present_operators


def get_hr_attendance(target_date: str) -> Tuple[Dict[str, str], Set[str]]:
    """
    Get HR attendance for target date (Press Shop department only).
    Tries consolidated file first, falls back to individual files.
    """
    client = get_gcs_client()
    bucket = client.bucket(GCS_BUCKET)

    hr_records: Dict[str, str] = {}
    processed_codes: Set[str] = set()

    # --- Try consolidated file first ---
    consolidated_path = f"{HR_ATTENDANCE_DIR}/{target_date}.json"
    consolidated_blob = bucket.blob(consolidated_path)

    if consolidated_blob.exists():
        try:
            content = consolidated_blob.download_as_text()
            all_records = json.loads(content)

            if not isinstance(all_records, dict):
                print(f"⚠️ Consolidated file {consolidated_path} is not a dict. Falling back.")
            else:
                for employee_code, record in all_records.items():
                    if not isinstance(record, dict):
                        print(f"⚠️ Record for {employee_code} is not a dict. Skipping.")
                        continue

                    department = normalize_name(record.get("department", ""))
                    if department != "PRESS SHOP":
                        continue

                    worker_name = normalize_name(record.get("worker_name", ""))
                    status = str(record.get("status", "")).upper()

                    if worker_name and status:
                        hr_records[worker_name] = status
                        processed_codes.add(employee_code)

                print(
                    f"✅ Read {len(processed_codes)} Press Shop records "
                    f"from consolidated file: {consolidated_path}"
                )
                return hr_records, processed_codes

        except json.JSONDecodeError as exc:
            print(f"⚠️ Consolidated file {consolidated_path} has invalid JSON: {exc}. Falling back.")
        except Exception as exc:
            print(f"⚠️ Error reading {consolidated_path}: {exc}. Falling back to individual files.")

    # --- Fallback: individual files ---
    date_folder = f"{HR_ATTENDANCE_DIR}/{target_date}/"
    blobs = bucket.list_blobs(prefix=date_folder)

    for blob in blobs:
        if blob.name.endswith("/") or not blob.name.endswith(".json"):
            continue
        try:
            content = blob.download_as_text()
            record = json.loads(content)

            if not isinstance(record, dict):
                print(f"⚠️ {blob.name} is not a dict. Skipping.")
                continue

            department = normalize_name(record.get("department", ""))
            if department != "PRESS SHOP":
                continue

            worker_name = normalize_name(record.get("worker_name", ""))
            status = str(record.get("status", "")).upper()
            employee_code = record.get("employee_code", blob.name)

            if worker_name and status:
                hr_records[worker_name] = status
                processed_codes.add(employee_code)

        except json.JSONDecodeError as exc:
            print(f"⚠️ {blob.name} has invalid JSON: {exc}. Skipping.")
        except Exception as exc:
            print(f"⚠️ Error processing {blob.name}: {exc}")
            continue

    if processed_codes:
        print(f"✅ Read {len(processed_codes)} Press Shop records from {date_folder}")
    else:
        print(f"⚠️ No Press Shop attendance records found for {target_date}")

    return hr_records, processed_codes


def find_discrepancies(target_date: str) -> List[Dict[str, str]]:
    """
    Find attendance discrepancies between press shop and HR records.
    Returns list of discrepancy dicts.
    """
    discrepancies: List[Dict[str, str]] = []

    try:
        press_present = get_press_present_operators(target_date)
    except FileNotFoundError as exc:
        return [{"type": "FILE_MISSING", "message": str(exc), "date": target_date}]

    hr_records, _ = get_hr_attendance(target_date)

    # Case 1: Press PRESENT, HR ABSENT
    for present_name in press_present:
        if hr_records.get(present_name) == "ABSENT":
            discrepancies.append({
                "employee_name": present_name,
                "type": "Press PRESENT, HR ABSENT",
                "date": target_date,
                "press_status": "PRESENT (in operations)",
                "hr_status": "ABSENT",
            })

    # Case 2: Press ABSENT, HR PRESENT
    for worker_name, hr_status in hr_records.items():
        if hr_status == "PRESENT" and worker_name not in press_present:
            discrepancies.append({
                "employee_name": worker_name,
                "type": "Press ABSENT, HR PRESENT",
                "date": target_date,
                "press_status": "ABSENT (not in operations)",
                "hr_status": "PRESENT",
            })

    return discrepancies


# ============================================================
# MESSAGE FORMATTERS  — UNCHANGED
# ============================================================

def format_telegram_message(discrepancies: List[Dict[str, str]], target_date: str) -> str:
    """Format Telegram message (Markdown v1 format)."""
    if discrepancies and discrepancies[0].get("type") == "FILE_MISSING":
        return (
            f"⚠️ **ATTENDANCE FILE MISSING**\n"
            f"📅 **Date:** {target_date}\n"
            f"❌ **Error:** {discrepancies[0]['message']}\n"
            f"Please upload the press shop operations report file for this date.\n"
            f"🕐 _{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}_"
        )

    count = len(discrepancies)
    msg = (
        f"🚨 **ATTENDANCE DISCREPANCY ALERT**\n"
        f"📅 **Date:** {target_date}\n"
        f"📊 **Total Issues:** {count}\n"
    )

    type1 = [d for d in discrepancies if "Press PRESENT" in d["type"]]
    type2 = [d for d in discrepancies if "Press ABSENT" in d["type"]]

    if type1:
        msg += f"🔴 **Press PRESENT, HR ABSENT** ({len(type1)})\n"
        for idx, disc in enumerate(type1[:5], 1):
            msg += f"{idx}. {disc['employee_name']}\n"
        if len(type1) > 5:
            msg += f"   _...and {len(type1) - 5} more_\n"
        msg += "\n"

    if type2:
        msg += f"🔵 **Press ABSENT, HR PRESENT** ({len(type2)})\n"
        for idx, disc in enumerate(type2[:5], 1):
            msg += f"{idx}. {disc['employee_name']}\n"
        if len(type2) > 5:
            msg += f"   _...and {len(type2) - 5} more_\n"
        msg += "\n"

    msg += (
        f"⚠️ **Action:** Please verify and correct these records.\n\n"
        f"🕐 _{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}_"
    )
    return msg


def format_email_body(discrepancies: List[Dict[str, str]], target_date: str) -> str:
    """Format HTML email body — UNCHANGED."""
    if discrepancies and discrepancies[0].get("type") == "FILE_MISSING":
        return f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .header {{ background-color: #dc3545; color: white; padding: 20px; border-radius: 5px; }}
                .content {{ padding: 20px; background-color: #f8f9fa; margin-top: 20px; border-radius: 5px; }}
                .error {{ color: #dc3545; font-weight: bold; }}
            </style>
        </head>
        <body>
            <div class="header"><h2>⚠️ Attendance File Missing Alert</h2></div>
            <div class="content">
                <p><strong>Date:</strong> {target_date}</p>
                <p class="error">{discrepancies[0]['message']}</p>
                <p>Please ensure the press shop operations report file is uploaded for this date.</p>
            </div>
            <hr>
            <p style="font-size: 12px; color: #666;">
                Automated alert — Attendance Discrepancy Detection System.<br>
                Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}
            </p>
        </body>
        </html>"""

    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .header {{ background-color: #ff6b6b; color: white; padding: 20px; border-radius: 5px; }}
            .summary {{ background-color: #fff3cd; padding: 15px; border-left: 4px solid #ffc107; margin: 20px 0; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
            th {{ background-color: #343a40; color: white; padding: 12px; text-align: left; }}
            td {{ padding: 12px; border-bottom: 1px solid #ddd; }}
            tr:hover {{ background-color: #f5f5f5; }}
            .mismatch-1 {{ background-color: #ffe6e6; }}
            .mismatch-2 {{ background-color: #e6f3ff; }}
            .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd;
                       font-size: 12px; color: #666; }}
        </style>
    </head>
    <body>
        <div class="header"><h2>🚨 Attendance Discrepancy Alert</h2></div>
        <div class="summary">
            <strong>Summary:</strong> {len(discrepancies)} discrepanc{'y' if len(discrepancies) == 1 else 'ies'}
            found for <strong>{target_date}</strong>
        </div>
        <table>
            <thead>
                <tr>
                    <th>#</th><th>Employee Name</th><th>Press Shop Status</th>
                    <th>HR Status</th><th>Mismatch Type</th>
                </tr>
            </thead>
            <tbody>"""

    for idx, disc in enumerate(discrepancies, 1):
        row_class = "mismatch-1" if "Press PRESENT" in disc["type"] else "mismatch-2"
        html += f"""
                <tr class="{row_class}">
                    <td>{idx}</td>
                    <td><strong>{disc['employee_name']}</strong></td>
                    <td>{disc['press_status']}</td>
                    <td>{disc['hr_status']}</td>
                    <td>{disc['type']}</td>
                </tr>"""

    html += f"""
            </tbody>
        </table>
        <div class="footer">
            <p><strong>Action Required:</strong> Please verify and correct the attendance records.</p>
            <p>Automated alert — Attendance Discrepancy Detection System.<br>
            Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}</p>
        </div>
    </body>
    </html>"""
    return html


# ============================================================
# EMAIL SENDER  — UNCHANGED
# ============================================================

def send_email(to_emails: List[str], subject: str, html_body: str):
    """Send HTML email via SMTP. Raises on failure."""
    if not to_emails:
        print("No admin emails found. Skipping email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_DEFAULT_SENDER
    msg["To"] = ", ".join(to_emails)
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as server:
            server.starttls()
            server.login(MAIL_USERNAME, MAIL_PASSWORD)
            server.send_message(msg)
        print(f"✅ Email sent to {len(to_emails)} admin(s)")
    except Exception as exc:
        print(f"❌ Failed to send email: {exc}")
        raise


# ============================================================
# CLOUD FUNCTION ENTRY POINT  — signature UNCHANGED
# ============================================================

@functions_framework.http
def check_attendance_discrepancies(request):
    """
    Main Cloud Function entry point.
    Triggered by Cloud Scheduler OR manually with ?date=YYYY-MM-DD.
    """
    try:
        print("=== Attendance Discrepancy Check Started ===")

        # Check admin dashboard enable/disable flag
        func_config = get_function_config()
        if not func_config.get("enabled", True):
            print("⚠️ Function DISABLED via admin dashboard. Skipping.")
            return {"status": "skipped", "message": "Function is disabled"}, 200

        # Manual date override via query param
        manual_date = None
        if request.args:
            manual_date = request.args.get("date")

        if manual_date:
            try:
                datetime.strptime(manual_date, "%Y-%m-%d")
                target_date = manual_date
                print(f"🔧 MANUAL MODE: using date {target_date}")
            except ValueError:
                return {
                    "status": "error",
                    "message": f"Invalid date format: {manual_date}. Use YYYY-MM-DD.",
                }, 400
        else:
            lag_days = get_lag_days()
            target_date = (datetime.now() - timedelta(days=lag_days)).strftime("%Y-%m-%d")
            print(f"⏰ AUTO MODE: lag_days={lag_days}, target_date={target_date}")

        # Find discrepancies
        discrepancies = find_discrepancies(target_date)

        if not discrepancies:
            print("✅ No discrepancies found. No notifications sent.")
            return {
                "status": "success",
                "message": "No discrepancies found",
                "target_date": target_date,
                "mode": "manual" if manual_date else "auto",
            }, 200

        print(f"⚠️ Found {len(discrepancies)} discrepanc{'y' if len(discrepancies) == 1 else 'ies'}")

        # Load admin contacts (with validation)
        admin_contacts = get_admin_contacts()
        admin_emails = [c["email"] for c in admin_contacts.values()]
        print(f"👥 Admin emails: {admin_emails}")

        # Build subject
        mode_prefix = "[MANUAL] " if manual_date else ""
        if discrepancies[0].get("type") == "FILE_MISSING":
            subject = f"{mode_prefix}⚠️ Attendance File Missing - {target_date}"
        else:
            count = len(discrepancies)
            subject = (
                f"{mode_prefix}🚨 Attendance Discrepancy Alert - {target_date} "
                f"({count} issue{'s' if count > 1 else ''})"
            )

        # Send EMAIL — non-fatal: Telegram still runs even if email fails
        email_sent_count = 0
        try:
            html_body = format_email_body(discrepancies, target_date)
            send_email(admin_emails, subject, html_body)
            email_sent_count = len(admin_emails)
        except Exception as email_exc:
            print(f"⚠️ Email failed (Telegram will still run): {email_exc}")

        # Send TELEGRAM — group first, optionally individual admins
        telegram_message = format_telegram_message(discrepancies, target_date)
        telegram_results = dispatch_admin_alert(
            bot_token=TELEGRAM_BOT_TOKEN,
            message=telegram_message,
            admin_contacts=admin_contacts,
        )

        print("=== Check Completed Successfully ===")

        return {
            "status": "success",
            "message": f"Found {len(discrepancies)} discrepancies",
            "target_date": target_date,
            "mode": "manual" if manual_date else "auto",
            "discrepancies_count": len(discrepancies),
            "notifications": {
                "email_sent": email_sent_count,
                "telegram_group_sent": telegram_results.get("group_sent", False),
                "telegram_individual_sent": telegram_results.get("individual_sent_count", 0),
                "telegram_individual_failed": telegram_results.get("individual_failed_count", 0),
            },
        }, 200

    except Exception as exc:
        error_msg = f"Error: {str(exc)}"
        print(f"❌ {error_msg}")
        traceback.print_exc()

        # Try to notify admins about the error
        try:
            admin_contacts = get_admin_contacts()
            admin_emails = [c["email"] for c in admin_contacts.values()]

            error_html = f"""
            <html><body>
                <h2 style="color: red;">❌ Attendance Check Failed</h2>
                <p><strong>Error:</strong> {str(exc)}</p>
                <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}</p>
            </body></html>"""
            send_email(admin_emails, "❌ Attendance Check System Error", error_html)

            error_telegram = (
                f"❌ **ATTENDANCE CHECK FAILED**\n"
                f"**Error:** {str(exc)}\n"
                f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}\n"
                "Please check Cloud Logs."
            )
            dispatch_admin_alert(
                bot_token=TELEGRAM_BOT_TOKEN,
                message=error_telegram,
                admin_contacts=admin_contacts,
            )
        except Exception as notify_exc:
            print(f"⚠️ Failed to send error notifications: {notify_exc}")

        return {"status": "error", "message": error_msg}, 500
