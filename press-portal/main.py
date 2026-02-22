"""
Cloud Function: Press Analytics & Monitoring Portal
Entry point: press_portal

Triggered by: HTTP GET (browser visit)

What this function does:
    1. Serves a web dashboard for press shop analytics and monitoring
    2. Reads production and maintenance master records from GCS
    3. Reads HR attendance records from GCS
    4. Renders two HTML views:
       - Dashboard (default): tile-based overview with summary statistics
       - Report: date-filtered detailed analytics table (production + maintenance + HR)

Routes (via ?view= query parameter):
    /                      → press_monitoring_dashboard.html  (summary tiles + recent dates)
    /?view=report          → press_report_processed.html      (full table, latest available date)
    /?view=report&date=YYYY-MM-DD  → same, filtered to a specific date

GCP Deployment:
    Trigger=HTTP, Runtime=Python 3.12, Entry point=press_portal
    Environment variables: GCS_BUCKET_NAME, DEBUG_MODE
"""

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import functions_framework
from google.cloud import storage
from jinja2 import Environment, FileSystemLoader

from config import (
    GCS_BUCKET_NAME,
    HR_ATTENDANCE_DIR,
    PRESS_MASTER_MAINTENANCE,
    PRESS_MASTER_PRODUCTION,
    PRESS_DATA_STORAGE_BUCKET,
    PRESS_DAILY_REPORT_PREFIX,
    PRESS_PROCESSED_PREFIX,
    DEBUG_MODE,
)

# ============================================================
# TEMPLATE SETUP
# ============================================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_jinja_env = Environment(
    loader=FileSystemLoader(os.path.join(_BASE_DIR, "templates", "press_portal")),
    autoescape=True,
)


# ============================================================
# GCS HELPERS
# ============================================================

def _gcs_client() -> storage.Client:
    return storage.Client()


def _read_json(bucket_name: str, blob_path: str) -> Optional[Any]:
    """Read and parse a JSON file from GCS. Returns None on any error."""
    try:
        client = _gcs_client()
        blob = client.bucket(bucket_name).blob(blob_path)
        if not blob.exists():
            if DEBUG_MODE:
                print(f"[DEBUG] Not found: gs://{bucket_name}/{blob_path}")
            return None
        return json.loads(blob.download_as_text())
    except Exception as exc:
        print(f"⚠️ Error reading gs://{bucket_name}/{blob_path}: {exc}")
        return None


# ============================================================
# DATA LOADING
# ============================================================

def _load_press_records(bucket: str) -> Dict[str, List[Dict]]:
    """Load both production and maintenance master record lists from GCS."""
    production = _read_json(bucket, PRESS_MASTER_PRODUCTION) or []
    maintenance = _read_json(bucket, PRESS_MASTER_MAINTENANCE) or []

    if not isinstance(production, list):
        print(f"⚠️ {PRESS_MASTER_PRODUCTION} is not a JSON array — treating as empty.")
        production = []
    if not isinstance(maintenance, list):
        print(f"⚠️ {PRESS_MASTER_MAINTENANCE} is not a JSON array — treating as empty.")
        maintenance = []

    return {"production": production, "maintenance": maintenance}


def _load_hr_attendance(bucket: str, date_str: str) -> List[Dict]:
    """Load HR attendance records for a given date (Press Shop department only)."""
    path = f"{HR_ATTENDANCE_DIR}/{date_str}.json"
    raw = _read_json(bucket, path)

    if not isinstance(raw, dict):
        return []

    return [
        {"employee_code": k, **v}
        for k, v in raw.items()
        if isinstance(v, dict)
        and str(v.get("department", "")).upper().strip() == "PRESS SHOP"
    ]


def _filter_by_date(records: List[Dict], date_str: str) -> List[Dict]:
    """Return records where plan_date == date_str."""
    return [r for r in records if r.get("plan_date") == date_str]


def _get_sorted_dates(records: List[Dict]) -> List[str]:
    """Return unique plan_dates from records, newest first."""
    return sorted(
        {r.get("plan_date", "") for r in records if r.get("plan_date")},
        reverse=True,
    )


def _extract_operators(records: List[Dict]) -> set:
    """Extract all unique normalized operator names from a list of records."""
    operators = set()
    for r in records:
        raw = r.get("operator_names", "")
        if isinstance(raw, str):
            for name in raw.split(","):
                name = name.strip().upper()
                if name:
                    operators.add(name)
        elif isinstance(raw, list):
            for name in raw:
                if isinstance(name, str) and name.strip():
                    operators.add(name.strip().upper())
    return operators


def _compute_dashboard_stats(records: Dict[str, List[Dict]]) -> Dict:
    """Compute summary statistics for the dashboard tiles."""
    prod = records["production"]
    maint = records["maintenance"]
    all_records = prod + maint

    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    recent_prod = [r for r in prod if week_ago <= r.get("plan_date", "") <= today]
    recent_maint = [r for r in maint if week_ago <= r.get("plan_date", "") <= today]

    all_operators = _extract_operators(all_records)
    all_dates = [r.get("plan_date", "") for r in all_records if r.get("plan_date")]
    latest_date = max(all_dates) if all_dates else None

    return {
        "total_production_entries": len(prod),
        "total_maintenance_entries": len(maint),
        "recent_production_7d": len(recent_prod),
        "recent_maintenance_7d": len(recent_maint),
        "unique_operators": len(all_operators),
        "latest_date": latest_date or "—",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M IST"),
    }


def _infer_columns(records: List[Dict], priority: Optional[List[str]] = None) -> List[str]:
    """
    Infer column headers from a list of records (union of all keys).
    Priority keys are shown first if present in any record.
    """
    priority = priority or [
        "plan_date", "operator_names", "job_no", "part_name",
        "machine_no", "qty", "shift", "status",
    ]
    cols: List[str] = []
    seen: set = set()

    for key in priority:
        if any(key in r for r in records) and key not in seen:
            cols.append(key)
            seen.add(key)

    for r in records:
        for key in r:
            if key not in seen:
                cols.append(key)
                seen.add(key)

    return cols


def _friendly_label(key: str) -> str:
    """Convert a snake_case key to a Title Case display label."""
    return key.replace("_", " ").title()


def _fmt_num(val: Any, decimals: int = 1, default: str = "—") -> str:
    """Format a numeric value for display; returns default for None."""
    if val is None:
        return default
    try:
        return f"{float(val):.{decimals}f}"
    except (TypeError, ValueError):
        return default


# ============================================================
# MACHINE MONITORING — GCS HELPERS
# ============================================================

def _list_press_machines(bucket_name: str) -> List[str]:
    """Return sorted list of press machine names from daily_press_report_operation/ prefix."""
    try:
        client = _gcs_client()
        iterator = client.list_blobs(
            bucket_name,
            prefix=f"{PRESS_DAILY_REPORT_PREFIX}/",
            delimiter="/",
        )
        machines: List[str] = []
        for page in iterator.pages:
            for prefix in page.prefixes:
                # prefix looks like: "daily_press_report_operation/press_machine_NP01/"
                machine = prefix.rstrip("/").split("/")[-1]
                if machine:
                    machines.append(machine)
        return sorted(machines)
    except Exception as exc:
        print(f"⚠️ Error listing press machines from {bucket_name}: {exc}")
        return []


def _list_machine_dates(bucket_name: str, machine: str) -> List[str]:
    """Return sorted list of available report dates (newest first) for a press machine."""
    try:
        client = _gcs_client()
        prefix = f"{PRESS_DAILY_REPORT_PREFIX}/{machine}/"
        blobs = client.list_blobs(bucket_name, prefix=prefix)
        dates: List[str] = []
        for blob in blobs:
            # blob.name: daily_press_report_operation/{machine}/{YYYY}/{MM}/{DD}.json
            parts = blob.name.split("/")
            if len(parts) == 5 and parts[4].endswith(".json"):
                year, month, day = parts[2], parts[3], parts[4][:-5]
                dates.append(f"{year}-{month}-{day}")
        return sorted(set(dates), reverse=True)
    except Exception as exc:
        print(f"⚠️ Error listing dates for {machine}: {exc}")
        return []


def _load_daily_press_report(bucket_name: str, machine: str, date_str: str) -> Optional[Dict]:
    """Load the 24-hr daily press report JSON for a machine and date."""
    year, month, day = date_str.split("-")
    path = f"{PRESS_DAILY_REPORT_PREFIX}/{machine}/{year}/{month}/{day}.json"
    return _read_json(bucket_name, path)


def _list_processed_files(bucket_name: str, machine: str, date_str: str) -> List[str]:
    """Return sorted list of 30-min processed file names (without .json) for a machine/date."""
    try:
        year, month, day = date_str.split("-")
        prefix = f"{PRESS_PROCESSED_PREFIX}/{machine}/{year}/{month}/{day}/"
        client = _gcs_client()
        blobs = client.list_blobs(bucket_name, prefix=prefix)
        files: List[str] = []
        for blob in blobs:
            name = blob.name.split("/")[-1]
            if name.endswith(".json"):
                files.append(name[:-5])
        return sorted(files)
    except Exception as exc:
        print(f"⚠️ Error listing processed files for {machine}/{date_str}: {exc}")
        return []


def _load_processed_file(
    bucket_name: str, machine: str, date_str: str, filename: str
) -> Optional[Any]:
    """Load a single 30-min processed JSON file."""
    year, month, day = date_str.split("-")
    path = f"{PRESS_PROCESSED_PREFIX}/{machine}/{year}/{month}/{day}/{filename}.json"
    return _read_json(bucket_name, path)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

@functions_framework.http
def press_portal(request):
    """
    HTTP Cloud Function — Press Analytics & Monitoring Portal.
    Routes to dashboard or report view based on ?view= query param.
    """
    view = request.args.get("view", "dashboard")
    bucket = GCS_BUCKET_NAME

    try:
        if view == "machine_analytics":
            return _render_machine_analytics(request)
        elif view == "report":
            return _render_report(request, bucket)
        else:
            return _render_dashboard(bucket)
    except Exception as exc:
        print(f"❌ Unhandled error in press_portal [{view}]: {exc}")
        return (
            f"<h2>Error</h2><pre>{exc}</pre>",
            500,
            {"Content-Type": "text/html; charset=utf-8"},
        )


# ============================================================
# VIEW RENDERERS
# ============================================================

def _render_dashboard(bucket: str):
    """Render the monitoring dashboard (tile overview + recent dates)."""
    records = _load_press_records(bucket)
    stats = _compute_dashboard_stats(records)
    all_dates = _get_sorted_dates(records["production"] + records["maintenance"])

    template = _jinja_env.get_template("press_monitoring_dashboard.html")
    html = template.render(
        stats=stats,
        recent_dates=all_dates[:15],
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def _render_report(request, bucket: str):
    """Render the detailed analytics report for a selected date."""
    records = _load_press_records(bucket)
    all_dates = _get_sorted_dates(records["production"] + records["maintenance"])

    selected_date = request.args.get("date") or (all_dates[0] if all_dates else "")

    prod_for_date = _filter_by_date(records["production"], selected_date)
    maint_for_date = _filter_by_date(records["maintenance"], selected_date)
    hr_for_date = _load_hr_attendance(bucket, selected_date) if selected_date else []

    prod_cols = _infer_columns(prod_for_date)
    maint_cols = _infer_columns(maint_for_date, priority=[
        "plan_date", "tool_name", "operator_names", "machine_no",
        "maintenance_type", "status", "remarks",
    ])
    hr_cols = _infer_columns(hr_for_date, priority=[
        "employee_code", "worker_name", "department", "status",
    ])

    template = _jinja_env.get_template("press_report_processed.html")
    html = template.render(
        selected_date=selected_date,
        all_dates=all_dates[:90],
        production_records=prod_for_date,
        maintenance_records=maint_for_date,
        hr_records=hr_for_date,
        prod_cols=prod_cols,
        maint_cols=maint_cols,
        hr_cols=hr_cols,
        friendly_label=_friendly_label,
        as_of=datetime.now().strftime("%Y-%m-%d %H:%M IST"),
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def _render_machine_analytics(request):
    """Render the press machine analytics view (sensor data: strokes, vibration, temperature)."""
    monitoring_bucket = PRESS_DATA_STORAGE_BUCKET

    machines = _list_press_machines(monitoring_bucket)
    selected_machine = request.args.get("machine") or (machines[0] if machines else "")

    machine_dates: List[str] = []
    if selected_machine:
        machine_dates = _list_machine_dates(monitoring_bucket, selected_machine)

    selected_date = request.args.get("date") or (machine_dates[0] if machine_dates else "")
    selected_file = request.args.get("file", "")

    daily_report: Optional[Dict] = None
    processed_files: List[str] = []
    selected_processed: Optional[Any] = None

    if selected_machine and selected_date:
        daily_report = _load_daily_press_report(monitoring_bucket, selected_machine, selected_date)
        processed_files = _list_processed_files(monitoring_bucket, selected_machine, selected_date)
        if selected_file:
            selected_processed = _load_processed_file(
                monitoring_bucket, selected_machine, selected_date, selected_file
            )

    template = _jinja_env.get_template("press_machine_analytics.html")
    html = template.render(
        machines=machines,
        selected_machine=selected_machine,
        machine_dates=machine_dates[:90],
        selected_date=selected_date,
        daily_report=daily_report,
        processed_files=processed_files,
        selected_file=selected_file,
        selected_processed=selected_processed,
        fmt=_fmt_num,
        as_of=datetime.now().strftime("%Y-%m-%d %H:%M IST"),
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}
