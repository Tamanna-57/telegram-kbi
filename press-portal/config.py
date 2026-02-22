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
# DEBUG
# ============================================================

DEBUG_MODE: bool = os.environ.get("DEBUG_MODE", "false").lower() == "true"
