"""
Press Portal Blueprint - press_portal.py
Place this file in the same directory as your app.py
"""

import re

from datetime import datetime, timedelta

import logging

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash, current_app, Response, make_response, send_file
import json
from io import BytesIO
from datetime import datetime, timedelta
import time
from datetime import datetime, timedelta
import queue
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from functools import wraps
import uuid
import os
import logging
import pandas as pd
import io
from io import BytesIO
from flask import send_file
from decorators import service_access_required
from config import USERS_FILE  # Add this to import USERS_FILE from config
from press_production_planning import (
    get_machine_recommendations,
    parse_customer_schedule,
    match_parts_to_components,
    calculate_production_requirements,
    generate_daily_schedule,
    generate_multi_day_schedule
)
from config import (
    CUSTOMER_SCHEDULES_FOLDER,
    GENERATED_SCHEDULES_FOLDER,
    PART_MAPPING_FILE
)
# Create the blueprint
press_portal_bp = Blueprint('press_portal', __name__,
                           template_folder='templates/press_portal',
                           static_folder='static/press_portal',
                           url_prefix='/press')

# Configuration - will use same GCS bucket as main app
PRESSES_FILE = "press_portal/presses.json"
OPERATIONS_FILE_NAME = "norms/operations_data.json"  # Reuse from main app
OPERATORS_FILE_NAME = "norms/operators_data.json" # Reuse from main app
MACHINES_FILE_NAME = "norms/machines_data.json"  # Reuse from main app
COMMANDS_GCS_FOLDER = "press_portal/commands/" # New folder for command JSONs
COMMANDS_BUCKET_NAME = os.environ.get('COMMANDS_GCS_BUCKET_NAME', 'press_data_storage')
PRESSES_BUCKET_NAME = os.environ.get('GCS_BUCKET_NAME', 'kbi-first')  # Main app bucket for presses
IST = ZoneInfo("Asia/Kolkata")

# Production plan constants (JSON format in new folders)
PRESS_ENTRY_REPORTS_GCS_FOLDER = "press_entry_reports/"
MASTER_PRESS_ENTRY_REPORT_FILE = "press_entry_reports/master_press_entry_report.json"
ABSENTEES_PRESS_ENTRY_GCS_FOLDER = "absentees_press_entry/"
MASTER_ABSENTEES_JSON_FILE = "absentees_press_entry/master_absentees.json"

# EMQX Configuration - loaded from environment
MQTT_BROKER = os.environ.get('MQTT_BROKER', 'q66b7066.ala.asia-southeast1.emqxsl.com')
MQTT_TOPIC_PREFIX = "kbi/press/"
# Construct an absolute path to the certificate file
APP_ROOT = os.path.dirname(os.path.abspath(__file__))

# EMQX HTTP Publish API (stateless - no persistent connection needed)
# Use EMQX_API_KEY/SECRET if set, otherwise fall back to MQTT credentials
EMQX_API_KEY    = os.environ.get('EMQX_API_KEY')    or os.environ.get('MQTT_USERNAME', '')
EMQX_API_SECRET = os.environ.get('EMQX_API_SECRET') or os.environ.get('MQTT_PASSWORD', '')
EMQX_HTTP_PORT  = int(os.environ.get('EMQX_HTTP_PORT', 8443))
EMQX_PUBLISH_URL = f"https://{MQTT_BROKER}:{EMQX_HTTP_PORT}/api/v5/publish"

# --- GCS-Only Configuration ---
# No SSE or MQTT subscription - commands checked via GCS polling
MACHINE_DISCONNECT_TIMEOUT = 300  # 5 minutes in seconds (for detailed monitoring)
DASHBOARD_DISCONNECT_TIMEOUT = 1200  # 20 minutes in seconds (for dashboard tiles, 2x discovery interval)
COMMAND_CONFIRMATION_WINDOW = 10  # seconds to search for confirmation in GCS files
COMMAND_AUTO_CANCEL_TIMEOUT = 30   # 30 seconds - auto-cancel unconfirmed commands
REPORTS_BUCKET_NAME = 'kbi-daily-reports'

ALERT_THRESHOLDS = {
    'temperature': {
        'warning': 65,    # Celsius
        'critical': 80    # Celsius
    },
    'vibration': {
        'warning': 8,     # magnitude
        'critical': 12    # magnitude
    },
    'stroke_rate': {
        'low': 5,         # SPM - below this is considered idle
        'optimal': 15     # SPM - target rate
    }
}



# Helper Functions
def get_gcs_bucket(bucket_name=None):
    """Get a specific GCS bucket or the default one from the main app context."""
    from google.cloud import storage
    try:
        storage_client = storage.Client()
        bucket_to_get = bucket_name or os.environ.get('GCS_BUCKET_NAME', 'kbi-first')
        return storage_client.bucket(bucket_to_get)
    except Exception as e:
        print(f"Error accessing GCS bucket: {e}")
        print("=" * 60)
        print("GCS AUTHENTICATION ERROR - LOCAL DEVELOPMENT")
        print("=" * 60)
        print("If running locally, you need to set up GCS credentials:")
        print("")
        print("Option 1: Application Default Credentials (Recommended)")
        print("  gcloud auth application-default login")
        print("")
        print("Option 2: Service Account Key")
        print("  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json")
        print("")
        print("Option 3: Use the emulator for local testing")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return None

def read_from_gcs(file_path, bucket_name=None):
    """Read JSON data from GCS

    Args:
        file_path: Path to file in bucket
        bucket_name: Optional bucket name, defaults to COMMANDS_BUCKET_NAME
    """
    bucket = get_gcs_bucket(bucket_name or COMMANDS_BUCKET_NAME)
    if not bucket:
        print(f"Warning: Could not get GCS bucket for {file_path}")
        return None
    try:
        blob = bucket.blob(file_path)
        if blob.exists():
            content = blob.download_as_text()
            return json.loads(content)
        else:
            print(f"File not found in GCS: {file_path}")
            return None
    except Exception as e:
        print(f"Error reading from GCS {file_path}: {e}")


def read_from_gcs_fresh(file_path, bucket_name=None):
    """Read JSON data from GCS with cache control to ensure fresh data.

    This function explicitly reloads the blob to bypass any GCS client caching.
    Use this for data that must be current (like press_list.json on monitoring dashboard).

    Args:
        file_path: Path to file in bucket
        bucket_name: Optional bucket name, defaults to COMMANDS_BUCKET_NAME
    """
    bucket = get_gcs_bucket(bucket_name or COMMANDS_BUCKET_NAME)
    if not bucket:
        print(f"Warning: Could not get GCS bucket for {file_path}")
        return None
    try:
        blob = bucket.blob(file_path)
        # Reload blob metadata to ensure we have the latest version
        blob.reload()
        if blob.exists():
            # Download with explicit cache control
            content = blob.download_as_text()
            return json.loads(content)
        else:
            print(f"File not found in GCS: {file_path}")
            return None
    except Exception as e:
        print(f"Error reading fresh data from GCS {file_path}: {e}")
        import traceback
        traceback.print_exc()
        return None

def write_to_gcs(file_path, data, bucket_name=None):
    """Write JSON data to GCS

    Args:
        file_path: Path to file in bucket
        data: JSON data to write
        bucket_name: Optional bucket name, defaults to COMMANDS_BUCKET_NAME
    """
    bucket = get_gcs_bucket(bucket_name or COMMANDS_BUCKET_NAME)
    if not bucket:
        return False
    try:
        blob = bucket.blob(file_path)
        blob.upload_from_string(json.dumps(data, indent=2), content_type='application/json')
        return True
    except Exception as e:
        print(f"Error writing to GCS {file_path}: {e}")
        return False

# ==================== MACHINE SETTINGS HELPERS ====================

def is_user_admin(email):
    """
    Check if a user has admin or super_admin role.
    Returns True if user is admin or super_admin, False otherwise.
    """
    try:
        # Read from main app bucket (kbi-first) where users.json is stored
        bucket = get_gcs_bucket('kbi-first')
        if not bucket:
            return False
        
        blob = bucket.blob(USERS_FILE)
        if not blob.exists():
            return False
        
        users_data = json.loads(blob.download_as_text())
        
        if email in users_data:
            role = users_data[email].get('role', '')
            return role in ['admin', 'super_admin']
        
        return False
    except Exception as e:
        logging.error(f"Error checking admin status: {e}")
        return False


# REPLACE these three functions in your press_portal.py file
# (Lines 240-318 approximately)

# ==================== MACHINE SETTINGS HELPERS (CORRECTED) ====================


    
def login_required(f):
    """Decorator to require login - uses main app's session"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_email' not in session:
            # Save the requested URL for redirect after login
            session['next_url'] = request.url
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin access"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('press_admin_access'):
            flash('Admin access required for this page.', 'error')
            return redirect(url_for('press_portal.admin_login'))
        return f(*args, **kwargs)
    return decorated_function


def save_operation_command_to_gcs(press_name, command_data):
    """Saves the operation command JSON to GCS."""
    bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
    if not bucket:
        print("ERROR: GCS bucket not available. Cannot save command.")
        return False
    
    try:
        # Create a unique filename based on timestamp to ensure order
        timestamp = datetime.fromisoformat(command_data['timestamp'])
        filename = f"{timestamp.strftime('%Y-%m-%dT%H%M%S.%f')}.json"
        
        # Path in bucket: press_portal/commands/<press_name>/<filename>.json
        blob_path = f"{COMMANDS_GCS_FOLDER}{press_name}/{filename}"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(json.dumps(command_data, indent=2), content_type='application/json')
        return True
    except Exception as e:
        print(f"Error saving command to GCS: {e}")
        return False

def publish_mqtt_message(press_id, press_name, component, operation, operator_name=None, status="START", **kwargs):
    """Publish operation command to EMQX via HTTP Publish API (stateless — no persistent connection)."""
    import requests as _requests

    if not EMQX_API_KEY or not EMQX_API_SECRET:
        current_app.logger.error("EMQX_API_KEY / EMQX_API_SECRET not configured.")
        return None, False

    try:
        topic = f"factory/{press_name}/command"
        message = {
            "press_id": press_id,
            "component": component,
            "operation": operation,
            "timestamp": datetime.now(IST).isoformat(),
            "operator": operator_name or "Unknown",
            "status": status,
        }

        if kwargs.get('debounce_value') is not None:
            message['debounce_value'] = kwargs['debounce_value']
        if kwargs.get('frequency') is not None:
            message['frequency'] = kwargs['frequency']
        if kwargs.get('spm') is not None:
            message['spm'] = kwargs['spm']

        response = _requests.post(
            EMQX_PUBLISH_URL,
            auth=(EMQX_API_KEY, EMQX_API_SECRET),
            json={"topic": topic, "payload": json.dumps(message), "qos": 1},
            timeout=5
        )

        success = response.status_code in (200, 202)
        if not success:
            current_app.logger.error(f"EMQX HTTP publish failed: {response.status_code} {response.text}")
        return message, success

    except Exception as e:
        current_app.logger.error(f"Error publishing command via EMQX HTTP API: {e}")
        return None, False

# Configuration for raw data bucket
RAW_DATA_BUCKET_NAME = os.environ.get('RAW_DATA_GCS_BUCKET_NAME', 'press_data_storage')
RAW_DATA_PREFIX = 'press_data/'
STATUS_DATA_PREFIX = 'status/'  # MQTT ONLINE/OFFLINE status files
LOGS_DATA_PREFIX = 'logs/'      # ESP32 log files


def get_press_online_status(bucket, press_name):
    """
    Read authoritative ONLINE/OFFLINE status from status/latest.json written by the MQTT bridge.
    Returns 'ONLINE', 'OFFLINE', or 'UNKNOWN' if file doesn't exist.
    Falls back to timestamp-freshness check on UNKNOWN so the caller can decide.
    """
    try:
        status_path = f"{STATUS_DATA_PREFIX}press_name={press_name}/latest.json"
        blob = bucket.blob(status_path)
        if blob.exists():
            data = json.loads(blob.download_as_text())
            val = data.get('payload_value', 'UNKNOWN').upper()
            return val if val in ('ONLINE', 'OFFLINE') else 'UNKNOWN'
    except Exception as e:
        logging.warning(f"Could not read status file for {press_name}: {e}")
    return 'UNKNOWN'


# ==================== ROUTES ====================

@press_portal_bp.route('/')
@service_access_required('press_portal')
@login_required
def hub():
    """Hub page with navigation to all sections"""
    return render_template('press_hub.html')


@press_portal_bp.route('/operations')
@login_required
def operations_index():
    """Operations landing page showing all presses"""
    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []
    return render_template('press_portal.html', presses=presses)

@press_portal_bp.route('/operations/<press_id>')
@login_required
def operations_press_page(press_id):
    """Individual press operation page (QR code destination)"""
    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []
    press = next((p for p in presses if p['id'] == press_id), None)

    if not press:
        flash('Press not found', 'error')
        return redirect(url_for('press_portal.operations_index'))

    operations_data = []
    operator_names = []
    main_app_bucket = get_gcs_bucket('kbi-first')
    if main_app_bucket:
        ops_blob = main_app_bucket.blob(OPERATIONS_FILE_NAME)
        if ops_blob.exists():
            operations_data = json.loads(ops_blob.download_as_text())

        # Fetch operators from HR workers database instead of norms
        hr_blob = main_app_bucket.blob("hr_workers/hr_workers.json")
        if hr_blob.exists():
            hr_workers = json.loads(hr_blob.download_as_text())
            operator_names = sorted(set(
                w.get('operator_name', '').strip().upper()
                for w in hr_workers
                if w.get('is_active') and w.get('department') == 'Press Shop' and w.get('operator_name')
            ))

    return render_template('press_operation.html',
                           press=press,
                           operations_data=operations_data,
                           operators=operator_names,
                           mqtt_status=True)
# Backward compatibility redirect for old QR codes
@press_portal_bp.route('/press/<press_id>')
def press_page_redirect(press_id):
    """Redirect old URLs to new operations URLs"""
    return redirect(url_for('press_portal.operations_press_page', press_id=press_id), code=301)

@press_portal_bp.route('/api/start_operation', methods=['POST'])
def start_operation():
    """API endpoint to start an operation with multiple operators"""
    data = request.get_json()

    press_id = data.get('press_id')
    component = data.get('component')
    operation = data.get('operation')
    operator_names = data.get('operator_names')
    debounce_value = data.get('debounce_value')
    frequency = data.get('frequency')
    spm = data.get('spm')

    if not all([press_id, component, operation, operator_names]):
        return jsonify({"error": "Missing required fields"}), 400
    
    if not isinstance(operator_names, list) or len(operator_names) == 0:
        return jsonify({"error": "At least one operator must be selected"}), 400

    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []
    press = next((p for p in presses if p['id'] == press_id), None)
    if not press:
        return jsonify({"error": "Press not found"}), 404

    active_operators = get_active_operators()
    busy_operators = [op for op in operator_names if op in active_operators]
    
    if busy_operators:
        busy_details = [f"{op} (on {active_operators[op]['press_name']})" for op in busy_operators]
        logging.warning(f"Rejected operation start - busy operators: {busy_details}")
        return jsonify({
            "error": "Some operators are already assigned",
            "busy_operators": busy_details
        }), 400

    operators_str = ", ".join(operator_names)

    message_payload, mqtt_success = publish_mqtt_message(
        press_id, press['name'], component, operation, operators_str, status="START",
        debounce_value=debounce_value, frequency=frequency, spm=spm
    )

    if mqtt_success:
        command_timestamp = message_payload.get('timestamp') if message_payload else datetime.now(IST).isoformat()
        logging.info(f"Started operation on {press['name']} with operators: {operator_names}")
    
        return jsonify({
            "message": "Operation started successfully",
            "mqtt_sent": True,
            "command_timestamp": command_timestamp,
            "press_name": press['name'],
            "operators": operator_names
        }), 200
    else:
        return jsonify({
            "error": "Failed to send MQTT message",
            "mqtt_sent": False
        }), 500
    
@press_portal_bp.route('/api/stop_operation', methods=['POST'])
def stop_operation():
    """API endpoint to stop an operation"""
    data = request.get_json()

    press_id = data.get('press_id')
    component = data.get('component')
    operation = data.get('operation')
    operator_name = data.get('operator_name')

    if not all([press_id, component, operation, operator_name]):
        return jsonify({"error": "Missing required fields"}), 400

    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []
    press = next((p for p in presses if p['id'] == press_id), None)
    if not press:
        return jsonify({"error": "Press not found"}), 404

    message_payload, mqtt_success = publish_mqtt_message(
        press_id, press['name'], component, operation, operator_name, status="STOP"
    )

    if mqtt_success:
        command_timestamp = message_payload.get('timestamp') if message_payload else datetime.now(IST).isoformat()
        return jsonify({
            "message": "Operation stopped successfully",
            "mqtt_sent": True,
            "command_timestamp": command_timestamp,
            "press_name": press['name']
        }), 200
    else:
        return jsonify({
            "error": "Failed to send MQTT stop message",
            "mqtt_sent": False
        }), 500


@press_portal_bp.route('/api/command_status/<command_id>')
def get_command_status(command_id):
    """
    API endpoint to check the status of a specific command.
    Useful for polling fallback if SSE is not available.
    """
    with pending_confirmations_lock:
        cmd_data = pending_confirmations.get(command_id)

    if not cmd_data:
        return jsonify({"error": "Command not found or expired"}), 404

    # Check for timeout
    elapsed = (datetime.now(IST) - cmd_data['timestamp']).total_seconds()
    if elapsed > CONFIRMATION_TIMEOUT and not cmd_data['confirmed']:
        cmd_data['confirmed'] = True
        cmd_data['confirmation_status'] = 'timeout'
        cmd_data['confirmation_message'] = f"No confirmation received within {CONFIRMATION_TIMEOUT}s"

    return jsonify({
        "command_id": command_id,
        "press_name": cmd_data['press_name'],
        "expected_behavior": cmd_data['expected_behavior'],
        "confirmed": cmd_data['confirmed'],
        "status": cmd_data.get('confirmation_status'),
        "message": cmd_data.get('confirmation_message'),
        "elapsed_seconds": elapsed
    }), 200


# ==================== GCS-BASED POLLING ENDPOINTS ====================

@press_portal_bp.route('/api/press/live_status/<press_name>', methods=['GET'])
def get_press_live_status(press_name):
    """
    Get live press status from GCS for the individual Operations Page.

    Data sources:
    - status:       status/{name}/latest.json  (authoritative ONLINE/OFFLINE from MQTT LWT)
    - stroke_count: press_data/{name}/latest.json  (fast single-file read, no listing)
    - stroke_rate:  processed/{name}/YYYY-MM-DD.json  (5-min aggregate)

    Fallback: if status/latest.json missing, derives status from stroke data age.
    Fallback: if press_data/latest.json missing, falls back to listing dated files.
    """
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        now = datetime.now(IST)
        today_str = now.strftime('%Y-%m-%d')
        stroke_count = None
        timestamp_str = None
        last_updated = None

        # --- Stroke count: try latest.json first, fall back to listing dated files ---
        latest_raw_path = f"{RAW_DATA_PREFIX}press_name={press_name}/latest.json"
        latest_blob = bucket.blob(latest_raw_path)
        if latest_blob.exists():
            raw_data = json.loads(latest_blob.download_as_text())
            stroke_count_str = raw_data.get('stroke_count')
            stroke_count = int(stroke_count_str) if stroke_count_str is not None else None
            timestamp_str = raw_data.get('timestamp')
            last_updated = datetime.fromisoformat(timestamp_str) if timestamp_str else None
        else:
            # Fallback: search dated folders up to 7 days back
            for days_back in range(7):
                check_date = now - timedelta(days=days_back)
                prefix = f"{RAW_DATA_PREFIX}press_name={press_name}/month={check_date.month:02d}/day={check_date.day:02d}/"
                blobs = list(bucket.list_blobs(prefix=prefix))
                if blobs:
                    blobs.sort(key=lambda b: b.name, reverse=True)
                    raw_data = json.loads(blobs[0].download_as_text())
                    stroke_count_str = raw_data.get('stroke_count')
                    stroke_count = int(stroke_count_str) if stroke_count_str is not None else None
                    timestamp_str = raw_data.get('timestamp')
                    last_updated = datetime.fromisoformat(timestamp_str) if timestamp_str else None
                    break

        if stroke_count is None and last_updated is None:
            return jsonify({
                "press_name": press_name,
                "stroke_count": None,
                "stroke_rate": None,
                "last_updated": None,
                "status": "NO_DATA"
            }), 200

        # --- Status: read status/latest.json (ONLINE/OFFLINE from MQTT LWT) ---
        status = get_press_online_status(bucket, press_name)
        if status == 'UNKNOWN':
            # Fallback: derive from stroke data freshness if no status file yet
            if last_updated:
                age_s = (now - last_updated).total_seconds()
                status = 'ONLINE' if age_s <= MACHINE_DISCONNECT_TIMEOUT else 'OFFLINE'
            else:
                status = 'OFFLINE'

        # --- Stroke rate: processed file (5-min aggregate, can be a few minutes stale) ---
        stroke_rate = None
        daily_data = read_from_gcs(f"{PROCESSED_DATA_PREFIX}{press_name}/{today_str}.json")
        if daily_data:
            data_points = daily_data.get('data_points', [])
            if data_points:
                stroke_rate = data_points[-1].get('stroke_rate')

        return jsonify({
            "press_name": press_name,
            "stroke_count": stroke_count,
            "stroke_rate": stroke_rate,
            "last_updated": timestamp_str,
            "status": status
        }), 200

    except Exception as e:
        logging.error(f"Error getting live status for {press_name}: {e}")
        return jsonify({"error": str(e)}), 500


@press_portal_bp.route('/api/press/logs/<press_name>', methods=['GET'])
@login_required
def get_press_logs(press_name):
    """
    Fetch recent ESP32 log entries for a press from GCS.

    Query params:
    - after: ISO timestamp — only return logs strictly after this time (used for confirmation polling)
    - limit: max entries to return (default 50)

    Log files live at: logs/press_name={name}/month=MM/day=DD/HHMMSS.microseconds.json
    payload_value is a JSON string: {"lvl": "INFO", "msg": "Remote Command: System STARTED"}
    """
    after_str = request.args.get('after')
    limit = min(int(request.args.get('limit', 50)), 200)

    after_dt = None
    if after_str:
        try:
            after_dt = datetime.fromisoformat(after_str)
        except ValueError:
            return jsonify({"error": "Invalid 'after' timestamp format"}), 400

    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        now = datetime.now(IST)
        logs = []

        # Search today and yesterday to handle commands near midnight
        for days_back in range(2):
            check_date = now - timedelta(days=days_back)
            prefix = f"{LOGS_DATA_PREFIX}press_name={press_name}/month={check_date.month:02d}/day={check_date.day:02d}/"
            blobs = list(bucket.list_blobs(prefix=prefix))
            if not blobs:
                continue

            blobs.sort(key=lambda b: b.name, reverse=True)  # newest first

            for blob in blobs:
                try:
                    entry = json.loads(blob.download_as_text())
                    ts_str = entry.get('timestamp')
                    ts = datetime.fromisoformat(ts_str) if ts_str else None

                    if after_dt and ts and ts <= after_dt:
                        continue  # skip entries at or before the command timestamp

                    # payload_value is a JSON-encoded string: {"lvl":"INFO","msg":"..."}
                    raw_payload = entry.get('payload_value', '{}')
                    try:
                        payload = json.loads(raw_payload)
                        level = payload.get('lvl', 'INFO')
                        message = payload.get('msg', raw_payload)
                    except (json.JSONDecodeError, TypeError):
                        level = 'INFO'
                        message = str(raw_payload)

                    logs.append({
                        'timestamp': ts_str,
                        'level': level,
                        'message': message,
                    })

                    if len(logs) >= limit:
                        break
                except Exception:
                    continue

            if len(logs) >= limit:
                break

        return jsonify({
            "press_name": press_name,
            "logs": logs,
            "count": len(logs)
        }), 200

    except Exception as e:
        logging.error(f"Error fetching logs for {press_name}: {e}")
        return jsonify({"error": str(e)}), 500


@press_portal_bp.route('/api/command/confirm', methods=['POST'])
def mark_command_confirmed():
    """
    Simple endpoint to mark a command as confirmed.
    Called from frontend after client-side verification.

    Expects JSON: {
        "press_name": "press_machine_NP19",
        "command_timestamp": "2025-12-18T18:21:17+05:30",
        "confirmation_message": "Press started - count 0 → 5"
    }
    """
    try:
        data = request.get_json()
        press_name = data.get('press_name')
        command_timestamp = data.get('command_timestamp')
        confirmation_message = data.get('confirmation_message')

        if not all([press_name, command_timestamp, confirmation_message]):
            return jsonify({"error": "Missing required fields"}), 400

        # Parse timestamp
        cmd_time = datetime.fromisoformat(command_timestamp)

        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        # Find and update command file
        cmd_prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={cmd_time.month:02d}/day={cmd_time.day:02d}/"
        cmd_blobs = list(bucket.list_blobs(prefix=cmd_prefix))

        for cmd_blob in cmd_blobs:
            if cmd_blob.name.endswith('.json'):
                cmd_content = cmd_blob.download_as_text()
                cmd_data = json.loads(cmd_content)

                # Check if this is the matching command by timestamp proximity
                cmd_file_time = datetime.fromisoformat(cmd_data.get('timestamp'))
                if abs((cmd_file_time - cmd_time).total_seconds()) < 2:  # Within 2 seconds
                    # Update with confirmation
                    cmd_data['confirmed'] = True
                    cmd_data['confirmed_at'] = datetime.now(IST).isoformat()
                    cmd_data['confirmation_message'] = confirmation_message

                    # Write back
                    cmd_blob.upload_from_string(
                        json.dumps(cmd_data, indent=2),
                        content_type='application/json'
                    )
                    logging.info(f"[GCS] Command confirmed: {cmd_blob.name}")

                    return jsonify({
                        "success": True,
                        "message": "Command confirmed successfully"
                    }), 200

        return jsonify({"error": "Command not found"}), 404

    except Exception as e:
        logging.error(f"Error marking command as confirmed: {e}")
        return jsonify({"error": str(e)}), 500


@press_portal_bp.route('/api/command/cancel', methods=['POST'])
def mark_command_cancelled():
    """
    Endpoint to mark a command as cancelled.
    Called from frontend when user cancels a pending confirmation.

    Expects JSON: {
        "press_name": "press_machine_NP19",
        "command_timestamp": "2025-12-18T18:21:17+05:30"
    }
    """
    try:
        data = request.get_json()
        press_name = data.get('press_name')
        command_timestamp = data.get('command_timestamp')

        if not all([press_name, command_timestamp]):
            return jsonify({"error": "Missing required fields"}), 400

        # Parse timestamp
        cmd_time = datetime.fromisoformat(command_timestamp)

        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        # Find and update command file
        cmd_prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={cmd_time.month:02d}/day={cmd_time.day:02d}/"
        cmd_blobs = list(bucket.list_blobs(prefix=cmd_prefix))

        for cmd_blob in cmd_blobs:
            if cmd_blob.name.endswith('.json'):
                cmd_content = cmd_blob.download_as_text()
                cmd_data = json.loads(cmd_content)

                # Check if this is the matching command by timestamp proximity
                cmd_file_time = datetime.fromisoformat(cmd_data.get('timestamp'))
                if abs((cmd_file_time - cmd_time).total_seconds()) < 2:  # Within 2 seconds
                    # Update with cancellation
                    cmd_data['cancelled'] = True
                    cmd_data['cancelled_at'] = datetime.now(IST).isoformat()

                    # Write back
                    cmd_blob.upload_from_string(
                        json.dumps(cmd_data, indent=2),
                        content_type='application/json'
                    )
                    logging.info(f"[GCS] Command cancelled: {cmd_blob.name}")

                    return jsonify({
                        "success": True,
                        "message": "Command cancelled successfully"
                    }), 200

        return jsonify({"error": "Command not found"}), 404

    except Exception as e:
        logging.error(f"Error marking command as cancelled: {e}")
        return jsonify({"error": str(e)}), 500


# DEPRECATED: Complex confirmation check - replaced by client-side logic
# Keeping for backward compatibility but frontend now uses client-side confirmation
def check_command_confirmation(press_name, command_timestamp):
    """
    Check if a command is confirmed by examining RAW GCS stroke_data files SEQUENTIALLY.

    Logic (SEQUENTIAL FILE CHECKING):
    - Parse command timestamp
    - Get ALL raw data files for the day, sorted by timestamp
    - Find file immediately BEFORE command (baseline count)
    - For START: Check files AFTER command sequentially for ANY increment
    - For STOP: Check files AFTER command sequentially for count = 0
    - Keep checking indefinitely (frontend polls until confirmed or user cancels)
    - If confirmed: update command file in GCS with confirmation field

    Query params:
    - command_type: START or STOP
    """
    try:
        command_type = request.args.get('command_type', 'START').upper()

        # Parse command timestamp
        cmd_time = datetime.fromisoformat(command_timestamp)

        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        # Only read files within ±2 minutes of command (not entire day to avoid timeout)
        window_start = cmd_time - timedelta(minutes=2)
        window_end = cmd_time + timedelta(minutes=2)

        prefix = f"{RAW_DATA_PREFIX}press_name={press_name}/month={cmd_time.month:02d}/day={cmd_time.day:02d}/"

        blobs = list(bucket.list_blobs(prefix=prefix))

        # Read files within time window only
        all_files = []
        for blob in blobs:
            if blob.name.endswith('/'):
                continue
            try:
                content = blob.download_as_text()
                data = json.loads(content)
                file_time = datetime.fromisoformat(data.get('timestamp'))

                # Only include files within ±2 minute window
                if not (window_start <= file_time <= window_end):
                    continue

                stroke_count_str = data.get('stroke_count')
                stroke_count = int(stroke_count_str) if stroke_count_str is not None else None

                all_files.append({
                    'timestamp': file_time,
                    'stroke_count': stroke_count,
                    'blob_name': blob.name
                })
            except Exception as e:
                logging.warning(f"Error reading file {blob.name}: {e}")
                continue

        if not all_files:
            return jsonify({
                "confirmed": False,
                "message": "No data files found within 2 minutes of command - waiting for press data"
            }), 200

        # Sort by timestamp
        all_files.sort(key=lambda x: x['timestamp'])

        # Find files before and after command
        files_before = [f for f in all_files if f['timestamp'] < cmd_time]
        files_after = [f for f in all_files if f['timestamp'] >= cmd_time]

        if not files_after:
            return jsonify({
                "confirmed": False,
                "message": "No data files found after command - waiting for press to publish data"
            }), 200

        # Check confirmation based on command type
        confirmed = False
        confirmation_message = ""

        if command_type == 'STOP':
            # STOP: Check files AFTER command sequentially for count = 0
            for file in files_after:
                if file['stroke_count'] == 0:
                    confirmed = True
                    confirmation_message = f"Press stopped - count reset to 0 at {file['timestamp'].strftime('%H:%M:%S')}"
                    break

            if not confirmed:
                # Show current count in message
                latest_count = files_after[-1]['stroke_count']
                return jsonify({
                    "confirmed": False,
                    "message": f"Waiting for count to reset to 0 (current: {latest_count}, checked {len(files_after)} files)"
                }), 200

        elif command_type == 'START':
            # START: Get baseline count from last file before command
            baseline_count = None
            if files_before:
                # Get the last known count before command
                for file in reversed(files_before):
                    if file['stroke_count'] is not None:
                        baseline_count = file['stroke_count']
                        break

            # Check files AFTER command for ANY increment
            for file in files_after:
                if file['stroke_count'] is not None:
                    if baseline_count is None:
                        # No baseline, use first after file as baseline
                        baseline_count = file['stroke_count']
                        continue

                    if file['stroke_count'] > baseline_count:
                        confirmed = True
                        confirmation_message = f"Press started - count incremented from {baseline_count} to {file['stroke_count']}"
                        break

            if not confirmed:
                if baseline_count is None:
                    return jsonify({
                        "confirmed": False,
                        "message": f"Waiting for baseline count (checked {len(files_after)} files)"
                    }), 200
                else:
                    latest_count = files_after[-1]['stroke_count'] if files_after[-1]['stroke_count'] is not None else 'N/A'
                    return jsonify({
                        "confirmed": False,
                        "message": f"Waiting for count increment (baseline: {baseline_count}, current: {latest_count}, checked {len(files_after)} files)"
                    }), 200

        # If confirmed, update the command file in GCS
        # Update the command file in GCS (regardless of confirmation status)
        try:
            # Find and update command file
            cmd_prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={cmd_time.month:02d}/day={cmd_time.day:02d}/"
            cmd_blobs = list(bucket.list_blobs(prefix=cmd_prefix))

            for cmd_blob in cmd_blobs:
                if cmd_blob.name.endswith('.json'):
                    cmd_content = cmd_blob.download_as_text()
                    cmd_data = json.loads(cmd_content)

                    # Check if this is the matching command by timestamp proximity
                    cmd_file_time = datetime.fromisoformat(cmd_data.get('timestamp'))
                    if abs((cmd_file_time - cmd_time).total_seconds()) < 2:  # Within 2 seconds
                        # Update with confirmation status (true or false)
                        if confirmed:
                            cmd_data['confirmed'] = True
                            cmd_data['confirmed_at'] = datetime.now(IST).isoformat()
                            cmd_data['confirmation_message'] = confirmation_message
                        else:
                            # Mark as unconfirmed if not yet confirmed
                            if 'confirmed' not in cmd_data:
                                cmd_data['confirmed'] = False
                                cmd_data['last_checked'] = datetime.now(IST).isoformat()

                        # Write back
                        cmd_blob.upload_from_string(
                            json.dumps(cmd_data, indent=2),
                            content_type='application/json'
                        )
                        logging.info(f"[GCS] Updated command file: {cmd_blob.name} (confirmed={cmd_data['confirmed']})")
                        break
        except Exception as e:
            logging.error(f"Error updating command file: {e}")

        return jsonify({
            "confirmed": confirmed,
            "message": confirmation_message,
            "files_checked": len(all_files),
            "files_after_command": len(files_after)
        }), 200

    except Exception as e:
        logging.error(f"Error checking confirmation for {press_name}: {e}")
        return jsonify({"error": str(e)}), 500

# SNIPPET: Replace the mark_command_timeout function in press_portal.py
# Around line 850 in your current file

@press_portal_bp.route('/api/command/timeout', methods=['POST'])
def mark_command_timeout():
    """
    Mark a command as unconfirmed due to timeout.
    Called from frontend when confirmation polling times out.

    Expects JSON: {
        "press_name": "press_machine_NP19",
        "command_timestamp": "2025-12-18T18:21:17+05:30"
    }
    """
    try:
        data = request.get_json()
        press_name = data.get('press_name')
        command_timestamp = data.get('command_timestamp')

        if not all([press_name, command_timestamp]):
            return jsonify({"error": "Missing required fields"}), 400

        # Parse timestamp
        cmd_time = datetime.fromisoformat(command_timestamp)

        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        # Find and update command file
        cmd_prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={cmd_time.month:02d}/day={cmd_time.day:02d}/"
        cmd_blobs = list(bucket.list_blobs(prefix=cmd_prefix))

        command_found = False
        
        for cmd_blob in cmd_blobs:
            if cmd_blob.name.endswith('.json'):
                cmd_content = cmd_blob.download_as_text()
                cmd_data = json.loads(cmd_content)

                # Check if this is the matching command by timestamp proximity
                cmd_file_time = datetime.fromisoformat(cmd_data.get('timestamp'))
                if abs((cmd_file_time - cmd_time).total_seconds()) < 2:  # Within 2 seconds
                    command_found = True
                    
                    # Only mark as timeout if not already confirmed
                    if not cmd_data.get('confirmed', False):
                        # FIX 1: Set ALL timeout indicators consistently
                        cmd_data['confirmed'] = False
                        cmd_data['timeout_at'] = datetime.now(IST).isoformat()
                        cmd_data['auto_cancelled'] = True  # IMPORTANT: Set this flag
                        cmd_data['cancelled'] = False  # Distinguish from manual cancellation
                        cmd_data['confirmation_message'] = 'Auto-cancelled - no confirmation received within 5 minutes'
                        
                        # FIX 2: Add metadata for debugging
                        cmd_data['timeout_reason'] = 'client_side_5min_timeout'
                        cmd_data['timeout_method'] = 'automatic'

                        # Write back to GCS
                        cmd_blob.upload_from_string(
                            json.dumps(cmd_data, indent=2),
                            content_type='application/json'
                        )
                        
                        logging.info(f"[GCS] Command marked as timeout (auto_cancelled=True): {cmd_blob.name}")
                        
                        return jsonify({
                            "success": True,
                            "message": "Command marked as unconfirmed (timeout)",
                            "auto_cancelled": True,
                            "timeout_at": cmd_data['timeout_at']
                        }), 200
                    else:
                        # Command was already confirmed, don't mark as timeout
                        logging.info(f"[GCS] Command already confirmed, not marking as timeout: {cmd_blob.name}")
                        return jsonify({
                            "success": True,
                            "message": "Command already confirmed",
                            "auto_cancelled": False
                        }), 200

        if not command_found:
            logging.warning(f"Command not found for timeout marking: {press_name} at {command_timestamp}")
            return jsonify({"error": "Command not found"}), 404

    except Exception as e:
        logging.error(f"Error marking command as timeout: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 5000
    
# Add this new API endpoint after the mark_command_timeout function

@press_portal_bp.route('/api/command/check_timeout/<press_name>', methods=['GET'])
@login_required
def check_command_timeout(press_name):
    """
    Check for commands that have exceeded the 5-minute timeout and auto-cancel them.
    Called periodically from frontend.
    
    Returns: {
        "timed_out_commands": [...],
        "count": <number>
    }
    """
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        now = datetime.now(IST)
        timed_out = []

        # Check commands from last 24 hours
        for days_back in range(1):
            date = now - timedelta(days=days_back)
            cmd_prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={date.month:02d}/day={date.day:02d}/"
            
            try:
                cmd_blobs = list(bucket.list_blobs(prefix=cmd_prefix))
                
                for cmd_blob in cmd_blobs:
                    if not cmd_blob.name.endswith('.json'):
                        continue
                    
                    try:
                        cmd_content = cmd_blob.download_as_text()
                        cmd_data = json.loads(cmd_content)
                        
                        # Skip if already confirmed, cancelled, or timed out
                        if cmd_data.get('confirmed', False):
                            continue
                        if cmd_data.get('cancelled', False):
                            continue
                        if cmd_data.get('timeout_at'):
                            continue
                        
                        # Check if command has timed out (5 minutes)
                        cmd_time = datetime.fromisoformat(cmd_data.get('timestamp'))
                        elapsed = (now - cmd_time).total_seconds()
                        
                        if elapsed > COMMAND_AUTO_CANCEL_TIMEOUT:
                            # Auto-cancel this command
                            cmd_data['confirmed'] = False
                            cmd_data['timeout_at'] = now.isoformat()
                            cmd_data['auto_cancelled'] = True
                            cmd_data['confirmation_message'] = f'Auto-cancelled - no confirmation received within {COMMAND_AUTO_CANCEL_TIMEOUT // 60} minutes'
                            
                            # Write back to GCS
                            cmd_blob.upload_from_string(
                                json.dumps(cmd_data, indent=2),
                                content_type='application/json'
                            )
                            
                            logging.info(f"[AUTO-CANCEL] Command timed out: {cmd_blob.name}")
                            
                            timed_out.append({
                                'timestamp': cmd_data.get('timestamp'),
                                'payload': cmd_data.get('parsed_payload', cmd_data.get('payload_value', {}))
                            })
                    
                    except Exception as e:
                        logging.error(f"Error checking command timeout {cmd_blob.name}: {e}")
                        continue
            
            except Exception as e:
                logging.error(f"Error listing commands for timeout check: {e}")
                continue

        return jsonify({
            "timed_out_commands": timed_out,
            "count": len(timed_out),
            "check_timestamp": now.isoformat()
        }), 200

    except Exception as e:
        logging.error(f"Error checking command timeouts: {e}")
        return jsonify({"error": str(e)}), 500
@press_portal_bp.route('/admin/login', methods=['GET', 'POST'])
@service_access_required('press_portal')
@login_required
def admin_login():
    """Admin login for press management"""
    if request.method == 'POST':
        # Use platform passcode from main app
        passcode = request.form.get('passcode')
        platform_passcode = os.environ.get('PLATFORM_PASSCODE', 'ceo@KBI')
        
        if passcode == platform_passcode:
            session['press_admin_access'] = True
            flash('Admin access granted', 'success')
            return redirect(url_for('press_portal.admin_dashboard'))
        else:
            flash('Invalid passcode', 'error')
    
    return render_template('press_admin_login.html')

@press_portal_bp.route('/admin/logout')
def admin_logout():
    """Admin logout"""
    session.pop('press_admin_access', None)
    flash('Logged out from press admin', 'success')
    return redirect(url_for('press_shop_reports.press_shop_details'))

@press_portal_bp.route('/admin/dashboard')
@service_access_required('press_portal')
@login_required
@admin_required
def admin_dashboard():
    """Admin dashboard"""
    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []

    # Fetch recent commands from all presses (last 50 commands across ALL presses)
    recent_logs = []
    bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
    if bucket:
        try:
            # Get all press names from command folders
            press_names = set()
            blobs = bucket.list_blobs(prefix=COMMAND_DATA_PREFIX, delimiter='/')
            for prefix in blobs.prefixes:
                # prefix format: command/press_name=X/
                if 'press_name=' in prefix:
                    press_name = prefix.split('press_name=')[1].rstrip('/')
                    press_names.add(press_name)

            # Get commands from last 7 days for each press
            now = datetime.now(IST)
            for press_name in press_names:
                for days_back in range(7):
                    date = now - timedelta(days=days_back)
                    cmd_prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={date.month:02d}/day={date.day:02d}/"

                    try:
                        cmd_blobs = bucket.list_blobs(prefix=cmd_prefix)
                        for blob in cmd_blobs:
                            if blob.name.endswith('/'):
                                continue
                            try:
                                log_data = json.loads(blob.download_as_text())
                                # Parse payload_value if it's a string
                                if 'payload_value' in log_data and isinstance(log_data['payload_value'], str):
                                    try:
                                        log_data['parsed_payload'] = json.loads(log_data['payload_value'])
                                    except json.JSONDecodeError:
                                        log_data['parsed_payload'] = {}
                                else:
                                    log_data['parsed_payload'] = log_data.get('payload_value', {})
                                recent_logs.append(log_data)
                            except Exception as e:
                                print(f"Error reading command log {blob.name}: {e}")
                    except Exception as e:
                        print(f"Error listing commands for {press_name} on {date.date()}: {e}")
        except Exception as e:
            print(f"Error listing blobs from GCS: {e}")
            import traceback
            traceback.print_exc()

    # Sort by timestamp descending and get the last 50
    recent_logs = sorted(recent_logs, key=lambda x: x.get('timestamp', ''), reverse=True)[:50]
    
    return render_template('press_admin_dashboard.html', 
                         presses=presses, 
                         recent_logs=recent_logs,
                         mqtt_status=True)

@press_portal_bp.route('/admin/add_press', methods=['POST'])
@login_required
@admin_required
def add_press():
    """Add a new press"""
    data = request.get_json()
    
    name = data.get('name')
    location = data.get('location')
    description = data.get('description', '')
    
    if not name:
        return jsonify({"error": "Press name is required"}), 400

    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []

    new_press = {
        "id": str(uuid.uuid4()),
        "name": name,
        "location": location or "Not specified",
        "description": description,
        "created_at": datetime.now(IST).isoformat(),
        "status": "active"
    }

    presses.append(new_press)

    if write_to_gcs(PRESSES_FILE, presses, PRESSES_BUCKET_NAME):
        return jsonify({
            "message": "Press added successfully",
            "press": new_press
        }), 201
    else:
        return jsonify({"error": "Failed to save press"}), 500

@press_portal_bp.route('/admin/delete_press/<press_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_press(press_id):
    """Delete a press"""
    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []
    presses = [p for p in presses if p['id'] != press_id]

    if write_to_gcs(PRESSES_FILE, presses, PRESSES_BUCKET_NAME):
        return jsonify({"message": "Press deleted successfully"}), 200
    else:
        return jsonify({"error": "Failed to delete press"}), 500

@press_portal_bp.route('/admin/generate_qr/<press_id>')
@service_access_required('press_portal')
@login_required
@admin_required
def generate_qr_code(press_id):
    """Generate QR code for a press"""
    presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
    if presses is None:
        presses = []
    press = next((p for p in presses if p['id'] == press_id), None)

    if not press:
        return jsonify({"error": "Press not found"}), 404

    # Generate URL to the new operations page
    press_url = url_for('press_portal.operations_press_page', press_id=press_id, _external=True)

    return render_template('press_qr_code.html', press=press, press_url=press_url)


# ==================== MONITORING ROUTES ====================

@press_portal_bp.route('/monitoring')
@login_required
def monitoring_dashboard():
    """
    Main monitoring dashboard showing all presses with their current status.
    Uses fresh GCS read to ensure latest press_list.json data is loaded.
    """
    # Get the press list from processed data - with fresh read (no cache)
    press_list_data = read_from_gcs_fresh(f"{PROCESSED_DATA_PREFIX}press_list.json")

    if not press_list_data:
        flash('No press monitoring data available yet. Cloud functions may still be processing.', 'warning')
        press_list_data = {
            'last_discovered': None,
            'total_presses': 0,
            'presses': []
        }

    # Calculate status for each press (green if data within 20 mins, red otherwise)
    now = datetime.now(IST)
    for press in press_list_data.get('presses', []):
        if press.get('last_updated'):
            try:
                # Parse the last_updated timestamp (ISO format: YYYY-MM-DDTHH:MM:SS)
                last_updated = datetime.fromisoformat(press['last_updated'].replace('Z', '+00:00'))
                # Make it timezone-aware if it isn't already
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=IST.tzinfo if hasattr(IST, 'tzinfo') else IST)
                elif last_updated.tzinfo != IST:
                    # Convert to IST
                    last_updated = last_updated.astimezone(IST)

                # Calculate time difference in minutes
                time_diff = (now - last_updated).total_seconds() / 60

                # Set status: green if within 20 mins, red otherwise
                press['status'] = 'active' if time_diff <= 20 else 'inactive'
            except Exception as e:
                print(f"Error parsing timestamp for {press.get('press_name')}: {e}")
                press['status'] = 'unknown'
        else:
            press['status'] = 'inactive'

    # Get current operations for all presses
    current_operations = get_all_current_operations()

    # Create response with no-cache headers to prevent browser caching
    response = make_response(render_template('press_monitoring_dashboard.html',
                         press_data=press_list_data,
                         current_operations=current_operations))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'

    return response


@press_portal_bp.route('/monitoring/tv')
@login_required
def monitoring_tv_mode():
    """
    TV Mode - Minimal display optimized for 32" TV screens.
    Shows only essential information: press name, current operation, stroke count.
    Uses fresh GCS read to ensure latest press_list.json data is loaded.
    """
    # Get the press list from processed data - with fresh read (no cache)
    press_list_data = read_from_gcs_fresh(f"{PROCESSED_DATA_PREFIX}press_list.json")

    if not press_list_data:
        press_list_data = {
            'last_discovered': None,
            'total_presses': 0,
            'presses': []
        }

    # Calculate status for each press
    now = datetime.now(IST)
    for press in press_list_data.get('presses', []):
        if press.get('last_updated'):
            try:
                last_updated = datetime.fromisoformat(press['last_updated'].replace('Z', '+00:00'))
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=IST.tzinfo if hasattr(IST, 'tzinfo') else IST)
                elif last_updated.tzinfo != IST:
                    last_updated = last_updated.astimezone(IST)

                time_diff = (now - last_updated).total_seconds() / 60
                press['status'] = 'active' if time_diff <= 20 else 'inactive'
            except Exception as e:
                print(f"Error parsing timestamp for {press.get('press_name')}: {e}")
                press['status'] = 'unknown'
        else:
            press['status'] = 'inactive'

    # Get current operations for all presses
    current_operations = get_all_current_operations()

    # Create response with no-cache headers to prevent browser caching
    response = make_response(render_template('press_tv_mode.html',
                         press_data=press_list_data,
                         current_operations=current_operations))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'

    return response


@press_portal_bp.route('/monitoring/<press_name>')
@login_required
def monitoring_detail(press_name):
    """Redirect to unified analytics dashboard with press pre-selected."""
    date = request.args.get('date', '')
    url = url_for('press_portal.analytics_dashboard', press=press_name)
    if date:
        url += f'&date={date}'
    return redirect(url)


@press_portal_bp.route('/monitoring/<press_name>/_legacy')
@login_required
def monitoring_detail_legacy(press_name):
    """
    Detailed monitoring view for a specific press with graphs.
    Supports date selection via query parameter.

    Query parameters:
    - date: YYYY-MM-DD format (defaults to today)
    """
    # Get selected date or default to today
    selected_date = request.args.get('date')
    today_str = datetime.now(IST).strftime('%Y-%m-%d')

    if not selected_date:
        selected_date = today_str

    # Don't load available dates on page load - will be loaded on-demand when dropdown is clicked
    available_dates = []

    # Read from date-specific files (YYYY-MM-DD.json)
    timeseries_path = f"{PROCESSED_DATA_PREFIX}{press_name}/{selected_date}.json"
    timeseries_data = read_from_gcs(timeseries_path)

    # Fallback: If no data for selected date, try to load most recent available date
    actual_date = selected_date
    if not timeseries_data:
        available_dates_list = get_available_dates(press_name)
        if available_dates_list:
            # Get the most recent date
            actual_date = available_dates_list[0]
            timeseries_path = f"{PROCESSED_DATA_PREFIX}{press_name}/{actual_date}.json"
            timeseries_data = read_from_gcs(timeseries_path)

            if timeseries_data:
                flash(f'No data available for {selected_date}. Showing most recent data from {actual_date}.', 'info')
            else:
                flash(f'No monitoring data available for {press_name}', 'error')
                return redirect(url_for('press_portal.monitoring_dashboard'))
        else:
            flash(f'No monitoring data available for {press_name}', 'error')
            return redirect(url_for('press_portal.monitoring_dashboard'))

    # Get press list to check available metrics
    press_list_data = read_from_gcs(f"{PROCESSED_DATA_PREFIX}press_list.json")
    press_info = None

    if press_list_data:
        for press in press_list_data.get('presses', []):
            if press['press_name'] == press_name:
                press_info = press
                break

    # Get command history for this press
    command_history = get_press_commands(press_name, limit=20)

    # Get current operation
    current_operation = get_current_operation(press_name)

    return render_template('press_monitoring_detail.html',
                         press_name=press_name,
                         press_info=press_info,
                         timeseries=timeseries_data,
                         command_history=command_history,
                         current_operation=current_operation,
                         selected_date=actual_date,  # Use actual date loaded (may differ from requested)
                         available_dates=available_dates,
                         today_date=today_str)


@press_portal_bp.route('/api/monitoring/press_list', methods=['GET'])
@login_required
def api_get_press_list():
    """
    API endpoint to get list of all presses with monitoring data
    """
    press_list_data = read_from_gcs(f"{PROCESSED_DATA_PREFIX}press_list.json")

    if not press_list_data:
        return jsonify({
            "error": "No press data available",
            "presses": []
        }), 404

    return jsonify(press_list_data), 200


# In-memory cache for live monitoring (shared across all requests)
_live_monitoring_cache = {"data": None, "timestamp": 0}

@press_portal_bp.route('/api/monitoring/live_all', methods=['GET'])
@login_required
def api_get_live_all():
    """
    NEW: Fast live monitoring endpoint for ALL presses using latest.json files.

    Features:
    - Reads latest.json (no sorting needed - instant access)
    - 5-second in-memory cache (shared across all users/pages)
    - Graceful fallback to sorting if latest.json doesn't exist
    - Error handling with old data retention

    Used by: Monitoring Dashboard & TV Mode for live stroke count updates
    Update frequency: Frontend polls every 5 seconds

    Returns:
    {
        "presses": [
            {
                "press_name": "press_machine_NP20",
                "stroke_count": 12458,
                "last_updated": "2025-12-21T00:10:37.305578+05:30",
                "status": "active"
            },
            ...
        ],
        "cached": true/false,
        "timestamp": "2025-12-21T00:10:42+05:30"
    }
    """
    global _live_monitoring_cache
    import time

    now_time = time.time()
    now_dt = datetime.now(IST)

    # Serve from cache if less than 5 seconds old
    if _live_monitoring_cache["data"] and (now_time - _live_monitoring_cache["timestamp"]) < 5:
        response_data = _live_monitoring_cache["data"].copy()
        response_data["cached"] = True
        response_data["cache_age"] = round(now_time - _live_monitoring_cache["timestamp"], 2)

        response = make_response(jsonify(response_data))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    # Cache expired or doesn't exist - fetch fresh data
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            # If GCS unavailable, return cached data if available
            if _live_monitoring_cache["data"]:
                logging.warning("GCS unavailable, serving stale cache")
                response_data = _live_monitoring_cache["data"].copy()
                response_data["cached"] = True
                response_data["stale"] = True
                return jsonify(response_data), 200
            return jsonify({"error": "GCS bucket not available"}), 500

        # Get list of presses
        press_list_data = read_from_gcs_fresh(f"{PROCESSED_DATA_PREFIX}press_list.json")
        if not press_list_data:
            return jsonify({
                "presses": [],
                "error": "No press list available",
                "timestamp": now_dt.isoformat()
            }), 200

        presses = press_list_data.get('presses', [])
        live_data = []

        # For each press, try to get data from latest.json
        for press_info in presses:
            press_name = press_info.get('press_name')
            if not press_name:
                continue

            stroke_count = None
            last_updated = None
            status = 'unknown'

            try:
                # --- Stroke count: try latest.json first, fall back to listing ---
                latest_path = f"{RAW_DATA_PREFIX}press_name={press_name}/latest.json"
                latest_blob = bucket.blob(latest_path)

                if latest_blob.exists():
                    data = json.loads(latest_blob.download_as_text())
                    stroke_count_str = data.get('stroke_count')
                    stroke_count = int(stroke_count_str) if stroke_count_str is not None else None
                    timestamp_str = data.get('timestamp')
                    if timestamp_str:
                        last_updated = datetime.fromisoformat(timestamp_str)
                else:
                    # Fallback: list dated folders
                    for days_back in range(7):
                        check_date = now_dt - timedelta(days=days_back)
                        prefix = f"{RAW_DATA_PREFIX}press_name={press_name}/month={check_date.month:02d}/day={check_date.day:02d}/"
                        blobs = list(bucket.list_blobs(prefix=prefix))
                        if blobs:
                            blobs.sort(key=lambda b: b.name, reverse=True)
                            data = json.loads(blobs[0].download_as_text())
                            stroke_count_str = data.get('stroke_count')
                            stroke_count = int(stroke_count_str) if stroke_count_str is not None else None
                            timestamp_str = data.get('timestamp')
                            if timestamp_str:
                                last_updated = datetime.fromisoformat(timestamp_str)
                            break

                # --- Status: read status/latest.json (authoritative ONLINE/OFFLINE) ---
                status = get_press_online_status(bucket, press_name)
                if status == 'UNKNOWN':
                    # Fallback: derive from stroke data age if status file not yet available
                    if last_updated:
                        age_min = (now_dt - last_updated).total_seconds() / 60
                        status = 'ONLINE' if age_min <= 5 else 'OFFLINE'
                    else:
                        status = 'OFFLINE'

                # Add to results
                live_data.append({
                    'press_name': press_name,
                    'stroke_count': stroke_count,
                    'last_updated': last_updated.isoformat() if last_updated else None,
                    'status': status
                })

            except Exception as e:
                logging.error(f"Error reading live data for {press_name}: {e}")
                # Add press with error state (keeps it visible with old data)
                live_data.append({
                    'press_name': press_name,
                    'stroke_count': None,
                    'last_updated': None,
                    'status': 'error',
                    'error': str(e)
                })

        # Build response
        response_data = {
            "presses": live_data,
            "cached": False,
            "timestamp": now_dt.isoformat()
        }

        # Update cache
        _live_monitoring_cache = {
            "data": response_data,
            "timestamp": now_time
        }

        # Return with no-cache headers
        response = make_response(jsonify(response_data))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    except Exception as e:
        logging.error(f"Error in live_all endpoint: {e}")
        # Return stale cache if available
        if _live_monitoring_cache["data"]:
            response_data = _live_monitoring_cache["data"].copy()
            response_data["cached"] = True
            response_data["stale"] = True
            response_data["error"] = str(e)
            return jsonify(response_data), 200
        return jsonify({"error": str(e)}), 500


@press_portal_bp.route('/api/monitoring/live_status', methods=['GET'])
@login_required
def api_get_live_status():
    """
    API endpoint to get live status of all presses from GCS daily files.
    Returns current stroke count, stroke rate, and connection status.
    """
    live_status = {}
    now = datetime.now(IST)
    today_str = now.strftime('%Y-%m-%d')

    # Get list of presses
    press_list_data = read_from_gcs(f"{PROCESSED_DATA_PREFIX}press_list.json")

    if not press_list_data:
        return jsonify({
            'status': {},
            'timestamp': now.isoformat(),
            'error': 'No press list available'
        }), 200

    presses = press_list_data.get('presses', [])

    # For each press, get latest data from today's file
    for press_info in presses:
        press_name = press_info.get('press_name')
        if not press_name:
            continue

        # Read today's daily file
        daily_file_path = f"{PROCESSED_DATA_PREFIX}{press_name}/{today_str}.json"
        daily_data = read_from_gcs(daily_file_path)

        if not daily_data:
            # No data for today - press is disconnected
            live_status[press_name] = {
                'stroke_count': 0,
                'connected': False,
                'last_seen': None
            }
            continue

        # Get latest data point
        data_points = daily_data.get('data_points', [])
        if not data_points:
            live_status[press_name] = {
                'stroke_count': 0,
                'connected': False,
                'last_seen': None
            }
            continue

        # Get the most recent data point (should be last in list)
        latest_point = data_points[-1]
        timestamp_str = latest_point.get('timestamp')
        stroke_count = latest_point.get('stroke_count', 0)
        stroke_rate = latest_point.get('stroke_rate', 0)

        # Check if press is connected (data within last 20 minutes for dashboard)
        connected = False
        last_seen_dt = None

        if timestamp_str:
            try:
                last_seen_dt = datetime.fromisoformat(timestamp_str)
                elapsed = (now - last_seen_dt).total_seconds()
                connected = elapsed <= DASHBOARD_DISCONNECT_TIMEOUT
            except:
                pass

        # Get latest command from press_info
        latest_command = press_info.get('latest_command', {})

        live_status[press_name] = {
            'stroke_count': stroke_count,
            'stroke_rate': stroke_rate,
            'connected': connected,
            'last_seen': timestamp_str,
            'latest_command': latest_command
        }

    return jsonify({
        'status': live_status,
        'timestamp': now.isoformat()
    }), 200


@press_portal_bp.route('/api/monitoring/<press_name>', methods=['GET'])
@login_required
def api_get_press_data(press_name):
    """
    API endpoint to get time-series data for a specific press.
    Supports date selection and optional time range filtering.

    Query parameters:
    - date: YYYY-MM-DD format (defaults to today)
    - start: ISO timestamp (e.g., 2025-11-21T00:00:00)
    - end: ISO timestamp (e.g., 2025-11-21T23:59:59)
    """
    # Get selected date or default to today
    selected_date = request.args.get('date')
    today_str = datetime.now(IST).strftime('%Y-%m-%d')

    if not selected_date:
        selected_date = today_str

    # Read ONLY from date-specific files (YYYY-MM-DD.json)
    # NO fallback to timeseries.json
    timeseries_path = f"{PROCESSED_DATA_PREFIX}{press_name}/{selected_date}.json"
    timeseries_data = read_from_gcs(timeseries_path)

    if not timeseries_data:
        return jsonify({
            "error": f"No data available for press {press_name} on {selected_date}"
        }), 404

    # Add metadata
    timeseries_data['requested_date'] = selected_date
    timeseries_data['is_today'] = (selected_date == today_str)

    # Apply time range filter if provided
    start_time = request.args.get('start')
    end_time = request.args.get('end')

    if start_time or end_time:
        data_points = timeseries_data.get('data_points', [])
        filtered_points = []

        for point in data_points:
            timestamp = point.get('timestamp')
            if timestamp:
                # Check if within range
                if start_time and timestamp < start_time:
                    continue
                if end_time and timestamp > end_time:
                    continue
                filtered_points.append(point)

        timeseries_data['data_points'] = filtered_points
        timeseries_data['filtered'] = True
        timeseries_data['filter_start'] = start_time
        timeseries_data['filter_end'] = end_time

    return jsonify(timeseries_data), 200


@press_portal_bp.route('/api/monitoring/<press_name>/dates', methods=['GET'])
@login_required
def api_get_available_dates(press_name):
    """
    API endpoint to get list of available dates for a press.
    Returns dates in descending order (most recent first).
    """
    dates = get_available_dates(press_name)
    today_str = datetime.now(IST).strftime('%Y-%m-%d')

    return jsonify({
        "press_name": press_name,
        "today": today_str,
        "available_dates": dates
    }), 200


@press_portal_bp.route('/api/press_commands/<press_name>', methods=['GET'])
def api_get_press_commands(press_name):
    """
    API endpoint to get command history for a press.
    Returns recent commands from GCS.
    """
    limit = request.args.get('limit', 20, type=int)
    commands = get_press_commands(press_name, limit=limit)

    return jsonify({
        "press_name": press_name,
        "commands": commands,
        "count": len(commands)
    }), 200


# Global constant for monitoring routes
PROCESSED_DATA_PREFIX = 'processed/'
COMMAND_DATA_PREFIX = 'command/'


def get_available_dates(press_name):
    """
    Get list of available dates for a press.
    First tries to read from available_dates.json, then falls back to scanning.
    Returns: list of date strings in YYYY-MM-DD format, sorted descending
    """
    bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
    if not bucket:
        return []

    # Try to read from available_dates.json first (cached by cloud function)
    dates_path = f"{PROCESSED_DATA_PREFIX}{press_name}/available_dates.json"
    try:
        blob = bucket.blob(dates_path)
        if blob.exists():
            content = blob.download_as_text()
            dates_data = json.loads(content)
            return dates_data.get('dates', [])
    except Exception as e:
        logging.warning(f"Could not read available_dates.json for {press_name}: {e}")

    # Fall back to scanning for date files
    dates = []
    prefix = f"{PROCESSED_DATA_PREFIX}{press_name}/"
    try:
        blobs = bucket.list_blobs(prefix=prefix)
        for blob in blobs:
            filename = blob.name.replace(prefix, '')
            # Look for YYYY-MM-DD.json files (15 chars)
            if filename.endswith('.json') and len(filename) == 15:
                date_str = filename[:-5]  # Remove .json
                try:
                    datetime.strptime(date_str, '%Y-%m-%d')
                    dates.append(date_str)
                except ValueError:
                    continue
    except Exception as e:
        logging.error(f"Error scanning dates for {press_name}: {e}")

    # Sort descending (most recent first)
    dates.sort(reverse=True)
    return dates


def get_press_commands(press_name, limit=10):
    """
    Get recent commands for a specific press from GCS.
    Commands are stored at: command/press_name=X/month=Y/day=Z/timestamp.json
    Returns list of command dicts sorted by timestamp descending.
    Searches last 7 days for better command history visibility.
    """
    bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
    if not bucket:
        return []

    commands = []
    now = datetime.now(IST)

    # Search in last 7 days for commands
    for days_back in range(7):
        date = now - timedelta(days=days_back)
        prefix = f"{COMMAND_DATA_PREFIX}press_name={press_name}/month={date.month:02d}/day={date.day:02d}/"

        try:
            blobs = bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                if blob.name.endswith('/'):
                    continue
                try:
                    content = blob.download_as_text()
                    cmd_data = json.loads(content)

                    # Parse the payload_value if it's a string
                    if 'payload_value' in cmd_data and isinstance(cmd_data['payload_value'], str):
                        try:
                            cmd_data['parsed_payload'] = json.loads(cmd_data['payload_value'])
                        except json.JSONDecodeError:
                            cmd_data['parsed_payload'] = {}
                    else:
                        cmd_data['parsed_payload'] = cmd_data.get('payload_value', {})

                    commands.append(cmd_data)
                except Exception as e:
                    logging.error(f"Error reading command {blob.name}: {e}")
        except Exception as e:
            logging.error(f"Error listing commands for {press_name}: {e}")

    # Sort by timestamp descending and limit
    commands.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return commands[:limit]


def get_current_operation(press_name):
    """
    Get the current operation for a press based on the latest command.
    Returns dict with operation info if START is active, None if STOP or no command.
    """
    commands = get_press_commands(press_name, limit=1)
    if not commands:
        return None

    latest_cmd = commands[0]
    payload = latest_cmd.get('parsed_payload', {})

    # Check if it's a START command (press is active)
    status = payload.get('status', '').upper()
    if status == 'START':
        return {
            'component': payload.get('component', 'Unknown'),
            'operation': payload.get('operation', 'Unknown'),
            'operator': payload.get('operator', 'Unknown'),
            'timestamp': payload.get('timestamp', latest_cmd.get('timestamp', '')),
            'status': 'RUNNING'
        }

    return None


def get_all_current_operations():
    """
    Get current operations for all presses.
    Returns dict: {press_name: operation_info}
    """
    bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
    if not bucket:
        return {}

    # Get list of all press names from command folder
    press_names = set()
    try:
        blobs = bucket.list_blobs(prefix=COMMAND_DATA_PREFIX, delimiter='/')
        for prefix in blobs.prefixes:
            # prefix format: command/press_name=X/
            if 'press_name=' in prefix:
                press_name = prefix.split('press_name=')[1].rstrip('/')
                press_names.add(press_name)
    except Exception as e:
        logging.error(f"Error listing press names: {e}")

    # Get current operation for each press
    operations = {}
    for press_name in press_names:
        op = get_current_operation(press_name)
        if op:
            operations[press_name] = op

    return operations


# ==================== RAW DATA EXPORT ROUTES ====================

@press_portal_bp.route('/raw-data')
@login_required
def raw_data_export():
    """Raw data export page"""
    # Get press list from processed data
    press_list_data = read_from_gcs(f"{PROCESSED_DATA_PREFIX}press_list.json")

    presses = []
    if press_list_data:
        presses = [p['press_name'] for p in press_list_data.get('presses', [])]

    return render_template('press_raw_data.html', presses=presses)


@press_portal_bp.route('/api/raw-data/available-dates/<press_name>')
@login_required
def api_raw_data_available_dates(press_name):
    """
    Get available dates for raw stroke data for a press.
    Scans the raw data folder structure to find dates with data.
    Returns list of dates in YYYY-MM-DD format.
    """
    bucket = get_gcs_bucket(RAW_DATA_BUCKET_NAME)
    if not bucket:
        return jsonify({"error": "Could not connect to storage"}), 500

    cache_path = f"{RAW_DATA_PREFIX}press_name={press_name}/available_dates.json"
    today_str = datetime.now(IST).strftime('%Y-%m-%d')

    # Check for cached available_dates.json first
    # Only use cache if it's recent (updated today) and has dates
    try:
        blob = bucket.blob(cache_path)
        if blob.exists():
            content = blob.download_as_text()
            cache_data = json.loads(content)
            cached_dates = cache_data.get('dates', [])
            last_updated = cache_data.get('last_updated', '')

            # Only use cache if it has dates AND was updated today
            if cached_dates and last_updated.startswith(today_str):
                return jsonify({
                    "press_name": press_name,
                    "available_dates": cached_dates,
                    "cached": True
                }), 200
            # If cache is empty or stale, re-scan
            logging.info(f"Cache for {press_name} is stale or empty, re-scanning...")
    except Exception as e:
        logging.warning(f"Could not read cached dates for {press_name}: {e}")

    # Scan the folder structure by listing all files
    available_dates = set()
    prefix = f"{RAW_DATA_PREFIX}press_name={press_name}/"

    logging.info(f"Scanning raw data for press {press_name} with prefix: {prefix}")

    try:
        # List all blobs and extract dates from paths
        # Path format: press_data/press_name=X/month=MM/day=DD/HHMMSS.microseconds.json
        blobs = bucket.list_blobs(prefix=prefix)
        blob_count = 0

        current_year = datetime.now(IST).year

        for blob in blobs:
            blob_count += 1
            # Extract month and day from path
            path = blob.name
            try:
                # Parse path: press_data/press_name=X/month=11/day=24/filename.json
                if '/month=' in path and '/day=' in path:
                    # Extract month
                    month_part = path.split('/month=')[1]
                    month_str = month_part.split('/')[0]

                    # Extract day
                    day_part = path.split('/day=')[1]
                    day_str = day_part.split('/')[0]

                    # Construct date - handle year transition
                    # If current month is January and we find month=12, use previous year
                    current_month = datetime.now(IST).month
                    year = current_year
                    if current_month <= 2 and int(month_str) >= 11:
                        year = current_year - 1

                    date_str = f"{year}-{int(month_str):02d}-{int(day_str):02d}"
                    # Validate it's a real date
                    datetime.strptime(date_str, '%Y-%m-%d')
                    available_dates.add(date_str)
            except (ValueError, TypeError, IndexError) as e:
                logging.debug(f"Could not parse date from path {path}: {e}")
                continue

        logging.info(f"Scanned {blob_count} blobs for {press_name}, found {len(available_dates)} dates")

    except Exception as e:
        logging.error(f"Error scanning raw data dates for {press_name}: {e}")
        return jsonify({"error": f"Failed to scan dates: {str(e)}"}), 500

    # Sort dates descending (most recent first)
    sorted_dates = sorted(list(available_dates), reverse=True)

    # Only cache if we found dates (don't cache empty results)
    if sorted_dates:
        try:
            cache_data = {
                "press_name": press_name,
                "dates": sorted_dates,
                "last_updated": datetime.now(IST).isoformat()
            }
            blob = bucket.blob(cache_path)
            blob.upload_from_string(json.dumps(cache_data, indent=2), content_type='application/json')
            logging.info(f"Cached {len(sorted_dates)} dates for {press_name}")
        except Exception as e:
            logging.warning(f"Could not cache dates for {press_name}: {e}")

    return jsonify({
        "press_name": press_name,
        "available_dates": sorted_dates,
        "cached": False
    }), 200


@press_portal_bp.route('/api/raw-data/fetch/<press_name>/<date>')
@login_required
def api_raw_data_fetch(press_name, date):
    """
    Fetch raw stroke data for a press on a specific date.
    Returns JSON array of data points.

    Query params:
    TIME WINDOW MODE (recommended for large datasets):
    - start_hour: Start hour (0-23, optional) - requires end_hour
    - end_hour: End hour (0-23, optional) - requires start_hour

    PAGINATION MODE (backward compatible, used when no time window specified):
    - limit: Max number of records to return (default: 1000, max: 5000)
    - offset: Number of records to skip (default: 0)

    Note: Time window mode and pagination mode are mutually exclusive.
    """
    bucket = get_gcs_bucket(RAW_DATA_BUCKET_NAME)
    if not bucket:
        return jsonify({"error": "Could not connect to storage"}), 500

    # Get time window parameters
    start_hour = request.args.get('start_hour', type=int)
    end_hour = request.args.get('end_hour', type=int)

    # Get pagination parameters (used when no time window)
    limit = min(request.args.get('limit', 1000, type=int), 5000)
    offset = request.args.get('offset', 0, type=int)

    # Parse date to get month and day
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        month = date_obj.month
        day = date_obj.day
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    # Validate time window parameters
    use_time_window = False
    if start_hour is not None or end_hour is not None:
        if start_hour is None or end_hour is None:
            return jsonify({"error": "Both start_hour and end_hour are required for time window mode"}), 400
        # Allow end_hour=24 to represent end of day (midnight)
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 24):
            return jsonify({"error": "Hours must be between 0 and 23 (end_hour can be 24 for midnight)"}), 400
        if start_hour >= end_hour:
            return jsonify({"error": "start_hour must be less than end_hour"}), 400
        use_time_window = True

    # Build the prefix path
    prefix = f"{RAW_DATA_PREFIX}press_name={press_name}/month={month:02d}/day={day:02d}/"

    def extract_hour_from_filename(blob_name):
        """Extract hour from filename format: HHMMSS.microseconds.json"""
        try:
            filename = blob_name.split('/')[-1]  # Get just the filename
            hour_str = filename[:2]  # First 2 chars are HH
            return int(hour_str)
        except:
            return None

    data_points = []
    total_count = 0
    filtered_count = 0

    try:
        # List all blobs first
        blobs = list(bucket.list_blobs(prefix=prefix))
        blob_files = [b for b in blobs if b.name.endswith('.json')]
        total_count = len(blob_files)

        # Sort by name (filename contains timestamp)
        blob_files.sort(key=lambda b: b.name)

        # Apply filtering based on mode
        if use_time_window:
            # TIME WINDOW MODE: Filter by hour range
            filtered_blobs = []
            for blob in blob_files:
                hour = extract_hour_from_filename(blob.name)
                if hour is not None and start_hour <= hour < end_hour:
                    filtered_blobs.append(blob)

            filtered_count = len(filtered_blobs)
            blobs_to_fetch = filtered_blobs
        else:
            # PAGINATION MODE: Use offset and limit
            filtered_count = total_count
            blobs_to_fetch = blob_files[offset:offset + limit]

        # Fetch the selected blobs
        for blob in blobs_to_fetch:
            try:
                content = blob.download_as_text()
                data = json.loads(content)
                data_points.append(data)
            except Exception as e:
                logging.warning(f"Error reading {blob.name}: {e}")
                continue

    except Exception as e:
        logging.error(f"Error fetching raw data for {press_name} on {date}: {e}")
        return jsonify({"error": f"Failed to fetch data: {str(e)}"}), 500

    # Sort by timestamp
    data_points.sort(key=lambda x: x.get('timestamp', ''))

    # Build response based on mode
    response = {
        "press_name": press_name,
        "date": date,
        "total_count": total_count,
        "count": len(data_points),
        "data": data_points
    }

    if use_time_window:
        response["mode"] = "time_window"
        response["start_hour"] = start_hour
        response["end_hour"] = end_hour
        response["filtered_count"] = filtered_count
    else:
        response["mode"] = "pagination"
        response["offset"] = offset
        response["limit"] = limit
        response["has_more"] = (offset + len(data_points)) < total_count

    return jsonify(response), 200


@press_portal_bp.route('/api/raw-data/download/<press_name>/<date>')
@login_required
def api_raw_data_download_csv(press_name, date):
    """
    Stream raw stroke data as CSV download.
    """
    bucket = get_gcs_bucket(RAW_DATA_BUCKET_NAME)
    if not bucket:
        return jsonify({"error": "Could not connect to storage"}), 500

    # Parse date to get month and day
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        month = date_obj.month
        day = date_obj.day
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    # Build the prefix path
    prefix = f"{RAW_DATA_PREFIX}press_name={press_name}/month={month:02d}/day={day:02d}/"

    def generate_csv():
        """Generator function to stream CSV data"""
        # CSV header
        yield "timestamp,stroke_count\n"

        try:
            blobs = list(bucket.list_blobs(prefix=prefix))
            # Sort blobs by name (which contains timestamp)
            blobs.sort(key=lambda b: b.name)

            for blob in blobs:
                if blob.name.endswith('.json'):
                    try:
                        content = blob.download_as_text()
                        data = json.loads(content)
                        timestamp = data.get('timestamp', '')
                        stroke_count = data.get('stroke_count', '')
                        yield f"{timestamp},{stroke_count}\n"
                    except Exception as e:
                        logging.warning(f"Error reading {blob.name}: {e}")
                        continue
        except Exception as e:
            logging.error(f"Error streaming CSV for {press_name} on {date}: {e}")

    filename = f"{press_name}_{date}_stroke_data.csv"

    return Response(
        generate_csv(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-cache'
        }
    )


# ==================== KBI ANALYTICS ROUTES ====================
# Add these routes to your press_portal.py file
# Place them after the existing analytics routes (around line 1800+)

# ==================== ENHANCED ANALYTICS ROUTES ====================

@press_portal_bp.route('/analytics/comprehensive')
@login_required
def comprehensive_analytics():
    """Comprehensive Analytics Dashboard with all report types"""
    # Get press list
    press_list_data = read_from_gcs_fresh(f"{PROCESSED_DATA_PREFIX}press_list.json")
    presses = []
    if press_list_data:
        presses = [p['press_name'] for p in press_list_data.get('presses', [])]
    
    return render_template('press_comprehensive_analytics.html', presses=presses)


@press_portal_bp.route('/api/analytics/report-types/<press_name>', methods=['GET'])
@login_required
def get_available_report_types(press_name):
    """Get available report types for a specific press"""
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        report_types = {
            '5min': {'available': False, 'latest_date': None, 'count': 0},
            '30min': {'available': False, 'latest_date': None, 'count': 0},
            'daily': {'available': False, 'latest_date': None, 'count': 0}
        }
        
        # Check each report type
        for report_type in ['5min', '30min', 'daily']:
            prefix = f"press_reports/{report_type}/{press_name}/"
            blobs = list(bucket.list_blobs(prefix=prefix, max_results=100))
            
            if blobs:
                report_types[report_type]['available'] = True
                report_types[report_type]['count'] = len(blobs)
                
                # Get latest date
                dates = []
                for blob in blobs:
                    try:
                        filename = blob.name.split('/')[-1]
                        if filename.endswith('.json'):
                            date_str = filename.replace('.json', '')
                            dates.append(date_str)
                    except:
                        continue
                
                if dates:
                    report_types[report_type]['latest_date'] = sorted(dates, reverse=True)[0]
        
        return jsonify({
            'press_name': press_name,
            'report_types': report_types
        }), 200
    
    except Exception as e:
        logging.error(f"Error getting report types: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/available-dates/<report_type>/<press_name>', methods=['GET'])
@login_required
def get_analytics_available_dates(report_type, press_name):
    """Get available dates for a specific report type and press"""
    try:
        if report_type not in ['5min', '30min', 'daily']:
            return jsonify({'error': 'Invalid report type'}), 400
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        prefix = f"press_reports/{report_type}/{press_name}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        dates = set()
        for blob in blobs:
            try:
                filename = blob.name.split('/')[-1]
                if filename.endswith('.json'):
                    # Extract date from filename (format: YYYY-MM-DD_HHMMSS.json or YYYY-MM-DD.json)
                    date_part = filename.split('_')[0]
                    if len(date_part) == 10:  # YYYY-MM-DD
                        dates.add(date_part)
            except:
                continue
        
        sorted_dates = sorted(list(dates), reverse=True)
        
        return jsonify({
            'press_name': press_name,
            'report_type': report_type,
            'available_dates': sorted_dates,
            'count': len(sorted_dates)
        }), 200
    
    except Exception as e:
        logging.error(f"Error getting available dates: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/reports/<report_type>/<press_name>/<date>', methods=['GET'])
@login_required
def get_analytics_reports(report_type, press_name, date):
    """
    Get analytical reports for a specific type, press, and date.
    Aggregates and analyzes vibration, temperature, and performance data.
    
    Query params:
    - start_hour: Filter by start hour (0-23)
    - end_hour: Filter by end hour (0-23)
    """
    try:
        if report_type not in ['5min', '30min', 'daily']:
            return jsonify({'error': 'Invalid report type'}), 400
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        # Get time filters
        start_hour = request.args.get('start_hour', type=int)
        end_hour = request.args.get('end_hour', type=int)
        
        # Build prefix
        prefix = f"press_reports/{report_type}/{press_name}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        # Filter blobs for the specific date
        matching_blobs = []
        for blob in blobs:
            filename = blob.name.split('/')[-1]
            if filename.startswith(date) and filename.endswith('.json'):
                matching_blobs.append(blob)
        
        if not matching_blobs:
            return jsonify({
                'press_name': press_name,
                'report_type': report_type,
                'date': date,
                'data_points': [],
                'aggregated': {},
                'message': 'No data found for this date'
            }), 404
        
        # Read and aggregate data
        data_points = []
        for blob in matching_blobs:
            try:
                content = blob.download_as_text()
                report = json.loads(content)
                
                # Apply time filter if specified
                if start_hour is not None and end_hour is not None:
                    timestamp = report.get('timestamp', '')
                    if timestamp:
                        hour = int(timestamp.split('T')[1][:2])
                        if not (start_hour <= hour < end_hour):
                            continue
                
                data_points.append(report)
            except Exception as e:
                logging.warning(f"Error reading blob {blob.name}: {e}")
                continue
        
        # Sort by timestamp
        data_points.sort(key=lambda x: x.get('timestamp', ''))
        
        # Aggregate analytics
        aggregated = aggregate_report_data(data_points, report_type)
        
        return jsonify({
            'press_name': press_name,
            'report_type': report_type,
            'date': date,
            'data_points': data_points,
            'aggregated': aggregated,
            'count': len(data_points)
        }), 200
    
    except Exception as e:
        logging.error(f"Error getting analytics reports: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/comparison', methods=['POST'])
@login_required
def get_comparison_analytics():
    """
    Compare analytics across multiple presses, dates, or report types.
    
    Request body:
    {
        "presses": ["press_machine_NP19", "press_machine_NP20"],
        "report_type": "30min",
        "start_date": "2025-01-20",
        "end_date": "2025-01-24"
    }
    """
    try:
        data = request.get_json()
        presses = data.get('presses', [])
        report_type = data.get('report_type', '30min')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if not presses or not start_date or not end_date:
            return jsonify({'error': 'Missing required fields'}), 400
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        # Generate date range
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        date_range = []
        current = start_dt
        while current <= end_dt:
            date_range.append(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)
        
        comparison_data = {}
        
        for press_name in presses:
            press_data = []
            
            for date in date_range:
                prefix = f"press_reports/{report_type}/{press_name}/"
                blobs = list(bucket.list_blobs(prefix=prefix))
                
                day_data = []
                for blob in blobs:
                    filename = blob.name.split('/')[-1]
                    if filename.startswith(date) and filename.endswith('.json'):
                        try:
                            content = blob.download_as_text()
                            report = json.loads(content)
                            day_data.append(report)
                        except:
                            continue
                
                if day_data:
                    aggregated = aggregate_report_data(day_data, report_type)
                    aggregated['date'] = date
                    press_data.append(aggregated)
            
            comparison_data[press_name] = press_data
        
        return jsonify({
            'comparison_data': comparison_data,
            'report_type': report_type,
            'date_range': date_range,
            'presses': presses
        }), 200
    
    except Exception as e:
        logging.error(f"Error in comparison analytics: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/vibration-analysis/<press_name>/<date>', methods=['GET'])
@login_required
def get_vibration_analysis(press_name, date):
    """
    Detailed vibration analysis with anomaly detection.
    Analyzes vibration patterns and identifies unusual spikes.
    """
    try:
        report_type = request.args.get('report_type', '30min')
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        prefix = f"press_reports/{report_type}/{press_name}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        vibration_data = []
        for blob in blobs:
            filename = blob.name.split('/')[-1]
            if filename.startswith(date) and filename.endswith('.json'):
                try:
                    content = blob.download_as_text()
                    report = json.loads(content)
                    
                    vib_x = report.get('avg_vibration_x')
                    vib_y = report.get('avg_vibration_y')
                    vib_z = report.get('avg_vibration_z')
                    timestamp = report.get('timestamp')
                    
                    if vib_x is not None and timestamp:
                        vibration_data.append({
                            'timestamp': timestamp,
                            'x': vib_x,
                            'y': vib_y,
                            'z': vib_z,
                            'magnitude': (vib_x**2 + vib_y**2 + vib_z**2)**0.5
                        })
                except:
                    continue
        
        vibration_data.sort(key=lambda x: x['timestamp'])
        
        # Calculate statistics and detect anomalies
        if vibration_data:
            magnitudes = [d['magnitude'] for d in vibration_data]
            mean_mag = sum(magnitudes) / len(magnitudes)
            variance = sum((x - mean_mag)**2 for x in magnitudes) / len(magnitudes)
            std_dev = variance ** 0.5
            
            # Mark anomalies (values > mean + 2*std_dev)
            threshold = mean_mag + 2 * std_dev
            for point in vibration_data:
                point['anomaly'] = point['magnitude'] > threshold
            
            analysis = {
                'mean_magnitude': mean_mag,
                'std_deviation': std_dev,
                'max_magnitude': max(magnitudes),
                'min_magnitude': min(magnitudes),
                'anomaly_threshold': threshold,
                'anomaly_count': sum(1 for p in vibration_data if p['anomaly'])
            }
        else:
            analysis = {}
        
        return jsonify({
            'press_name': press_name,
            'date': date,
            'vibration_data': vibration_data,
            'analysis': analysis,
            'count': len(vibration_data)
        }), 200
    
    except Exception as e:
        logging.error(f"Error in vibration analysis: {e}")
        return jsonify({'error': str(e)}), 500

@press_portal_bp.route('/api/reports/daily/trigger', methods=['POST'])
@login_required
def trigger_daily_report_generation():
    """
    Manually trigger daily operations report generation for specific dates.
    
    Request body:
    {
        "target_date": "2026-01-06",  // Required: YYYY-MM-DD format
        "press_name": "press_machine_NP1"  // Optional: specific machine or all if omitted
    }
    """
    try:
        data = request.get_json()
        target_date = data.get('target_date')
        press_name = data.get('press_name')
        
        if not target_date:
            return jsonify({'error': 'target_date is required (YYYY-MM-DD format)'}), 400
        
        # Validate date format
        try:
            date_obj = datetime.strptime(target_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        # Call the Cloud Function
        import requests
        
        cloud_function_url = 'https://asia-south1-arched-elixir-464218-j8.cloudfunctions.net/daily-operations-report'
        
        payload = {
            'target_date': target_date
        }
        
        if press_name:
            payload['press_name'] = press_name
        
        logging.info(f"Triggering daily report generation: {payload}")
        
        # Make request to Cloud Function (with timeout)
        response = requests.post(
            cloud_function_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=120  # 2 minute timeout
        )
        
        if response.status_code == 200:
            result = response.json()
            return jsonify({
                'success': True,
                'message': f'Report generation triggered successfully for {target_date}',
                'result': result
            }), 200
        else:
            return jsonify({
                'error': f'Cloud Function returned status {response.status_code}',
                'details': response.text
            }), 500
    
    except requests.exceptions.Timeout:
        return jsonify({
            'error': 'Request timed out',
            'message': 'Report generation may still be running in the background. Check back in a few minutes.'
        }), 504
    
    except Exception as e:
        logging.error(f"Error triggering report generation: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/reports/daily/check-status/<press_name>/<date>', methods=['GET'])
@login_required
def check_daily_report_status(press_name, date):
    """
    Check if a daily report exists for a specific machine and date.
    Used to verify if manual trigger was successful.
    """
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        year = date_obj.strftime('%Y')
        month = date_obj.strftime('%m')
        day = date_obj.strftime('%d')
        
        file_path = f"daily_press_report_operation/{press_name}/{year}/{month}/{day}_daily_report.json"
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'exists': False, 'error': 'Storage not available'}), 500
        
        blob = bucket.blob(file_path)
        exists = blob.exists()
        
        if exists:
            # Get file metadata
            blob.reload()
            return jsonify({
                'exists': True,
                'file_path': file_path,
                'last_modified': blob.updated.isoformat() if blob.updated else None
            }), 200
        else:
            return jsonify({
                'exists': False,
                'file_path': file_path
            }), 200
    
    except Exception as e:
        logging.error(f"Error checking report status: {e}")
        return jsonify({'error': str(e)}), 500
        
@press_portal_bp.route('/api/analytics/temperature-analysis/<press_name>/<date>', methods=['GET'])
@login_required
def get_temperature_analysis(press_name, date):
    """
    Detailed temperature analysis with trend detection.
    Monitors temperature patterns and identifies overheating risks.
    """
    try:
        report_type = request.args.get('report_type', '30min')
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        prefix = f"press_reports/{report_type}/{press_name}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        temperature_data = []
        for blob in blobs:
            filename = blob.name.split('/')[-1]
            if filename.startswith(date) and filename.endswith('.json'):
                try:
                    content = blob.download_as_text()
                    report = json.loads(content)
                    
                    temp = report.get('avg_temperature')
                    timestamp = report.get('timestamp')
                    
                    if temp is not None and timestamp:
                        temperature_data.append({
                            'timestamp': timestamp,
                            'temperature': temp
                        })
                except:
                    continue
        
        temperature_data.sort(key=lambda x: x['timestamp'])
        
        # Calculate statistics and detect overheating
        if temperature_data:
            temps = [d['temperature'] for d in temperature_data]
            mean_temp = sum(temps) / len(temps)
            max_temp = max(temps)
            min_temp = min(temps)
            
            # Define warning thresholds
            warning_threshold = 80  # Celsius
            critical_threshold = 90  # Celsius
            
            for point in temperature_data:
                if point['temperature'] >= critical_threshold:
                    point['status'] = 'critical'
                elif point['temperature'] >= warning_threshold:
                    point['status'] = 'warning'
                else:
                    point['status'] = 'normal'
            
            # Calculate trend (simple linear regression)
            n = len(temps)
            x_vals = list(range(n))
            x_mean = sum(x_vals) / n
            y_mean = mean_temp
            
            numerator = sum((x_vals[i] - x_mean) * (temps[i] - y_mean) for i in range(n))
            denominator = sum((x_vals[i] - x_mean)**2 for i in range(n))
            
            trend_slope = numerator / denominator if denominator != 0 else 0
            
            analysis = {
                'mean_temperature': mean_temp,
                'max_temperature': max_temp,
                'min_temperature': min_temp,
                'trend_slope': trend_slope,
                'trend_direction': 'increasing' if trend_slope > 0.1 else ('decreasing' if trend_slope < -0.1 else 'stable'),
                'warning_count': sum(1 for p in temperature_data if p['status'] == 'warning'),
                'critical_count': sum(1 for p in temperature_data if p['status'] == 'critical')
            }
        else:
            analysis = {}
        
        return jsonify({
            'press_name': press_name,
            'date': date,
            'temperature_data': temperature_data,
            'analysis': analysis,
            'count': len(temperature_data)
        }), 200
    
    except Exception as e:
        logging.error(f"Error in temperature analysis: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/efficiency-trends/<press_name>', methods=['GET'])
@login_required
def get_efficiency_trends(press_name):
    """
    Get efficiency trends over time for a specific press.
    Combines stroke rate, vibration, and temperature data.
    
    Query params:
    - start_date: YYYY-MM-DD
    - end_date: YYYY-MM-DD
    - report_type: 5min, 30min, or daily
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        report_type = request.args.get('report_type', '30min')
        
        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date required'}), 400
        
        # Generate date range
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        trends = []
        current = start_dt
        
        while current <= end_dt:
            date_str = current.strftime('%Y-%m-%d')
            prefix = f"press_reports/{report_type}/{press_name}/"
            blobs = list(bucket.list_blobs(prefix=prefix))
            
            day_data = []
            for blob in blobs:
                filename = blob.name.split('/')[-1]
                if filename.startswith(date_str) and filename.endswith('.json'):
                    try:
                        content = blob.download_as_text()
                        report = json.loads(content)
                        day_data.append(report)
                    except:
                        continue
            
            if day_data:
                aggregated = aggregate_report_data(day_data, report_type)
                aggregated['date'] = date_str
                trends.append(aggregated)
            
            current += timedelta(days=1)
        
        return jsonify({
            'press_name': press_name,
            'start_date': start_date,
            'end_date': end_date,
            'report_type': report_type,
            'trends': trends
        }), 200
    
    except Exception as e:
        logging.error(f"Error getting efficiency trends: {e}")
        return jsonify({'error': str(e)}), 500

# Add these new routes to press_portal.py

@press_portal_bp.route('/api/analytics/operation-analysis/<press_name>', methods=['GET'])
@login_required
def get_operation_analysis(press_name):
    """
    Get detailed analysis for a specific operation using reports from GCS.
    
    Query params:
    - start_time: Operation start timestamp (ISO format)
    - end_time: Operation end timestamp (ISO format)
    - granularity: '5min' or '30min' (default: 30min)
    """
    try:
        start_time = request.args.get('start_time')
        end_time = request.args.get('end_time')
        granularity = request.args.get('granularity', '30min')
        
        logging.info(f"[OPERATION ANALYSIS] Request: press={press_name}, start={start_time}, end={end_time}, granularity={granularity}")
        
        if not start_time or not end_time:
            return jsonify({'error': 'start_time and end_time required'}), 400
        
        # Parse timestamps - strip timezone and add IST
        import re
        try:
            start_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', start_time)
            end_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', end_time)
            
            start_dt = datetime.fromisoformat(start_time_clean).replace(tzinfo=IST)
            end_dt = datetime.fromisoformat(end_time_clean).replace(tzinfo=IST)
            
            logging.info(f"[OPERATION ANALYSIS] Parsed times: start={start_dt}, end={end_dt}")
        except Exception as e:
            logging.error(f"Error parsing timestamps: {e}")
            return jsonify({
                'error': f'Invalid timestamp format: {str(e)}',
                'press_name': press_name,
                'data_points': [],
                'aggregated': {'total_data_points': 0, 'granularity': granularity},
                'count': 0
            }), 200
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({
                'error': 'Storage not available',
                'press_name': press_name,
                'data_points': [],
                'aggregated': {'total_data_points': 0, 'granularity': granularity},
                'count': 0,
                'message': 'Could not connect to cloud storage'
            }), 200
        
        data_points = []
        
        # Determine which folder to use based on granularity
        if granularity == '5min':
            base_folder = 'processed'
        else:  # 30min
            base_folder = 'press_processed'
        
        # Calculate date range
        current_date = start_dt.date()
        end_date = end_dt.date()
        
        logging.info(f"[OPERATION ANALYSIS] Searching {base_folder} from {current_date} to {end_date}")
        
        while current_date <= end_date:
            year = current_date.year
            month = current_date.month
            day = current_date.day
            
            # Build correct path based on granularity
            if granularity == '5min':
                # processed/press_machine_NP1/2026-01-04.json (daily file with all 5min reports)
                date_str = current_date.strftime('%Y-%m-%d')
                blob_path = f"{base_folder}/{press_name}/{date_str}.json"
                
                try:
                    blob = bucket.blob(blob_path)
                    if blob.exists():
                        content = blob.download_as_text()
                        daily_data = json.loads(content)
                        
                        # This file contains array of 5min reports
                        if isinstance(daily_data, list):
                            reports = daily_data
                        elif isinstance(daily_data, dict) and 'data_points' in daily_data:
                            reports = daily_data['data_points']
                        else:
                            reports = [daily_data]
                        
                        # Filter by time range
                        for report in reports:
                            report_time_str = report.get('analysis_timestamp') or report.get('timestamp')
                            if report_time_str:
                                try:
                                    report_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', report_time_str)
                                    report_time = datetime.fromisoformat(report_time_clean).replace(tzinfo=IST)
                                    
                                    if start_dt <= report_time <= end_dt:
                                        data_points.append(report)
                                except:
                                    continue
                    else:
                        logging.info(f"[OPERATION ANALYSIS] File not found: {blob_path}")
                except Exception as e:
                    logging.error(f"Error reading {blob_path}: {e}")
            
            else:  # 30min
                # press_processed/press_machine_NP13/2026/01/24/*.json
                date_prefix = f"{base_folder}/{press_name}/{year:04d}/{month:02d}/{day:02d}/"
                
                try:
                    blobs = list(bucket.list_blobs(prefix=date_prefix))
                    logging.info(f"[OPERATION ANALYSIS] Found {len(blobs)} blobs in {date_prefix}")
                    
                    for blob in blobs:
                        if not blob.name.endswith('.json'):
                            continue
                        
                        try:
                            content = blob.download_as_text()
                            report = json.loads(content)
                            
                            report_time_str = report.get('analysis_timestamp') or report.get('timestamp')
                            if not report_time_str:
                                continue
                            
                            report_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', report_time_str)
                            report_time = datetime.fromisoformat(report_time_clean).replace(tzinfo=IST)
                            
                            if start_dt <= report_time <= end_dt:
                                data_points.append(report)
                                
                        except Exception as e:
                            logging.warning(f"Error reading {blob.name}: {e}")
                            continue
                except Exception as e:
                    logging.error(f"Error listing blobs for {date_prefix}: {e}")
            
            current_date += timedelta(days=1)
        
        # Sort by timestamp
        data_points.sort(key=lambda x: x.get('analysis_timestamp') or x.get('timestamp', ''))
        
        logging.info(f"[OPERATION ANALYSIS] Found {len(data_points)} data points")
        
        # Handle no data found - return 200 with empty result
        if len(data_points) == 0:
            return jsonify({
                'press_name': press_name,
                'start_time': start_time,
                'end_time': end_time,
                'granularity': granularity,
                'data_points': [],
                'aggregated': {
                    'total_data_points': 0,
                    'granularity': granularity
                },
                'count': 0,
                'message': f'No {granularity} data found for this operation time range. Data may not have been processed yet for this date.'
            }), 200
        
        # Aggregate data
        aggregated = aggregate_operation_data(data_points, granularity)
        
        return jsonify({
            'press_name': press_name,
            'start_time': start_time,
            'end_time': end_time,
            'granularity': granularity,
            'data_points': data_points,
            'aggregated': aggregated,
            'count': len(data_points)
        }), 200
    
    except Exception as e:
        logging.error(f"Error in operation analysis: {e}")
        import traceback
        traceback.print_exc()
        # Return 200 with error details instead of 500
        return jsonify({
            'error': str(e),
            'type': type(e).__name__,
            'press_name': press_name,
            'data_points': [],
            'aggregated': {'total_data_points': 0, 'granularity': granularity},
            'count': 0,
            'message': 'An error occurred while loading the analysis data'
        }), 200
    
@press_portal_bp.route('/api/reports/daily/available-dates/<press_name>', methods=['GET'])
@login_required
def get_daily_report_dates(press_name):
    """Get available dates for daily operation reports."""
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        # Scan for daily report files
        prefix = f"daily_press_report_operation/{press_name}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        dates = set()
        for blob in blobs:
            try:
                # Extract date from path: daily_press_report_operation/{press_name}/{year}/{month}/{day}_daily_report.json
                parts = blob.name.split('/')
                if len(parts) >= 5 and '_daily_report.json' in parts[-1]:
                    year = parts[2]
                    month = parts[3]
                    day = parts[4].replace('_daily_report.json', '')
                    date_str = f"{year}-{month}-{day}"
                    dates.add(date_str)
            except:
                continue
        
        sorted_dates = sorted(list(dates), reverse=True)
        
        return jsonify({
            'press_name': press_name,
            'available_dates': sorted_dates,
            'count': len(sorted_dates)
        }), 200
    
    except Exception as e:
        logging.error(f"Error getting dates: {e}")
        return jsonify({'error': str(e)}), 500


def aggregate_operation_data(data_points, granularity):
    """Aggregate operation analysis data from 5min reports."""
    if not data_points:
        return {}
    
    total_strokes = 0
    stroke_rates = []
    temperatures = []
    vibrations = []
    
    for point in data_points:
        # Handle 5min report structure
        stroke_count_data = point.get('stroke_count', {})
        if isinstance(stroke_count_data, dict):
            total_strokes += stroke_count_data.get('total_strokes', 0)
            rate = stroke_count_data.get('stroke_rate_per_minute')
            if rate is not None and rate > 0:
                stroke_rates.append(rate)
        
        temp_data = point.get('temperature', {})
        if isinstance(temp_data, dict):
            avg_temp = temp_data.get('avg')
            if avg_temp is not None and avg_temp > -100 and avg_temp < 200:
                temperatures.append(avg_temp)
        
        vib_data = point.get('vibration', {})
        if isinstance(vib_data, dict):
            avg_vib = vib_data.get('avg')
            if avg_vib is not None and avg_vib > 0:
                vibrations.append(avg_vib)
    
    aggregated = {
        'total_data_points': len(data_points),
        'granularity': granularity,
        'total_strokes': total_strokes
    }
    
    if stroke_rates:
        aggregated['stroke_rate'] = {
            'avg': sum(stroke_rates) / len(stroke_rates),
            'max': max(stroke_rates),
            'min': min(stroke_rates)
        }
    
    if temperatures:
        aggregated['temperature'] = {
            'avg': sum(temperatures) / len(temperatures),
            'max': max(temperatures),
            'min': min(temperatures)
        }
    
    if vibrations:
        aggregated['vibration'] = {
            'avg': sum(vibrations) / len(vibrations),
            'max': max(vibrations),
            'min': min(vibrations)
        }
    
    return aggregated
def aggregate_report_data(data_points, report_type):
    """
    Aggregate report data points into summary statistics.
    Handles vibration, temperature, stroke rate, and performance metrics.
    """
    if not data_points:
        return {}
    
    # Extract all metrics
    stroke_rates = []
    vibration_x = []
    vibration_y = []
    vibration_z = []
    temperatures = []
    strokes = []
    
    for point in data_points:
        if point.get('avg_stroke_rate') is not None:
            stroke_rates.append(point['avg_stroke_rate'])
        if point.get('avg_vibration_x') is not None:
            vibration_x.append(point['avg_vibration_x'])
        if point.get('avg_vibration_y') is not None:
            vibration_y.append(point['avg_vibration_y'])
        if point.get('avg_vibration_z') is not None:
            vibration_z.append(point['avg_vibration_z'])
        if point.get('avg_temperature') is not None:
            temperatures.append(point['avg_temperature'])
        if point.get('total_strokes') is not None:
            strokes.append(point['total_strokes'])
    
    aggregated = {
        'total_data_points': len(data_points),
        'report_type': report_type
    }
    
    # Stroke rate stats
    if stroke_rates:
        aggregated['stroke_rate'] = {
            'avg': sum(stroke_rates) / len(stroke_rates),
            'max': max(stroke_rates),
            'min': min(stroke_rates),
            'std_dev': calculate_std_dev(stroke_rates)
        }
    
    # Vibration stats
    if vibration_x and vibration_y and vibration_z:
        magnitudes = [(x**2 + y**2 + z**2)**0.5 
                      for x, y, z in zip(vibration_x, vibration_y, vibration_z)]
        
        aggregated['vibration'] = {
            'avg_x': sum(vibration_x) / len(vibration_x),
            'avg_y': sum(vibration_y) / len(vibration_y),
            'avg_z': sum(vibration_z) / len(vibration_z),
            'avg_magnitude': sum(magnitudes) / len(magnitudes),
            'max_magnitude': max(magnitudes),
            'std_dev': calculate_std_dev(magnitudes)
        }
    
    # Temperature stats
    if temperatures:
        aggregated['temperature'] = {
            'avg': sum(temperatures) / len(temperatures),
            'max': max(temperatures),
            'min': min(temperatures),
            'std_dev': calculate_std_dev(temperatures)
        }
    
    # Total strokes
    if strokes:
        aggregated['total_strokes'] = sum(strokes)
    
    # Calculate efficiency score (0-100)
    efficiency_factors = []
    
    if stroke_rates:
        # Higher stroke rate = better (normalize to 0-1)
        avg_rate = aggregated['stroke_rate']['avg']
        max_expected_rate = 60  # strokes per minute
        rate_score = min(avg_rate / max_expected_rate, 1.0)
        efficiency_factors.append(rate_score)
    
    if vibration_x:
        # Lower vibration = better (inverse normalize)
        avg_vib = aggregated['vibration']['avg_magnitude']
        max_acceptable_vib = 10.0
        vib_score = max(0, 1 - (avg_vib / max_acceptable_vib))
        efficiency_factors.append(vib_score)
    
    if temperatures:
        # Optimal temperature range: 40-70°C
        avg_temp = aggregated['temperature']['avg']
        if 40 <= avg_temp <= 70:
            temp_score = 1.0
        elif avg_temp < 40:
            temp_score = avg_temp / 40
        else:
            temp_score = max(0, 1 - ((avg_temp - 70) / 30))
        efficiency_factors.append(temp_score)
    
    if efficiency_factors:
        aggregated['efficiency_score'] = (sum(efficiency_factors) / len(efficiency_factors)) * 100
    
    return aggregated


def calculate_std_dev(values):
    """Calculate standard deviation of a list of values"""
    if not values or len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    variance = sum((x - mean)**2 for x in values) / len(values)
    return variance ** 0.5

@press_portal_bp.route('/analytics')
@login_required
def analytics_dashboard():
    """KBI Analytics Dashboard - Fleet-wide performance metrics"""
    preselected_press = request.args.get('press', '')
    return render_template('press_operations_analytics.html', preselected_press=preselected_press)


@press_portal_bp.route('/api/kbi/report/pdf/<date>', methods=['GET'])
@login_required
def get_kbi_report_pdf(date):
    """Download KBI daily report PDF for a specific date."""
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        if date_obj.tzinfo is None:
            date_obj = date_obj.replace(tzinfo=IST)
        
        pdf_bytes = load_kbi_daily_report_pdf(date_obj)
        
        if pdf_bytes:
            return send_file(
                BytesIO(pdf_bytes),
                mimetype='application/pdf',
                as_attachment=True,
                download_name=f'kbi_report_{date}.pdf'
            )
        
        return jsonify({'error': 'Report not found'}), 404
    
    except Exception as e:
        logging.error(f"Error serving PDF: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/kbi/report/data/<date>', methods=['GET'])
@login_required
def get_kbi_report_data(date):
    """Get KBI daily report data (parsed from PDF) for a specific date."""
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        if date_obj.tzinfo is None:
            date_obj = date_obj.replace(tzinfo=IST)
        
        pdf_bytes = load_kbi_daily_report_pdf(date_obj)
        
        if pdf_bytes:
            parsed_data = parse_kbi_report_pdf(pdf_bytes)
            parsed_data['date'] = date
            return jsonify(parsed_data)
        
        return jsonify({
            'date': date,
            'machines': [],
            'total_machines': 0,
            'error': 'Report not found'
        }), 404
    
    except Exception as e:
        logging.error(f"Error parsing KBI report: {e}")
        return jsonify({'error': str(e)}), 500

# Add these routes to press_portal.py

@press_portal_bp.route('/operations-analytics')
@login_required
def operations_analytics():
    """Operations Analytics Dashboard"""
    return render_template('press_operations_analytics.html')


@press_portal_bp.route('/api/reports/daily/<press_name>/<date>', methods=['GET'])
@login_required
def api_get_daily_operations_report(press_name, date):
    """Get daily operations report for a specific machine and date."""
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        if date_obj.tzinfo is None:
            date_obj = date_obj.replace(tzinfo=IST)
        
        # Load from daily operations report bucket
        year = date_obj.strftime('%Y')
        month = date_obj.strftime('%m')
        day = date_obj.strftime('%d')
        
        file_path = f"daily_press_report_operation/{press_name}/{year}/{month}/{day}_daily_report.json"
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        blob = bucket.blob(file_path)
        if not blob.exists():
            return jsonify({'error': 'Report not found'}), 404
        
        content = blob.download_as_text()
        report = json.loads(content)
        
        return jsonify(report), 200
    
    except Exception as e:
        logging.error(f"Error loading daily operations report: {e}")
        return jsonify({'error': str(e)}), 500
@press_portal_bp.route('/api/fleet/status', methods=['GET'])
@login_required
def get_fleet_status_with_kbi():
    """Get current status of all machines including KBI data."""
    try:
        # Get press list
        press_list_data = read_from_gcs_fresh(f"{PROCESSED_DATA_PREFIX}press_list.json")
        
        if not press_list_data:
            return jsonify({
                'total_machines': 0,
                'active': 0,
                'idle': 0,
                'total_strokes_today': 0,
                'total_runtime_hours': 0,
                'machines': []
            }), 200
        
        today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Load KBI report for today
        kbi_data = {}
        pdf_bytes = load_kbi_daily_report_pdf(today)
        if pdf_bytes:
            parsed = parse_kbi_report_pdf(pdf_bytes)
            kbi_data = {m['machine']: m for m in parsed['machines']}
        
        fleet_status = {
            'total_machines': 0,
            'active': 0,
            'idle': 0,
            'total_strokes_today': 0,
            'total_runtime_hours': 0,
            'machines': []
        }
        
        for press_info in press_list_data.get('presses', []):
            press_name = press_info.get('press_name')
            kbi_machine_data = kbi_data.get(press_name, {})
            
            # Determine status
            status = press_info.get('status', 'idle')
            if status == 'active':
                fleet_status['active'] += 1
            else:
                fleet_status['idle'] += 1
            
            fleet_status['total_machines'] += 1
            fleet_status['total_strokes_today'] += kbi_machine_data.get('strokes', 0)
            fleet_status['total_runtime_hours'] += kbi_machine_data.get('run_time_hours', 0)
            
            fleet_status['machines'].append({
                'name': press_name,
                'status': status,
                'today_strokes': kbi_machine_data.get('strokes', 0),
                'on_time_hours': kbi_machine_data.get('on_time_hours', 0),
                'run_time_hours': kbi_machine_data.get('run_time_hours', 0),
                'idle_time_hours': kbi_machine_data.get('idle_time_hours', 0),
                'changeovers': kbi_machine_data.get('changeovers', 0),
                'efficiency': kbi_machine_data.get('efficiency', 0)
            })
        
        return jsonify(fleet_status), 200
    
    except Exception as e:
        logging.error(f"Error getting fleet status: {e}")
        return jsonify({'error': str(e)}), 500
    
def get_active_operators():
    """
    Get list of operators currently assigned to active operations.
    Handles both single operators and comma-separated multiple operators.
    Returns dict: {operator_name: {press_name, component, operation, timestamp}}
    """
    bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
    if not bucket:
        return {}
    
    active_operators = {}
    now = datetime.now(IST)
    
    # Get all press names
    press_names = set()
    try:
        blobs = bucket.list_blobs(prefix=COMMAND_DATA_PREFIX, delimiter='/')
        for prefix in blobs.prefixes:
            if 'press_name=' in prefix:
                press_name = prefix.split('press_name=')[1].rstrip('/')
                press_names.add(press_name)
    except Exception as e:
        logging.error(f"Error listing press names: {e}")
        return {}
    
    # Check last command for each press
    for press_name in press_names:
        commands = get_press_commands(press_name, limit=1)
        if not commands:
            continue
        
        latest_cmd = commands[0]
        payload = latest_cmd.get('parsed_payload', {})
        status = (payload.get('status', '')).upper()
        
        # ✅ CRITICAL FIX: Only process START commands (not STOP!)
        if status != 'START':
            logging.info(f"[ACTIVE OPERATORS] Press {press_name} - Last command is {status} (operators FREE)")
            continue
        
        # ✅ CRITICAL FIX: Treat None/undefined as False for boolean checks
        is_cancelled = latest_cmd.get('cancelled') == True  # Explicit True check
        is_auto_cancelled = latest_cmd.get('auto_cancelled') == True  # Explicit True check
        is_confirmed = latest_cmd.get('confirmed') == True  # Explicit True check
        
        logging.info(f"[ACTIVE OPERATORS DEBUG] Press {press_name}: status={status}, confirmed={is_confirmed}, cancelled={is_cancelled}, auto_cancelled={is_auto_cancelled}")
        
        # ✅ Operator is BUSY if: START command AND confirmed AND not cancelled AND not auto_cancelled
        if is_confirmed and not is_cancelled and not is_auto_cancelled:
            operator = payload.get('operator', '')
            if operator:
                # Handle both single operator and comma-separated multiple
                operator_list = [op.strip() for op in operator.split(',')]
                
                for op_name in operator_list:
                    if op_name:  # Skip empty strings
                        active_operators[op_name] = {
                            'press_name': press_name,
                            'component': payload.get('component'),
                            'operation': payload.get('operation'),
                            'timestamp': latest_cmd.get('timestamp')
                        }
                
                logging.info(f"[ACTIVE OPERATORS] Press {press_name} has ACTIVE operators: {operator_list}")
        else:
            logging.info(f"[ACTIVE OPERATORS] Press {press_name} - START exists but not active (confirmed={is_confirmed}, cancelled={is_cancelled}, auto_cancelled={is_auto_cancelled})")
    
    return active_operators


def get_kbi_report_path(date: datetime) -> str:
    """Get the path to KBI daily report PDF for a specific date."""
    date_str = date.strftime('%Y_%m_%d')
    prefix = f"daily_reports/report_{date_str}_"
    return prefix


def load_kbi_daily_report_pdf(date: datetime):
    """Load KBI daily report PDF for a specific date."""
    try:
        from google.cloud import storage
        storage_client = storage.Client()
        reports_bucket = storage_client.bucket(REPORTS_BUCKET_NAME)
        
        prefix = get_kbi_report_path(date)
        blobs = list(reports_bucket.list_blobs(prefix=prefix))
        
        if blobs:
            latest_blob = sorted(blobs, key=lambda x: x.name)[-1]
            return latest_blob.download_as_bytes()
    except Exception as e:
        logging.error(f"Error loading KBI report PDF: {e}")
    
    return None


def parse_kbi_report_pdf(pdf_bytes: bytes) -> dict:
    """Parse KBI daily report PDF to extract structured data."""
    import PyPDF2
    import re
    
    try:
        pdf_file = BytesIO(pdf_bytes)
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        text = pdf_reader.pages[0].extract_text()
        
        machines_data = []
        lines = text.split('\n')
        
        for line in lines:
            if 'press_machine_' in line:
                parts = [p.strip() for p in line.split('|')]
                parts = [p for p in parts if p]
                
                if len(parts) >= 6:
                    try:
                        machine_name = parts[0]
                        strokes = int(parts[1]) if parts[1] and parts[1] != '—' else 0
                        on_time = float(parts[2]) if parts[2] and parts[2] != '—' else 0.0
                        run_time = float(parts[3]) if parts[3] and parts[3] != '—' else 0.0
                        idle_time = float(parts[4]) if parts[4] and parts[4] != '—' else 0.0
                        changeovers = int(parts[5]) if parts[5] and parts[5] != '—' else 0
                        
                        machines_data.append({
                            'machine': machine_name,
                            'strokes': strokes,
                            'on_time_hours': on_time,
                            'run_time_hours': run_time,
                            'idle_time_hours': idle_time,
                            'changeovers': changeovers,
                            'efficiency': round((run_time / on_time * 100), 2) if on_time > 0 else 0
                        })
                    except (ValueError, IndexError) as e:
                        logging.warning(f"Error parsing line: {line} - {e}")
                        continue
        
        date_match = re.search(r'From\s*:\s*(\d+\s+\w+\s+\d+)', text)
        report_date = date_match.group(1) if date_match else None
        
        return {
            'report_date': report_date,
            'machines': machines_data,
            'total_machines': len(machines_data)
        }
    
    except Exception as e:
        logging.error(f"Error parsing KBI report PDF: {e}")
        return {'machines': [], 'total_machines': 0}
    

@press_portal_bp.route('/api/operators/available', methods=['GET'])
@login_required
def get_available_operators():
    """
    Get list of available operators (not currently assigned to any press).
    Returns all operators with their availability status.
    """
    try:
        # Get all operators from HR workers database
        all_operators = []
        main_app_bucket = get_gcs_bucket('kbi-first')
        if main_app_bucket:
            hr_blob = main_app_bucket.blob("hr_workers/hr_workers.json")
            if hr_blob.exists():
                hr_workers = json.loads(hr_blob.download_as_text())
                all_operators = sorted(set(
                    w.get('operator_name', '').strip().upper()
                    for w in hr_workers
                    if w.get('is_active') and w.get('department') == 'Press Shop' and w.get('operator_name')
                ))
        
        # Get active operators
        active_operators = get_active_operators()
        
        # Build response with availability status
        operators_list = []
        for operator_name in all_operators:
            if operator_name in active_operators:
                operators_list.append({
                    'name': operator_name,
                    'available': False,
                    'assigned_to': active_operators[operator_name]
                })
            else:
                operators_list.append({
                    'name': operator_name,
                    'available': True
                })
        
        return jsonify({
            'operators': operators_list,
            'timestamp': datetime.now(IST).isoformat()
        }), 200
    
    except Exception as e:
        logging.error(f"Error getting available operators: {e}")
        return jsonify({'error': str(e)}), 500
    
"""
Press Portal Analytics API Additions
=====================================
Add these routes and functions to your press_portal.py file.

These endpoints provide:
1. V2 Analytics APIs that combine daily operations reports, 30-min reports, and KBI PDF data
2. Proper handling of missing data
3. Alert thresholds for temperature and vibration

INSTRUCTIONS:
1. Add the imports at the top of press_portal.py
2. Add the ALERT_THRESHOLDS constant after your existing constants
3. Add the helper functions (parse_kbi_report_pdf_v2, get_kbi_data_for_date, generate_day_alerts)
4. Add all the route functions to your blueprint
"""




# ==================== ADD THESE HELPER FUNCTIONS ====================

def parse_kbi_report_pdf_v2(pdf_bytes):
    """
    Parse KBI daily report PDF to extract structured data.
    
    Expected PDF format (from screenshot):
    KBI – Daily Press Performance Report
    ======================================
    Report Window (IST):
    From : 12 Jan 2026 07:25 PM
    To   : 13 Jan 2026 07:25 PM
    --------------------------------------
    | Machine          | Strokes | ON (hrs) | RUN (hrs) | IDLE (hrs) |
    --------------------------------------
    | press_machine_NP1 |   542  |   4.66   |   0.74    |    3.92    |
    ...
    
    Returns dict with machine data including on_time, run_time, idle_time, efficiency
    """
    try:
        import PyPDF2
        
        pdf_file = io.BytesIO(pdf_bytes)
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        
        # Extract text from all pages
        full_text = ""
        for page in pdf_reader.pages:
            full_text += page.extract_text() + "\n"
        
        machines_data = {}
        
        # Parse the report window dates
        report_from = None
        report_to = None
        
        from_match = re.search(r'From\s*:\s*(\d+\s+\w+\s+\d+\s+\d+:\d+\s*(?:AM|PM)?)', full_text, re.IGNORECASE)
        to_match = re.search(r'To\s*:\s*(\d+\s+\w+\s+\d+\s+\d+:\d+\s*(?:AM|PM)?)', full_text, re.IGNORECASE)
        
        if from_match:
            report_from = from_match.group(1)
        if to_match:
            report_to = to_match.group(1)
        
        # Parse machine data lines
        lines = full_text.split('\n')
        
        for line in lines:
            # Skip non-data lines
            if 'press_machine_' not in line:
                continue
            
            # Clean and split the line
            parts = [p.strip() for p in line.split('|')]
            parts = [p for p in parts if p]  # Remove empty strings
            
            if len(parts) < 5:
                continue
            
            try:
                machine_name = parts[0].strip()
                
                if not machine_name.startswith('press_machine_'):
                    continue
                
                # Parse values, handling "—" as None/0
                def parse_value(val, is_int=False):
                    val = val.strip()
                    if val in ['—', '-', '', 'N/A', 'null']:
                        return 0 if is_int else 0.0
                    try:
                        return int(val) if is_int else float(val)
                    except (ValueError, TypeError):
                        return 0 if is_int else 0.0
                
                strokes = parse_value(parts[1], is_int=True)
                on_time = parse_value(parts[2])
                run_time = parse_value(parts[3])
                idle_time = parse_value(parts[4])
                
                # Parse changeovers if present (optional column)
                changeovers = 0
                if len(parts) >= 6:
                    changeovers = parse_value(parts[5], is_int=True)
                
                # Calculate efficiency
                efficiency = 0.0
                if on_time > 0:
                    efficiency = round((run_time / on_time) * 100, 2)
                
                machines_data[machine_name] = {
                    'machine': machine_name,
                    'strokes': strokes,
                    'on_time_hours': on_time,
                    'run_time_hours': run_time,
                    'idle_time_hours': idle_time,
                    'changeovers': changeovers,
                    'efficiency': efficiency
                }
                
            except (IndexError, ValueError) as e:
                logging.warning(f"Error parsing KBI line: {line} - {e}")
                continue
        
        return {
            'report_from': report_from,
            'report_to': report_to,
            'machines': machines_data,
            'total_machines': len(machines_data)
        }
    
    except Exception as e:
        logging.error(f"Error parsing KBI report PDF: {e}")
        import traceback
        traceback.print_exc()
        return {'machines': {}, 'total_machines': 0, 'error': str(e)}


def get_kbi_data_for_date(date_str, press_name):
    """
    Get KBI data for a specific machine on a specific date.
    
    Note: KBI reports are generated at 7:30 PM for the last 24 hours,
    so data may not align perfectly with calendar dates.
    
    Args:
        date_str: Date in YYYY-MM-DD format
        press_name: Machine name (e.g., 'press_machine_NP1')
    
    Returns:
        Dict with on_time_hours, run_time_hours, idle_time_hours, efficiency, strokes
        or empty dict if not found
    """
    try:
        from google.cloud import storage
        
        storage_client = storage.Client()
        reports_bucket = storage_client.bucket(REPORTS_BUCKET_NAME)
        
        # Parse the date
        target_date = datetime.strptime(date_str, '%Y-%m-%d')
        
        # KBI reports are generated at 7:30 PM, so:
        # - For date X, check report generated on date X (covers X-1 7:30PM to X 7:30PM)
        # - Also check report generated on date X+1 (covers X 7:30PM to X+1 7:30PM)
        
        dates_to_check = [
            target_date,
            target_date + timedelta(days=1)
        ]
        
        for check_date in dates_to_check:
            prefix = f"daily_reports/report_{check_date.strftime('%Y_%m_%d')}_"
            
            blobs = list(reports_bucket.list_blobs(prefix=prefix))
            
            if not blobs:
                continue
            
            # Get the latest report for this date
            latest_blob = sorted(blobs, key=lambda x: x.name)[-1]
            
            pdf_bytes = latest_blob.download_as_bytes()
            parsed = parse_kbi_report_pdf_v2(pdf_bytes)
            
            if press_name in parsed.get('machines', {}):
                return parsed['machines'][press_name]
        
        return {}
    
    except Exception as e:
        logging.error(f"Error getting KBI data for {press_name} on {date_str}: {e}")
        return {}


def generate_day_alerts(data):
    """
    Generate alerts from day analysis data.
    Checks temperature, vibration, and anomalies from 30-min reports.
    
    Args:
        data: Dict containing thirty_min_data and daily_report
    
    Returns:
        List of alert dicts with type, icon, title, message, time
    """
    alerts = []
    thirty_min_data = data.get('thirty_min_data', [])
    
    for d in thirty_min_data:
        time_str = ''
        timestamp = d.get('analysis_timestamp') or d.get('timestamp')
        if timestamp:
            try:
                time_str = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%H:%M')
            except:
                time_str = timestamp
        
        # Temperature alerts
        temp_data = d.get('temperature', {})
        temp = temp_data.get('avg') if isinstance(temp_data, dict) else None
        
        if temp is not None and temp > -100:  # Filter out invalid readings
            if temp > ALERT_THRESHOLDS['temperature']['critical']:
                alerts.append({
                    'type': 'critical',
                    'icon': '🔥',
                    'title': 'Critical Temperature',
                    'message': f"Temperature reached {temp:.1f}°C (threshold: {ALERT_THRESHOLDS['temperature']['critical']}°C)",
                    'time': time_str
                })
            elif temp > ALERT_THRESHOLDS['temperature']['warning']:
                alerts.append({
                    'type': 'warning',
                    'icon': '⚠️',
                    'title': 'High Temperature',
                    'message': f"Temperature at {temp:.1f}°C (warning: {ALERT_THRESHOLDS['temperature']['warning']}°C)",
                    'time': time_str
                })
        
        # Vibration alerts
        vib_data = d.get('vibration', {})
        vib = vib_data.get('avg') if isinstance(vib_data, dict) else None
        
        if vib is not None and vib > 0:
            if vib > ALERT_THRESHOLDS['vibration']['critical']:
                alerts.append({
                    'type': 'critical',
                    'icon': '📳',
                    'title': 'Critical Vibration',
                    'message': f"Vibration level at {vib:.2f} (threshold: {ALERT_THRESHOLDS['vibration']['critical']})",
                    'time': time_str
                })
            elif vib > ALERT_THRESHOLDS['vibration']['warning']:
                alerts.append({
                    'type': 'warning',
                    'icon': '📳',
                    'title': 'High Vibration',
                    'message': f"Vibration level at {vib:.2f} (warning: {ALERT_THRESHOLDS['vibration']['warning']})",
                    'time': time_str
                })
        
        # Anomalies from 30-min reports
        anomalies = d.get('anomalies', [])
        for anomaly in anomalies:
            alerts.append({
                'type': 'info',
                'icon': 'ℹ️',
                'title': 'Anomaly Detected',
                'message': anomaly,
                'time': time_str
            })
    
    return alerts


# ==================== ADD THESE API ROUTES ====================
# (Add these to your press_portal_bp Blueprint)

@press_portal_bp.route('/api/analytics/v2/available-dates/<press_name>', methods=['GET'])
@login_required
def api_v2_available_dates(press_name):
    """
    Get available dates for analytics for a specific press.
    Checks all three data sources so dates with any data are shown:
      1. daily_press_report_operation  (daily reports)
      2. processed/{press_name}/       (5-min timeseries YYYY-MM-DD.json)
    """
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500

        dates = set()

        # --- Source 1: daily report files ---
        try:
            prefix = f"daily_press_report_operation/{press_name}/"
            for blob in bucket.list_blobs(prefix=prefix):
                try:
                    # Path: daily_press_report_operation/{press_name}/{year}/{month}/{day}_daily_report.json
                    parts = blob.name.split('/')
                    if len(parts) >= 5 and '_daily_report.json' in parts[-1]:
                        year = parts[2]
                        month = parts[3]
                        day = parts[4].replace('_daily_report.json', '')
                        date_str = f"{year}-{month}-{day}"
                        datetime.strptime(date_str, '%Y-%m-%d')
                        dates.add(date_str)
                except (IndexError, ValueError):
                    continue
        except Exception as e:
            logging.warning(f"Could not scan daily_report dates for {press_name}: {e}")

        # --- Source 2: 5-min timeseries files (processed/{press_name}/YYYY-MM-DD.json) ---
        try:
            prefix = f"{PROCESSED_DATA_PREFIX}{press_name}/"
            for blob in bucket.list_blobs(prefix=prefix):
                filename = blob.name.replace(prefix, '')
                # Match exactly YYYY-MM-DD.json (15 chars)
                if filename.endswith('.json') and len(filename) == 15:
                    date_str = filename[:-5]
                    try:
                        datetime.strptime(date_str, '%Y-%m-%d')
                        dates.add(date_str)
                    except ValueError:
                        continue
        except Exception as e:
            logging.warning(f"Could not scan 5-min dates for {press_name}: {e}")

        sorted_dates = sorted(list(dates), reverse=True)

        return jsonify({
            'press_name': press_name,
            'available_dates': sorted_dates,
            'count': len(sorted_dates)
        }), 200

    except Exception as e:
        logging.error(f"Error getting available dates: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/v2/day-analysis/<press_name>/<date>', methods=['GET'])
@login_required
def api_v2_day_analysis(press_name, date):
    """
    Get comprehensive day analysis combining:
    1. Daily operations report (24-hour summary with operations list)
    2. 30-minute reports (time-series data throughout the day)
    3. KBI PDF data (on time, run time, idle time, efficiency)
    
    Returns:
    {
        "press_name": "...",
        "date": "...",
        "daily_report": { ... },      // From daily_press_report_operation
        "thirty_min_data": [ ... ],   // From press_processed
        "kbi_data": { ... },          // From kbi-daily-reports PDF
        "alerts": [ ... ],            // Generated alerts
        "data_available": true/false
    }
    """
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        # Validate date format
        try:
            date_obj = datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        year = date_obj.strftime('%Y')
        month = date_obj.strftime('%m')
        day = date_obj.strftime('%d')
        
        result = {
            'press_name': press_name,
            'date': date,
            'daily_report': None,
            'thirty_min_data': [],
            'five_min_data': [],
            'five_min_aggregated': {},
            'command_history': [],
            'kbi_data': {},
            'alerts': [],
            'data_available': False
        }
        
        # 1. Load daily operations report
        daily_report_path = f"daily_press_report_operation/{press_name}/{year}/{month}/{day}_daily_report.json"
        
        try:
            blob = bucket.blob(daily_report_path)
            if blob.exists():
                content = blob.download_as_text()
                result['daily_report'] = json.loads(content)
                result['data_available'] = True
        except Exception as e:
            logging.warning(f"Error loading daily report for {press_name}/{date}: {e}")
        
        # 2. Load 30-minute reports from press_processed folder
        thirty_min_prefix = f"press_processed/{press_name}/{year}/{month}/{day}/"
        
        try:
            blobs = list(bucket.list_blobs(prefix=thirty_min_prefix))
            
            for blob in blobs:
                if not blob.name.endswith('.json'):
                    continue
                
                try:
                    content = blob.download_as_text()
                    data = json.loads(content)
                    result['thirty_min_data'].append(data)
                except Exception as e:
                    logging.warning(f"Error reading 30-min file {blob.name}: {e}")
                    continue
            
            # Sort by timestamp
            result['thirty_min_data'].sort(
                key=lambda x: x.get('analysis_timestamp') or x.get('timestamp', '')
            )
            
            if result['thirty_min_data']:
                result['data_available'] = True
                
        except Exception as e:
            logging.warning(f"Error loading 30-min reports for {press_name}/{date}: {e}")
        
        # 3. Load 5-min timeseries data from processed/{press_name}/{date}.json
        try:
            five_min_path = f"{PROCESSED_DATA_PREFIX}{press_name}/{date}.json"
            blob = bucket.blob(five_min_path)
            if blob.exists():
                raw = json.loads(blob.download_as_text())
                if isinstance(raw, list):
                    pts = raw
                elif isinstance(raw, dict) and 'data_points' in raw:
                    pts = raw['data_points']
                else:
                    pts = [raw]
                pts.sort(key=lambda x: x.get('timestamp') or x.get('analysis_timestamp', ''))
                result['five_min_data'] = pts
                result['five_min_aggregated'] = aggregate_five_min_data(pts)
                if pts:
                    result['data_available'] = True
        except Exception as e:
            logging.warning(f"Error loading 5-min data for {press_name}/{date}: {e}")

        # 5. Load command history (last 20 commands, searches last 7 days)
        try:
            result['command_history'] = get_press_commands(press_name, limit=20)
        except Exception as e:
            logging.warning(f"Error loading command history for {press_name}/{date}: {e}")

        # 6. Load KBI data from PDF
        try:
            kbi_data = get_kbi_data_for_date(date, press_name)
            if kbi_data:
                result['kbi_data'] = kbi_data
        except Exception as e:
            logging.warning(f"Error loading KBI data for {press_name}/{date}: {e}")

        # 7. Generate alerts from the data
        result['alerts'] = generate_day_alerts(result)

        if not result['data_available']:
            return jsonify({
                'press_name': press_name,
                'date': date,
                'data_available': False,
                'message': 'No data available for this date'
            }), 200

        return jsonify(result), 200

    except Exception as e:
        logging.error(f"Error in day analysis: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/v2/operation-detail/<press_name>', methods=['GET'])
@login_required  
def api_v2_operation_detail(press_name):
    """
    Get detailed analysis for a specific operation using 30-minute reports
    that fall within the operation's time window.
    
    Query params:
    - start_time: Operation start timestamp (ISO format)
    - end_time: Operation end timestamp (ISO format)
    
    Returns:
    {
        "press_name": "...",
        "start_time": "...",
        "end_time": "...",
        "thirty_min_data": [ ... ],  // 30-min reports within this window
        "aggregated": { ... },       // Aggregated metrics
        "alerts": [ ... ]
    }
    """
    try:
        start_time = request.args.get('start_time')
        end_time = request.args.get('end_time')
        
        if not start_time or not end_time:
            return jsonify({'error': 'start_time and end_time required'}), 400
        
        # Parse timestamps
        try:
            start_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', start_time)
            end_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', end_time)
            
            start_dt = datetime.fromisoformat(start_time_clean).replace(tzinfo=IST)
            end_dt = datetime.fromisoformat(end_time_clean).replace(tzinfo=IST)
        except Exception as e:
            return jsonify({
                'error': f'Invalid timestamp format: {str(e)}',
                'thirty_min_data': [],
                'aggregated': {}
            }), 200
        
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({
                'error': 'Storage not available',
                'thirty_min_data': [],
                'aggregated': {}
            }), 200
        
        data_points = []
        
        # Calculate date range
        current_date = start_dt.date()
        end_date = end_dt.date()
        
        while current_date <= end_date:
            year = current_date.year
            month = current_date.month
            day = current_date.day
            
            # Load from press_processed folder (30-min reports)
            date_prefix = f"press_processed/{press_name}/{year:04d}/{month:02d}/{day:02d}/"
            
            try:
                blobs = list(bucket.list_blobs(prefix=date_prefix))
                
                for blob in blobs:
                    if not blob.name.endswith('.json'):
                        continue
                    
                    try:
                        content = blob.download_as_text()
                        report = json.loads(content)
                        
                        report_time_str = report.get('analysis_timestamp') or report.get('timestamp')
                        if not report_time_str:
                            continue
                        
                        report_time_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', report_time_str)
                        report_time = datetime.fromisoformat(report_time_clean).replace(tzinfo=IST)
                        
                        if start_dt <= report_time <= end_dt:
                            data_points.append(report)
                            
                    except Exception as e:
                        logging.warning(f"Error reading {blob.name}: {e}")
                        continue
            except Exception as e:
                logging.error(f"Error listing blobs for {date_prefix}: {e}")
            
            current_date += timedelta(days=1)
        
        # Sort by timestamp
        data_points.sort(key=lambda x: x.get('analysis_timestamp') or x.get('timestamp', ''))
        
        # Aggregate data
        aggregated = aggregate_thirty_min_data(data_points)
        
        # Generate alerts
        alerts = []
        for d in data_points:
            time_str = ''
            timestamp = d.get('analysis_timestamp') or d.get('timestamp')
            if timestamp:
                try:
                    time_str = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%H:%M')
                except:
                    pass
            
            # Check temperature
            temp_data = d.get('temperature', {})
            temp = temp_data.get('avg') if isinstance(temp_data, dict) else None
            if temp and temp > ALERT_THRESHOLDS['temperature']['warning']:
                alerts.append({
                    'type': 'warning' if temp <= ALERT_THRESHOLDS['temperature']['critical'] else 'critical',
                    'icon': '🔥',
                    'title': 'High Temperature',
                    'message': f"Temperature at {temp:.1f}°C",
                    'time': time_str
                })
            
            # Check vibration
            vib_data = d.get('vibration', {})
            vib = vib_data.get('avg') if isinstance(vib_data, dict) else None
            if vib and vib > ALERT_THRESHOLDS['vibration']['warning']:
                alerts.append({
                    'type': 'warning' if vib <= ALERT_THRESHOLDS['vibration']['critical'] else 'critical',
                    'icon': '📳',
                    'title': 'High Vibration',
                    'message': f"Vibration at {vib:.2f}",
                    'time': time_str
                })
        
        # Load 5-min data filtered within the operation's time window
        five_min_points = []
        try:
            op_current_date = start_dt.date()
            op_end_date = end_dt.date()
            while op_current_date <= op_end_date:
                date_str = op_current_date.strftime('%Y-%m-%d')
                five_min_path = f"{PROCESSED_DATA_PREFIX}{press_name}/{date_str}.json"
                blob = bucket.blob(five_min_path)
                if blob.exists():
                    raw = json.loads(blob.download_as_text())
                    if isinstance(raw, list):
                        pts = raw
                    elif isinstance(raw, dict) and 'data_points' in raw:
                        pts = raw['data_points']
                    else:
                        pts = [raw]
                    for pt in pts:
                        ts_str = pt.get('timestamp') or pt.get('analysis_timestamp', '')
                        if ts_str:
                            try:
                                ts_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', ts_str)
                                ts = datetime.fromisoformat(ts_clean).replace(tzinfo=IST)
                                if start_dt <= ts <= end_dt:
                                    five_min_points.append(pt)
                            except Exception:
                                pass
                op_current_date += timedelta(days=1)
            five_min_points.sort(key=lambda x: x.get('timestamp') or x.get('analysis_timestamp', ''))
        except Exception as e:
            logging.warning(f"Error loading 5-min data for operation {press_name}: {e}")

        return jsonify({
            'press_name': press_name,
            'start_time': start_time,
            'end_time': end_time,
            'thirty_min_data': data_points,
            'five_min_data': five_min_points,
            'five_min_aggregated': aggregate_five_min_data(five_min_points),
            'aggregated': aggregated,
            'alerts': alerts,
            'count': len(data_points)
        }), 200

    except Exception as e:
        logging.error(f"Error in operation detail: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': str(e),
            'thirty_min_data': [],
            'five_min_data': [],
            'aggregated': {}
        }), 200


def aggregate_thirty_min_data(data_points):
    """
    Aggregate 30-minute report data into summary statistics.
    """
    if not data_points:
        return {'total_data_points': 0}
    
    total_strokes = 0
    stroke_rates = []
    temperatures = []
    vibrations = []
    
    for point in data_points:
        # Handle stroke count data
        stroke_data = point.get('stroke_count', {})
        if isinstance(stroke_data, dict):
            total_strokes += stroke_data.get('total_strokes', 0)
            rate = stroke_data.get('stroke_rate_per_minute')
            if rate is not None and rate > 0:
                stroke_rates.append(rate)
        
        # Handle temperature data
        temp_data = point.get('temperature', {})
        if isinstance(temp_data, dict):
            avg_temp = temp_data.get('avg')
            if avg_temp is not None and avg_temp > -100 and avg_temp < 200:
                temperatures.append(avg_temp)
        
        # Handle vibration data
        vib_data = point.get('vibration', {})
        if isinstance(vib_data, dict):
            avg_vib = vib_data.get('avg')
            if avg_vib is not None and avg_vib > 0:
                vibrations.append(avg_vib)
    
    aggregated = {
        'total_data_points': len(data_points),
        'total_strokes': total_strokes
    }
    
    if stroke_rates:
        aggregated['stroke_rate'] = {
            'avg': sum(stroke_rates) / len(stroke_rates),
            'max': max(stroke_rates),
            'min': min(stroke_rates)
        }
    
    if temperatures:
        aggregated['temperature'] = {
            'avg': sum(temperatures) / len(temperatures),
            'max': max(temperatures),
            'min': min(temperatures)
        }
    
    if vibrations:
        aggregated['vibration'] = {
            'avg': sum(vibrations) / len(vibrations),
            'max': max(vibrations),
            'min': min(vibrations)
        }
    
    return aggregated


def aggregate_five_min_data(data_points):
    """
    Aggregate 5-minute timeseries data into summary statistics.
    Skips temperature values below -100 (sensor errors) and vibration values of 0.
    """
    if not data_points:
        return {'total_data_points': 0}

    stroke_rates = []
    temperatures = []
    vibrations = []

    for point in data_points:
        rate = point.get('stroke_rate')
        if rate is not None and rate > 0:
            stroke_rates.append(rate)

        temp = point.get('temperature')
        if isinstance(temp, dict):
            t = temp.get('avg')
        else:
            t = temp
        if t is not None and t > -100:
            temperatures.append(t)

        vib = point.get('vibration')
        if isinstance(vib, dict):
            v = vib.get('avg')
        else:
            v = vib
        if v is not None and v > 0:
            vibrations.append(v)

    aggregated = {'total_data_points': len(data_points)}

    if stroke_rates:
        aggregated['avg_stroke_rate'] = sum(stroke_rates) / len(stroke_rates)
        aggregated['max_stroke_rate'] = max(stroke_rates)

    if temperatures:
        aggregated['avg_temperature'] = sum(temperatures) / len(temperatures)
        aggregated['max_temperature'] = max(temperatures)

    if vibrations:
        aggregated['avg_vibration'] = sum(vibrations) / len(vibrations)
        aggregated['max_vibration'] = max(vibrations)

    return aggregated


@press_portal_bp.route('/api/analytics/v2/kbi-data/<press_name>/<date>', methods=['GET'])
@login_required
def api_v2_kbi_data(press_name, date):
    """
    Get KBI data (from PDF) for a specific machine and date.
    Returns on_time, run_time, idle_time, efficiency, strokes.
    """
    try:
        kbi_data = get_kbi_data_for_date(date, press_name)
        
        if kbi_data:
            return jsonify({
                'press_name': press_name,
                'date': date,
                'data': kbi_data,
                'available': True
            }), 200
        else:
            return jsonify({
                'press_name': press_name,
                'date': date,
                'data': {},
                'available': False,
                'message': 'No KBI data found for this date'
            }), 200
    
    except Exception as e:
        logging.error(f"Error getting KBI data: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/api/analytics/v2/export/csv/<press_name>/<date>', methods=['GET'])
@login_required
def api_v2_export_csv(press_name, date):
    """
    Export day analysis data as CSV.
    """
    try:
        bucket = get_gcs_bucket(COMMANDS_BUCKET_NAME)
        if not bucket:
            return jsonify({'error': 'Storage not available'}), 500
        
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        year = date_obj.strftime('%Y')
        month = date_obj.strftime('%m')
        day = date_obj.strftime('%d')
        
        # Load 30-min data
        thirty_min_prefix = f"press_processed/{press_name}/{year}/{month}/{day}/"
        data_points = []
        
        blobs = list(bucket.list_blobs(prefix=thirty_min_prefix))
        for blob in blobs:
            if blob.name.endswith('.json'):
                try:
                    content = blob.download_as_text()
                    data = json.loads(content)
                    data_points.append(data)
                except:
                    continue
        
        data_points.sort(key=lambda x: x.get('analysis_timestamp') or x.get('timestamp', ''))
        
        # Build CSV
        csv_lines = ['Timestamp,Stroke Rate (SPM),Total Strokes,Temperature (°C),Vibration']
        
        for d in data_points:
            timestamp = d.get('analysis_timestamp') or d.get('timestamp', '')
            
            stroke_data = d.get('stroke_count', {})
            stroke_rate = stroke_data.get('stroke_rate_per_minute', '') if isinstance(stroke_data, dict) else ''
            total_strokes = stroke_data.get('total_strokes', '') if isinstance(stroke_data, dict) else ''
            
            temp_data = d.get('temperature', {})
            temp = temp_data.get('avg', '') if isinstance(temp_data, dict) else ''
            if temp == -127:
                temp = ''
            
            vib_data = d.get('vibration', {})
            vib = vib_data.get('avg', '') if isinstance(vib_data, dict) else ''
            
            csv_lines.append(f"{timestamp},{stroke_rate},{total_strokes},{temp},{vib}")
        
        csv_content = '\n'.join(csv_lines)
        
        return Response(
            csv_content,
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename="{press_name}_{date}_analysis.csv"',
                'Cache-Control': 'no-cache'
            }
        )
    
    except Exception as e:
        logging.error(f"Error exporting CSV: {e}")
        return jsonify({'error': str(e)}), 500


@press_portal_bp.route('/production_scheduling')
@login_required
def production_scheduling():
    """Renders the Production Scheduling (Pre-Production) page."""
    return render_template('press_portal/production_scheduling.html', page_title='Production Scheduling')

@press_portal_bp.route('/api/recommend-machine', methods=['POST'])
@login_required
def recommend_machine():
    """
    API endpoint to recommend machines for a specific component and operation.
    """
    data = request.get_json()
    component = data.get('component')
    operation = data.get('operation')
    
    if not component or not operation:
        return jsonify({"error": "Component and Operation are required"}), 400
        
    try:
        recommendations = get_machine_recommendations(component, operation)
        return jsonify(recommendations), 200
    except Exception as e:
        logging.error(f"Error recommending machine: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# PRODUCTION SCHEDULING API ENDPOINTS
# ==============================================================================

@press_portal_bp.route('/api/scheduling/upload', methods=['POST'])
@login_required
def scheduling_upload():
    """
    Upload a customer schedule Excel file and parse it.
    Returns the parsed schedule data with part names and daily quantities.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    
    if not file.filename.endswith(('.xls', '.xlsx')):
        return jsonify({'error': 'Please upload an Excel file (.xls or .xlsx)'}), 400
    
    try:
        file_content = file.read()
        schedule_data = parse_customer_schedule(file_content, file.filename)
        
        # Store in session for subsequent steps
        session['pending_schedule'] = json.dumps(schedule_data, default=str)
        
        # Save uploaded file to GCS
        bucket = get_gcs_bucket()
        if bucket:
            timestamp = datetime.now(IST).strftime('%Y%m%d_%H%M%S')
            gcs_path = f"{CUSTOMER_SCHEDULES_FOLDER}{timestamp}_{file.filename}"
            blob = bucket.blob(gcs_path)
            blob.upload_from_string(file_content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        
        return jsonify(schedule_data), 200
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"Error processing schedule upload: {e}", exc_info=True)
        return jsonify({'error': f'Failed to process file: {str(e)}'}), 500


@press_portal_bp.route('/api/scheduling/match-parts', methods=['POST'])
@login_required
def scheduling_match_parts():
    """
    Match KBIPL Part Names from the uploaded schedule to norms components.
    Accepts optional user corrections for unmatched/fuzzy parts.
    """
    data = request.get_json() or {}
    schedule_parts = data.get('parts', [])
    user_mappings = data.get('user_mappings', {})  # Manual corrections
    
    if not schedule_parts:
        return jsonify({'error': 'No parts data provided'}), 400
    
    bucket = get_gcs_bucket()
    if not bucket:
        return jsonify({'error': 'Could not connect to GCS'}), 500
    
    try:
        # Get operations data (norms)
        operations_data = read_from_gcs(OPERATIONS_FILE_NAME, PRESSES_BUCKET_NAME) or []
        if isinstance(operations_data, dict):
            operations_data = list(operations_data.values())
        
        if not operations_data:
            return jsonify({'error': 'No norms data available. Please upload norms first.'}), 400
        
        # Load saved mappings from GCS
        saved_mappings = {}
        try:
            mapping_blob = bucket.blob(PART_MAPPING_FILE)
            if mapping_blob.exists():
                saved_mappings = json.loads(mapping_blob.download_as_text()) or {}
        except Exception:
            pass
        
        # Merge user corrections into saved mappings
        if user_mappings:
            saved_mappings.update(user_mappings)
            # Save updated mappings to GCS
            try:
                mapping_blob = bucket.blob(PART_MAPPING_FILE)
                mapping_blob.upload_from_string(
                    json.dumps(saved_mappings, indent=2),
                    content_type='application/json'
                )
            except Exception as e:
                logging.warning(f"Could not save part mappings: {e}")
        
        # Run matching
        match_results = match_parts_to_components(schedule_parts, operations_data, saved_mappings)
        
        # Get all unique components for the UI dropdown
        all_components = sorted(list(set(
            op['component'].strip() for op in operations_data if op.get('component')
        )))
        
        return jsonify({
            'matches': match_results,
            'all_components': all_components
        }), 200
        
    except Exception as e:
        logging.error(f"Error matching parts: {e}", exc_info=True)
        return jsonify({'error': f'Matching failed: {str(e)}'}), 500


@press_portal_bp.route('/api/scheduling/generate', methods=['POST'])
@login_required
def scheduling_generate():
    """
    Generate a 3-day shift-based production schedule.
    Takes matched parts, starting day, and generates machine assignments across 3 days.
    """
    data = request.get_json() or {}
    matched_parts = data.get('matched_parts', [])
    start_day = data.get('selected_day') or data.get('start_day')
    parts_data = data.get('parts_data', [])  # Original parts with daily_qty

    if not matched_parts:
        return jsonify({'error': 'No matched parts provided'}), 400
    if start_day is None:
        return jsonify({'error': 'Please select a starting day'}), 400

    start_day = int(start_day)

    bucket = get_gcs_bucket()
    if not bucket:
        return jsonify({'error': 'Could not connect to GCS'}), 500

    try:
        # Get operations and machines data
        operations_data = read_from_gcs(OPERATIONS_FILE_NAME, PRESSES_BUCKET_NAME) or []
        if isinstance(operations_data, dict):
            operations_data = list(operations_data.values())

        machines_data = read_from_gcs(MACHINES_FILE_NAME, PRESSES_BUCKET_NAME) or []
        if isinstance(machines_data, dict):
            machines_data = list(machines_data.values())

        if not operations_data:
            return jsonify({'error': 'No norms data available'}), 400
        if not machines_data:
            return jsonify({'error': 'No machines data available'}), 400

        # Build daily quantity map: {part_name: {day_str: qty}}
        daily_qty_map = {}
        has_any_qty = False
        for part in parts_data:
            part_name = part.get('part_name', '')
            daily_qty_map[part_name] = part.get('daily_qty', {})
            # Check if any qty exists in the 3-day window
            for offset in range(3):
                if daily_qty_map[part_name].get(str(start_day + offset), 0) > 0:
                    has_any_qty = True

        if not has_any_qty:
            return jsonify({'error': f'No production quantity found for days {start_day}-{start_day+2}'}), 400

        # Generate 3-day schedule
        schedule = generate_multi_day_schedule(
            matched_parts, daily_qty_map, start_day, operations_data, machines_data
        )
        schedule['selected_day'] = start_day

        return jsonify(schedule), 200

    except Exception as e:
        logging.error(f"Error generating schedule: {e}", exc_info=True)
        return jsonify({'error': f'Schedule generation failed: {str(e)}'}), 500


@press_portal_bp.route('/api/scheduling/save', methods=['POST'])
@login_required
def scheduling_save():
    """
    Save a finalized production schedule to GCS.
    """
    data = request.get_json() or {}
    schedule = data.get('schedule')
    schedule_name = data.get('schedule_name', '')
    selected_day = data.get('selected_day', '')
    
    if not schedule:
        return jsonify({'error': 'No schedule data provided'}), 400
    
    bucket = get_gcs_bucket()
    if not bucket:
        return jsonify({'error': 'Could not connect to GCS'}), 500
    
    try:
        timestamp = datetime.now(IST).strftime('%Y%m%d_%H%M%S')
        filename = f"{GENERATED_SCHEDULES_FOLDER}schedule_{schedule_name}_day{selected_day}_{timestamp}.json"
        
        save_data = {
            'schedule_name': schedule_name,
            'selected_day': selected_day,
            'generated_at': datetime.now(IST).isoformat(),
            'generated_by': session.get('user_email', 'Unknown'),
            'schedule': schedule
        }
        
        blob = bucket.blob(filename)
        blob.upload_from_string(
            json.dumps(save_data, indent=2, default=str),
            content_type='application/json'
        )
        
        return jsonify({
            'message': 'Schedule saved successfully',
            'filename': filename
        }), 200
        
    except Exception as e:
        logging.error(f"Error saving schedule: {e}", exc_info=True)
        return jsonify({'error': f'Failed to save schedule: {str(e)}'}), 500


@press_portal_bp.route('/api/scheduling/download', methods=['POST'])
@login_required
def scheduling_download():
    """
    Generate and download an Excel file of the 3-day schedule.
    One sheet per day + summary + unassigned.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    data = request.get_json() or {}
    schedule = data.get('schedule', {})
    schedule_name = data.get('schedule_name', 'Schedule')
    selected_day = data.get('selected_day', '')

    if not schedule:
        return jsonify({'error': 'No schedule data provided'}), 400

    try:
        wb = openpyxl.Workbook()

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
        fill_in_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
        border = Border(
            left=Side(style='thin'), right=Side(style='thin'),
            top=Side(style='thin'), bottom=Side(style='thin')
        )

        days_data = schedule.get('days', {})
        first_sheet = True

        for day_key in sorted(days_data.keys(), key=lambda x: int(x)):
            day_info = days_data[day_key]
            cal_day = day_info.get('calendar_day', day_key)

            if first_sheet:
                ws = wb.active
                ws.title = f"Day {cal_day}"
                first_sheet = False
            else:
                ws = wb.create_sheet(f"Day {cal_day}")

            # Title
            ws.merge_cells('A1:H1')
            ws['A1'].value = f"{schedule_name} — Day {cal_day}"
            ws['A1'].font = Font(bold=True, size=14, color="1F2937")
            ws['A1'].alignment = Alignment(horizontal='center')

            # Headers
            headers = ['Machine', 'Shift', 'Type', 'Component', 'Operation', 'Qty', 'Hours', 'Score']
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=3, column=col, value=h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal='center')
                cell.border = border

            row_idx = 4
            machines = day_info.get('machines', {})
            for m_name in sorted(machines.keys()):
                m_info = machines[m_name]
                shifts = m_info.get('shifts', {})
                for s_key in sorted(shifts.keys(), key=lambda x: int(x)):
                    shift = shifts[s_key]
                    shift_label = f"S{s_key}"
                    primary = shift.get('primary')

                    if primary:
                        values = [m_name, shift_label, 'Primary', primary.get('component', ''),
                                  primary.get('operation', ''), primary.get('qty', 0),
                                  primary.get('hours', 0),
                                  round(primary.get('recommendation_score', 0), 1)]
                        for col, val in enumerate(values, 1):
                            cell = ws.cell(row=row_idx, column=col, value=val)
                            cell.border = border
                            cell.alignment = Alignment(horizontal='center')
                        row_idx += 1

                    for fi in shift.get('fill_ins', []):
                        values = [m_name, shift_label, 'Fill-in', fi.get('component', ''),
                                  fi.get('operation', ''), fi.get('qty', 0),
                                  fi.get('hours', 0),
                                  round(fi.get('recommendation_score', 0), 1)]
                        for col, val in enumerate(values, 1):
                            cell = ws.cell(row=row_idx, column=col, value=val)
                            cell.border = border
                            cell.alignment = Alignment(horizontal='center')
                            cell.fill = fill_in_fill
                        row_idx += 1

            for col in range(1, 9):
                ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 18

        # --- Summary Sheet ---
        ws_sum = wb.create_sheet("Summary")
        summary = schedule.get('summary', {})
        ws_sum['A1'].value = "Schedule Summary"
        ws_sum['A1'].font = Font(bold=True, size=14)
        labels = [
            ('Total Operations', summary.get('total_operations', 0)),
            ('Fully Scheduled', summary.get('fully_scheduled', 0)),
            ('Partially Scheduled', summary.get('partially_scheduled', 0)),
            ('Unassignable', summary.get('unassignable', 0)),
            ('Total Machine Hours', summary.get('total_machine_hours', 0)),
            ('Total Used Hours', summary.get('total_used_hours', 0)),
            ('Avg Utilization %', summary.get('avg_utilization', 0)),
        ]
        for i, (label, val) in enumerate(labels, 3):
            ws_sum.cell(row=i, column=1, value=label).font = Font(bold=True)
            ws_sum.cell(row=i, column=2, value=val)
        ws_sum.column_dimensions['A'].width = 25
        ws_sum.column_dimensions['B'].width = 15

        # --- Unassigned Sheet ---
        unassigned = schedule.get('unassigned', [])
        if unassigned:
            ws_un = wb.create_sheet("Unassigned")
            ws_un['A1'].value = "Unassigned Operations"
            ws_un['A1'].font = Font(bold=True, size=14, color="991B1B")
            un_headers = ['Component', 'Operation', 'Qty', 'Hours Needed', 'Status', 'Reason']
            for col, h in enumerate(un_headers, 1):
                cell = ws_un.cell(row=3, column=col, value=h)
                cell.font = header_font
                cell.fill = PatternFill(start_color="EF4444", end_color="EF4444", fill_type="solid")
                cell.border = border
            for ri, u in enumerate(unassigned, 4):
                ws_un.cell(row=ri, column=1, value=u.get('component', '')).border = border
                ws_un.cell(row=ri, column=2, value=u.get('operation', '')).border = border
                ws_un.cell(row=ri, column=3, value=u.get('total_qty', 0)).border = border
                ws_un.cell(row=ri, column=4, value=u.get('remaining_hours', 0)).border = border
                ws_un.cell(row=ri, column=5, value=u.get('status', '')).border = border
                ws_un.cell(row=ri, column=6, value=u.get('reason', '')).border = border
            for col in range(1, 7):
                ws_un.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 22

        output = BytesIO()
        wb.save(output)
        output.seek(0)

        safe_name = schedule_name.replace(' ', '_')[:30]
        filename = f"Schedule_{safe_name}_Day{selected_day}_3day.xlsx"

        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        logging.error(f"Error generating Excel download: {e}", exc_info=True)
        return jsonify({'error': f'Download failed: {str(e)}'}), 500


@press_portal_bp.route('/api/scheduling/list', methods=['GET'])
@login_required
def scheduling_list():
    """
    List all saved schedules from GCS.
    """
    bucket = get_gcs_bucket()
    if not bucket:
        return jsonify({'error': 'Could not connect to GCS'}), 500
    
    try:
        blobs = bucket.list_blobs(prefix=GENERATED_SCHEDULES_FOLDER)
        schedules = []
        
        for blob in blobs:
            if not blob.name.endswith('.json'):
                continue
            
            # Extract info from filename
            name = blob.name.replace(GENERATED_SCHEDULES_FOLDER, '')
            schedules.append({
                'filename': name,
                'full_path': blob.name,
                'size_bytes': blob.size,
                'created': blob.time_created.isoformat() if blob.time_created else '',
                'updated': blob.updated.isoformat() if blob.updated else ''
            })
        
        # Sort by newest first
        schedules.sort(key=lambda s: s.get('created', ''), reverse=True)
        
        return jsonify({'schedules': schedules}), 200
        
    except Exception as e:
        logging.error(f"Error listing schedules: {e}", exc_info=True)
        return jsonify({'error': f'Failed to list schedules: {str(e)}'}), 500


@press_portal_bp.route('/api/scheduling/view', methods=['POST'])
@login_required
def scheduling_view():
    """
    View a previously saved schedule by its GCS path.
    """
    data = request.get_json() or {}
    full_path = data.get('full_path', '')
    
    if not full_path:
        return jsonify({'error': 'No file path provided'}), 400
    
    bucket = get_gcs_bucket()
    if not bucket:
        return jsonify({'error': 'Could not connect to GCS'}), 500
    
    try:
        blob = bucket.blob(full_path)
        if not blob.exists():
            return jsonify({'error': 'Schedule not found'}), 404
        
        content = json.loads(blob.download_as_text())
        return jsonify(content), 200
        
    except Exception as e:
        logging.error(f"Error viewing schedule: {e}", exc_info=True)
        return jsonify({'error': f'Failed to load schedule: {str(e)}'}), 500


# ==============================================================================
# OTA FLEET MANAGEMENT SERVICE
# ==============================================================================

OTA_GCS_FOLDER = "ota/"                          # firmware binaries
OTA_DEPLOYMENTS_FOLDER = "ota/deployments/"       # deployment state tracking
OTA_BUCKET_NAME = COMMANDS_BUCKET_NAME            # reuse press_data_storage

# In-memory deployment queue state
_ota_active_deployment = None  # Currently running deployment dict
_ota_lock = threading.Lock()

OTA_LOG_POLL_INTERVAL = 5       # seconds between log polls
OTA_LOG_TIMEOUT = 300            # 5 minutes - max wait for "OTA Success" log
OTA_STATUS_POLL_INTERVAL = 5    # seconds between status polls
OTA_STATUS_TIMEOUT = 120         # 2 minutes - max wait for version confirmation after reboot
OTA_MAX_BINARY_SIZE = 2 * 1024 * 1024  # 2MB max firmware size


def _save_deployment_state(deployment):
    """Persist deployment state to GCS."""
    try:
        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        if bucket:
            path = f"{OTA_DEPLOYMENTS_FOLDER}{deployment['deploy_id']}.json"
            blob = bucket.blob(path)
            blob.upload_from_string(json.dumps(deployment, indent=2), content_type='application/json')
    except Exception as e:
        logging.error(f"[OTA] Failed to save deployment state: {e}")


def _update_press_firmware_version(press_name, version):
    """Update the firmware_version field in presses.json for a specific press."""
    try:
        presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
        if presses is None:
            presses = []
        updated = False
        for p in presses:
            if p.get('name') == press_name:
                p['firmware_version'] = version
                p['firmware_updated_at'] = datetime.now(IST).isoformat()
                updated = True
                break
        if updated:
            write_to_gcs(PRESSES_FILE, presses, PRESSES_BUCKET_NAME)
            logging.info(f"[OTA] Updated firmware_version for {press_name} to {version}")
    except Exception as e:
        logging.error(f"[OTA] Failed to update firmware version for {press_name}: {e}")


def _check_machine_online(press_name):
    """Check if a machine is currently ONLINE via GCS status file."""
    try:
        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        if bucket:
            status = get_press_online_status(bucket, press_name)
            return status == 'ONLINE'
    except Exception:
        pass
    return False


def _poll_ota_success_log(press_name, command_sent_at):
    """
    Poll ESP32 logs from GCS for the exact 'OTA Success. Rebooting...' message.
    Returns True if found within timeout, False otherwise.
    """
    start_time = time.time()
    after_dt = datetime.fromisoformat(command_sent_at)

    while time.time() - start_time < OTA_LOG_TIMEOUT:
        try:
            bucket = get_gcs_bucket(OTA_BUCKET_NAME)
            if not bucket:
                time.sleep(OTA_LOG_POLL_INTERVAL)
                continue

            now = datetime.now(IST)
            # Search today (and yesterday if near midnight)
            for days_back in range(2):
                check_date = now - timedelta(days=days_back)
                prefix = f"{LOGS_DATA_PREFIX}press_name={press_name}/month={check_date.month:02d}/day={check_date.day:02d}/"
                blobs = list(bucket.list_blobs(prefix=prefix))
                if not blobs:
                    continue

                blobs.sort(key=lambda b: b.name, reverse=True)

                for blob in blobs[:20]:  # Check most recent 20 entries
                    try:
                        entry = json.loads(blob.download_as_text())
                        ts_str = entry.get('timestamp')
                        if not ts_str:
                            continue
                        ts = datetime.fromisoformat(ts_str)

                        # Only check logs after the command was sent
                        if ts <= after_dt:
                            continue

                        raw_payload = entry.get('payload_value', '{}')
                        try:
                            payload = json.loads(raw_payload)
                            msg = payload.get('msg', '')
                        except (json.JSONDecodeError, TypeError):
                            msg = str(raw_payload)

                        if 'OTA Success. Rebooting...' in msg:
                            logging.info(f"[OTA] OTA success log detected for {press_name}")
                            return True

                        # Also detect explicit failure
                        if 'OTA Failed' in msg or 'OTA Error' in msg:
                            logging.warning(f"[OTA] OTA failure log detected for {press_name}: {msg}")
                            return False

                    except Exception:
                        continue

        except Exception as e:
            logging.error(f"[OTA] Error polling logs for {press_name}: {e}")

        time.sleep(OTA_LOG_POLL_INTERVAL)

    logging.warning(f"[OTA] Timed out waiting for OTA success log from {press_name}")
    return False


def _poll_version_confirmation(press_name, expected_version):
    """
    Poll GCS status file for the confirmed version after reboot.
    Looks for: {"status":"ONLINE", "boot_cause":"SOFTWARE_RESET", "version":"vX.X"}
    Returns the confirmed version string or None on timeout.
    """
    start_time = time.time()

    while time.time() - start_time < OTA_STATUS_TIMEOUT:
        try:
            bucket = get_gcs_bucket(OTA_BUCKET_NAME)
            if not bucket:
                time.sleep(OTA_STATUS_POLL_INTERVAL)
                continue

            status_path = f"{STATUS_DATA_PREFIX}press_name={press_name}/latest.json"
            blob = bucket.blob(status_path)
            if blob.exists():
                # Force fresh read
                blob.reload()
                data = json.loads(blob.download_as_text())

                # Handle both raw payload_value string and structured JSON
                payload = data
                if 'payload_value' in data:
                    raw = data['payload_value']
                    if isinstance(raw, str):
                        try:
                            payload = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            payload = data

                status_val = payload.get('status', '').upper()
                version_val = payload.get('version', '')

                if status_val == 'ONLINE' and version_val == expected_version:
                    logging.info(f"[OTA] Version confirmed for {press_name}: {version_val}")
                    return version_val

        except Exception as e:
            logging.error(f"[OTA] Error polling status for {press_name}: {e}")

        time.sleep(OTA_STATUS_POLL_INTERVAL)

    logging.warning(f"[OTA] Timed out waiting for version confirmation from {press_name}")
    return None


def _ota_queue_worker(deployment):
    """
    Background worker that processes the OTA deployment queue sequentially.
    Sends MQTT command to one machine at a time, waits for confirmation, then proceeds.
    """
    global _ota_active_deployment

    logging.info(f"[OTA] Starting deployment {deployment['deploy_id']} for version {deployment['version']}")

    for i, machine in enumerate(deployment['queue']):
        # Check if deployment was cancelled
        with _ota_lock:
            if _ota_active_deployment and _ota_active_deployment.get('status') == 'cancelled':
                machine['status'] = 'skipped'
                machine['error'] = 'Deployment cancelled'
                # Skip remaining machines
                for j in range(i + 1, len(deployment['queue'])):
                    deployment['queue'][j]['status'] = 'skipped'
                    deployment['queue'][j]['error'] = 'Deployment cancelled'
                break

        press_name = machine['press_name']

        # Check if machine is online before attempting OTA
        if not _check_machine_online(press_name):
            machine['status'] = 'skipped'
            machine['error'] = 'Machine is OFFLINE'
            logging.warning(f"[OTA] Skipping {press_name} — machine is OFFLINE")
            _save_deployment_state(deployment)
            continue

        # Step 1: Mark machine as updating
        machine['status'] = 'updating'
        machine['command_sent_at'] = datetime.now(IST).isoformat()
        _save_deployment_state(deployment)

        # Step 2: Publish MQTT command
        client = get_mqtt_client()
        if not client:
            machine['status'] = 'failed'
            machine['error'] = 'MQTT client not available'
            _save_deployment_state(deployment)
            continue

        topic = f"factory/{press_name}/command"
        payload = {
            "cmd": "UPDATE",
            "version": deployment['version'],
            "url": deployment['firmware_url'],
            "force": deployment.get('force', False)
        }

        try:
            result = client.publish(topic, json.dumps(payload), qos=0)
            logging.info(f"[OTA] Sent UPDATE command to {press_name}: {payload}")
        except Exception as e:
            machine['status'] = 'failed'
            machine['error'] = f'MQTT publish failed: {str(e)}'
            _save_deployment_state(deployment)
            continue

        # Step 3: Poll for OTA success log
        ota_success = _poll_ota_success_log(press_name, machine['command_sent_at'])

        if not ota_success:
            machine['status'] = 'failed'
            machine['error'] = 'No OTA success confirmation within timeout'
            _save_deployment_state(deployment)
            continue

        # Step 4: Poll for version confirmation after reboot
        confirmed_version = _poll_version_confirmation(press_name, deployment['version'])

        if confirmed_version:
            machine['status'] = 'success'
            machine['confirmed_at'] = datetime.now(IST).isoformat()
            machine['confirmed_version'] = confirmed_version
            # Update press firmware version in the master list
            _update_press_firmware_version(press_name, confirmed_version)
        else:
            machine['status'] = 'failed'
            machine['error'] = 'OTA Success logged but version confirmation timed out after reboot'

        _save_deployment_state(deployment)

    # Mark deployment as completed
    with _ota_lock:
        if deployment.get('status') != 'cancelled':
            # Determine overall status
            statuses = [m['status'] for m in deployment['queue']]
            if all(s == 'success' for s in statuses):
                deployment['status'] = 'completed'
            elif all(s in ('failed', 'skipped') for s in statuses):
                deployment['status'] = 'failed'
            else:
                deployment['status'] = 'completed'  # partial success

        deployment['completed_at'] = datetime.now(IST).isoformat()
        _save_deployment_state(deployment)
        _ota_active_deployment = None

    logging.info(f"[OTA] Deployment {deployment['deploy_id']} finished. Status: {deployment['status']}")


# ==================== OTA ROUTES ====================


@press_portal_bp.route('/admin/ota')
@service_access_required('press_portal')
@login_required
@admin_required
def ota_dashboard():
    """OTA Fleet Management Dashboard."""
    return render_template('press_ota_dashboard.html', mqtt_status=mqtt_connected)


@press_portal_bp.route('/api/ota/upload', methods=['POST'])
@login_required
@admin_required
def ota_upload_binary():
    """
    Upload a firmware .bin file to GCS and return the public download URL.
    Expects multipart form: file (binary), version (string).
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    version = request.form.get('version', '').strip()

    if not version:
        return jsonify({"error": "Version is required"}), 400

    if not file.filename.endswith('.bin'):
        return jsonify({"error": "Only .bin files are accepted"}), 400

    # Read file content
    file_content = file.read()

    if len(file_content) > OTA_MAX_BINARY_SIZE:
        return jsonify({"error": f"File too large. Maximum size is {OTA_MAX_BINARY_SIZE // (1024*1024)}MB"}), 400

    if len(file_content) == 0:
        return jsonify({"error": "File is empty"}), 400

    try:
        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        # Store as: ota/firmware_v3.3.bin
        safe_version = version.replace(' ', '_')
        blob_path = f"{OTA_GCS_FOLDER}firmware_{safe_version}.bin"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(file_content, content_type='application/octet-stream')

        # Construct public URL
        firmware_url = f"https://storage.googleapis.com/{bucket.name}/{blob_path}"

        logging.info(f"[OTA] Firmware uploaded: {blob_path} ({len(file_content)} bytes)")

        return jsonify({
            "message": "Firmware uploaded successfully",
            "firmware_url": firmware_url,
            "version": version,
            "size_bytes": len(file_content),
            "gcs_path": blob_path
        }), 200

    except Exception as e:
        logging.error(f"[OTA] Upload failed: {e}")
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500


@press_portal_bp.route('/api/ota/fleet-status', methods=['GET'])
@login_required
def ota_fleet_status():
    """
    Return all presses with their current firmware version and online status.
    Used to populate the fleet matrix on the OTA dashboard.
    """
    try:
        presses = read_from_gcs(PRESSES_FILE, PRESSES_BUCKET_NAME)
        if presses is None:
            presses = []

        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        fleet = []
        for p in presses:
            name = p.get('name', '')
            status = 'UNKNOWN'
            if bucket:
                status = get_press_online_status(bucket, name)

            fleet.append({
                "id": p.get('id'),
                "name": name,
                "location": p.get('location', ''),
                "firmware_version": p.get('firmware_version', 'Unknown'),
                "firmware_updated_at": p.get('firmware_updated_at', ''),
                "status": status
            })

        return jsonify({"fleet": fleet}), 200

    except Exception as e:
        logging.error(f"[OTA] Fleet status error: {e}")
        return jsonify({"error": str(e)}), 500


@press_portal_bp.route('/api/ota/deploy', methods=['POST'])
@login_required
@admin_required
def ota_start_deployment():
    """
    Start a new sequential OTA deployment.
    
    Expects JSON:
    {
        "version": "v3.3",
        "firmware_url": "https://storage.googleapis.com/.../firmware_v3.3.bin",
        "target_machines": ["press_machine_NP1", "press_machine_NP2"],
        "force": false
    }
    """
    global _ota_active_deployment

    with _ota_lock:
        if _ota_active_deployment and _ota_active_deployment.get('status') == 'in_progress':
            return jsonify({
                "error": "A deployment is already in progress",
                "active_deploy_id": _ota_active_deployment['deploy_id']
            }), 409

    data = request.get_json()
    version = data.get('version', '').strip()
    firmware_url = data.get('firmware_url', '').strip()
    target_machines = data.get('target_machines', [])
    force = data.get('force', False)

    if not version:
        return jsonify({"error": "Version is required"}), 400
    if not firmware_url:
        return jsonify({"error": "Firmware URL is required"}), 400
    if not target_machines or not isinstance(target_machines, list):
        return jsonify({"error": "At least one target machine is required"}), 400

    # Build deployment state
    deploy_id = str(uuid.uuid4())[:12]
    deployment = {
        "deploy_id": deploy_id,
        "version": version,
        "firmware_url": firmware_url,
        "force": force,
        "status": "in_progress",
        "created_at": datetime.now(IST).isoformat(),
        "created_by": session.get('email', 'unknown'),
        "completed_at": None,
        "queue": []
    }

    for machine_name in target_machines:
        deployment['queue'].append({
            "press_name": machine_name,
            "status": "pending",
            "command_sent_at": None,
            "confirmed_at": None,
            "confirmed_version": None,
            "error": None
        })

    with _ota_lock:
        _ota_active_deployment = deployment

    # Save initial state
    _save_deployment_state(deployment)

    # Start background worker thread
    worker = threading.Thread(target=_ota_queue_worker, args=(deployment,), daemon=True)
    worker.start()

    logging.info(f"[OTA] Deployment {deploy_id} started for {len(target_machines)} machines")

    return jsonify({
        "message": "Deployment started",
        "deploy_id": deploy_id,
        "machines_queued": len(target_machines)
    }), 202


@press_portal_bp.route('/api/ota/deploy/<deploy_id>/status', methods=['GET'])
@login_required
def ota_deployment_status(deploy_id):
    """
    Get current status of a deployment.
    Frontend polls this every 3 seconds.
    """
    global _ota_active_deployment

    # Check in-memory first (for active deployment)
    with _ota_lock:
        if _ota_active_deployment and _ota_active_deployment.get('deploy_id') == deploy_id:
            return jsonify(_ota_active_deployment), 200

    # Fall back to GCS (for completed/historical deployments)
    try:
        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        if bucket:
            path = f"{OTA_DEPLOYMENTS_FOLDER}{deploy_id}.json"
            blob = bucket.blob(path)
            if blob.exists():
                data = json.loads(blob.download_as_text())
                return jsonify(data), 200
    except Exception as e:
        logging.error(f"[OTA] Error reading deployment status: {e}")

    return jsonify({"error": "Deployment not found"}), 404


@press_portal_bp.route('/api/ota/deploy/<deploy_id>/cancel', methods=['POST'])
@login_required
@admin_required
def ota_cancel_deployment(deploy_id):
    """
    Cancel a running deployment.
    The current machine finishes, remaining are skipped.
    """
    global _ota_active_deployment

    with _ota_lock:
        if _ota_active_deployment and _ota_active_deployment.get('deploy_id') == deploy_id:
            if _ota_active_deployment.get('status') == 'in_progress':
                _ota_active_deployment['status'] = 'cancelled'
                logging.info(f"[OTA] Deployment {deploy_id} cancelled by user")
                return jsonify({"message": "Deployment cancellation requested"}), 200
            else:
                return jsonify({"error": "Deployment is not in progress"}), 400

    return jsonify({"error": "Deployment not found or already finished"}), 404


@press_portal_bp.route('/api/ota/machine-logs/<press_name>', methods=['GET'])
@login_required
def ota_machine_logs(press_name):
    """
    Get recent ESP32 log entries for live console display.
    Query param: after (ISO timestamp) — only return logs after this time.
    """
    after_str = request.args.get('after')
    limit = min(int(request.args.get('limit', 30)), 100)

    after_dt = None
    if after_str:
        try:
            after_dt = datetime.fromisoformat(after_str)
        except ValueError:
            return jsonify({"error": "Invalid 'after' timestamp"}), 400

    try:
        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        now = datetime.now(IST)
        logs = []

        for days_back in range(2):
            check_date = now - timedelta(days=days_back)
            prefix = f"{LOGS_DATA_PREFIX}press_name={press_name}/month={check_date.month:02d}/day={check_date.day:02d}/"
            blobs = list(bucket.list_blobs(prefix=prefix))
            if not blobs:
                continue

            blobs.sort(key=lambda b: b.name, reverse=True)

            for blob in blobs:
                try:
                    entry = json.loads(blob.download_as_text())
                    ts_str = entry.get('timestamp')
                    ts = datetime.fromisoformat(ts_str) if ts_str else None

                    if after_dt and ts and ts <= after_dt:
                        continue

                    raw_payload = entry.get('payload_value', '{}')
                    try:
                        payload = json.loads(raw_payload)
                        level = payload.get('lvl', 'INFO')
                        message = payload.get('msg', raw_payload)
                    except (json.JSONDecodeError, TypeError):
                        level = 'INFO'
                        message = str(raw_payload)

                    logs.append({
                        'timestamp': ts_str,
                        'level': level,
                        'message': message
                    })

                    if len(logs) >= limit:
                        break
                except Exception:
                    continue

            if len(logs) >= limit:
                break

        return jsonify({
            "press_name": press_name,
            "logs": logs,
            "count": len(logs)
        }), 200

    except Exception as e:
        logging.error(f"[OTA] Error fetching logs for {press_name}: {e}")
        return jsonify({"error": str(e)}), 500


@press_portal_bp.route('/api/ota/history', methods=['GET'])
@login_required
def ota_deployment_history():
    """
    Get past deployment records from GCS.
    Returns the most recent 20 deployments.
    """
    try:
        bucket = get_gcs_bucket(OTA_BUCKET_NAME)
        if not bucket:
            return jsonify({"error": "GCS bucket not available"}), 500

        blobs = list(bucket.list_blobs(prefix=OTA_DEPLOYMENTS_FOLDER))
        deployments = []

        for blob in blobs:
            if not blob.name.endswith('.json'):
                continue
            try:
                data = json.loads(blob.download_as_text())
                # Return summary only (not full queue details)
                total = len(data.get('queue', []))
                success = sum(1 for m in data.get('queue', []) if m.get('status') == 'success')
                failed = sum(1 for m in data.get('queue', []) if m.get('status') == 'failed')
                skipped = sum(1 for m in data.get('queue', []) if m.get('status') == 'skipped')

                deployments.append({
                    "deploy_id": data.get('deploy_id'),
                    "version": data.get('version'),
                    "status": data.get('status'),
                    "created_at": data.get('created_at'),
                    "created_by": data.get('created_by'),
                    "completed_at": data.get('completed_at'),
                    "total_machines": total,
                    "success": success,
                    "failed": failed,
                    "skipped": skipped
                })
            except Exception:
                continue

        # Sort by created_at descending
        deployments.sort(key=lambda d: d.get('created_at', ''), reverse=True)

        return jsonify({"deployments": deployments[:20]}), 200

    except Exception as e:
        logging.error(f"[OTA] History error: {e}")
        return jsonify({"error": str(e)}), 500 press portal.py 