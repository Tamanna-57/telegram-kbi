"""
config.py — Configuration for the Press Analytics & Monitoring Portal.

HOW TO USE:
    Imported directly by main.py. Before deploying, deploy.sh copies this file
    from shared/ (or this folder is self-contained as-is).

ENVIRONMENT VARIABLES (set in Google Cloud Console → Cloud Functions → Edit → Variables):
    GCS_BUCKET_NAME   — GCS bucket name. Default: "kbi-first"
    DEBUG_MODE        — "true" to enable verbose logging. Default: "false"
"""

import os

# ============================================================
# GCS BUCKET & FILE PATHS
# ============================================================

GCS_BUCKET_NAME: str = os.environ.get("GCS_BUCKET_NAME", "kbi-first")

# Press shop master file paths
PRESS_MASTER_PRODUCTION: str = (
    "press_entry_report_shop/production/master_press_entry_report.json"
)
PRESS_MASTER_MAINTENANCE: str = (
    "press_entry_report_shop/maintenance/master_maintenance_tool_trial.json"
)

# HR attendance folder prefix (e.g. hr_attendance_v1/2026-02-04.json)
HR_ATTENDANCE_DIR: str = "hr_attendance_v1"

# Registered users file (for name lookups)
USERS_FILE: str = os.environ.get("USERS_FILE", "users3.json")

# ============================================================
# PRESS MACHINE MONITORING (separate GCS bucket)
# ============================================================

# Bucket that holds machine sensor / processed data
PRESS_DATA_STORAGE_BUCKET: str = os.environ.get(
    "PRESS_DATA_STORAGE_BUCKET", "press_data_storage"
)

# Path prefixes inside PRESS_DATA_STORAGE_BUCKET
# daily_press_report_operation/{machine}/{YYYY}/{MM}/{DD}.json  — 24-hr daily summary
PRESS_DAILY_REPORT_PREFIX: str = "daily_press_report_operation"

# press_processed/{machine}/{YYYY}/{MM}/{DD}/{HH-MM-SS}.json   — 30-min processed windows
PRESS_PROCESSED_PREFIX: str = "press_processed"

# raw_data/{machine}/{YYYY}/{MM}/{DD}/{HH-MM-SS}.json          — 5-min raw snapshots
PRESS_RAW_DATA_PREFIX: str = "raw_data"

# ============================================================
# DEBUG
# ============================================================

DEBUG_MODE: bool = os.environ.get("DEBUG_MODE", "false").lower() == "true"
