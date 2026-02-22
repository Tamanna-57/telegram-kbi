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
        if view == "report":
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
