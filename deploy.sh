#!/bin/bash
# =============================================================================
# deploy.sh — Deployment helper for KBI Telegram Cloud Functions
#
# WHAT THIS SCRIPT DOES:
#   1. Copies shared modules (config.py, telegram_service.py, alert_dispatcher.py)
#      into each Cloud Function folder (required because Cloud Functions cannot
#      share files across deployment packages).
#   2. Deploys each function to Google Cloud Functions using gcloud CLI.
#
# HOW TO RUN:
#   chmod +x deploy.sh
#   ./deploy.sh                     # Deploy all 3 functions
#   ./deploy.sh gantt               # Deploy only gantt-alerts
#   ./deploy.sh webhook             # Deploy only telegram-webhook
#   ./deploy.sh attendance          # Deploy only attendance-checker
#   ./deploy.sh copy                # Only copy shared files, don't deploy
#
# PREREQUISITES:
#   1. Install gcloud CLI: https://cloud.google.com/sdk/docs/install
#   2. Authenticate:   gcloud auth login
#   3. Set project:    gcloud config set project YOUR_PROJECT_ID
#   4. Set region:     gcloud config set functions/region asia-south2
#
# ENVIRONMENT VARIABLES TO SET IN CLOUD CONSOLE AFTER DEPLOY:
#   TELEGRAM_BOT_TOKEN       → Your bot token from @BotFather
#   TELEGRAM_GROUP_CHAT_ID   → Group chat ID (negative number, e.g. -1001234567890)
#   ADMIN_CHAT_ID            → Admin's personal chat ID
#   SEND_INDIVIDUAL_ALERTS   → "true" or "false" (default: "false")
#   GCS_BUCKET_NAME          → Your GCS bucket name (default: "kbi-first")
#   DEBUG_MODE               → "true" or "false" (default: "false")
# =============================================================================

set -e  # Exit immediately on any error

# ---- CONFIGURATION ----
PROJECT_ID="arched-elixir-464218-j8"   # Your GCP project ID
REGION="asia-south2"                    # Cloud Functions region
RUNTIME="python312"                     # Python version (3.12 recommended)
SHARED_DIR="./shared"                   # Location of shared modules

# Function folder names and their Cloud Function names (entry points)
GANTT_DIR="./gantt-alerts"
GANTT_FUNCTION_NAME="telegram1"         # Must match your existing Cloud Function name
GANTT_ENTRY_POINT="analyze_gantt_charts_telegram"

WEBHOOK_DIR="./telegram-webhook"
WEBHOOK_FUNCTION_NAME="telegramwebhook" # Must match your existing Cloud Function name
WEBHOOK_ENTRY_POINT="telegram_bot"

ATTENDANCE_DIR="./attendance-checker"
ATTENDANCE_FUNCTION_NAME="check-attendance-discrepancies"
ATTENDANCE_ENTRY_POINT="check_attendance_discrepancies"

# ---- COLORS FOR OUTPUT ----
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'  # No Color

echo_info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
echo_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
echo_error()   { echo -e "${RED}[ERROR]${NC} $1"; }


# =============================================================================
# STEP 1: COPY SHARED MODULES INTO EACH FUNCTION FOLDER
# =============================================================================
copy_shared_modules() {
    echo_info "Copying shared modules into function folders..."

    SHARED_FILES=("config.py" "telegram_service.py" "alert_dispatcher.py")

    for FUNCTION_DIR in "$GANTT_DIR" "$WEBHOOK_DIR" "$ATTENDANCE_DIR"; do
        if [ ! -d "$FUNCTION_DIR" ]; then
            echo_warning "Directory $FUNCTION_DIR not found. Skipping."
            continue
        fi

        for FILE in "${SHARED_FILES[@]}"; do
            SOURCE="$SHARED_DIR/$FILE"
            DEST="$FUNCTION_DIR/$FILE"

            if [ ! -f "$SOURCE" ]; then
                echo_error "Shared file $SOURCE not found! Run from the project root."
                exit 1
            fi

            cp "$SOURCE" "$DEST"
            echo_info "  Copied $FILE → $FUNCTION_DIR/"
        done
    done

    echo_info "✅ Shared modules copied successfully."
}


# =============================================================================
# STEP 2: DEPLOY FUNCTIONS
# =============================================================================
deploy_gantt() {
    echo_info "Deploying: $GANTT_FUNCTION_NAME (Gantt Alert Service)..."
    gcloud functions deploy "$GANTT_FUNCTION_NAME" \
        --gen2 \
        --runtime="$RUNTIME" \
        --region="$REGION" \
        --source="$GANTT_DIR" \
        --entry-point="$GANTT_ENTRY_POINT" \
        --trigger-http \
        --allow-unauthenticated \
        --timeout=540s \
        --memory=512MB
    echo_info "✅ $GANTT_FUNCTION_NAME deployed."
}

deploy_webhook() {
    echo_info "Deploying: $WEBHOOK_FUNCTION_NAME (Telegram Webhook Bot)..."
    gcloud functions deploy "$WEBHOOK_FUNCTION_NAME" \
        --gen2 \
        --runtime="$RUNTIME" \
        --region="$REGION" \
        --source="$WEBHOOK_DIR" \
        --entry-point="$WEBHOOK_ENTRY_POINT" \
        --trigger-http \
        --allow-unauthenticated \
        --timeout=60s \
        --memory=256MB
    echo_info "✅ $WEBHOOK_FUNCTION_NAME deployed."
}

deploy_attendance() {
    echo_info "Deploying: $ATTENDANCE_FUNCTION_NAME (Attendance Checker)..."
    gcloud functions deploy "$ATTENDANCE_FUNCTION_NAME" \
        --gen2 \
        --runtime="$RUNTIME" \
        --region="$REGION" \
        --source="$ATTENDANCE_DIR" \
        --entry-point="$ATTENDANCE_ENTRY_POINT" \
        --trigger-http \
        --allow-unauthenticated \
        --timeout=540s \
        --memory=256MB
    echo_info "✅ $ATTENDANCE_FUNCTION_NAME deployed."
}


# =============================================================================
# MAIN — Parse arguments and run
# =============================================================================
COMMAND="${1:-all}"

case "$COMMAND" in
    "copy")
        copy_shared_modules
        ;;
    "gantt")
        copy_shared_modules
        deploy_gantt
        ;;
    "webhook")
        copy_shared_modules
        deploy_webhook
        ;;
    "attendance")
        copy_shared_modules
        deploy_attendance
        ;;
    "all")
        copy_shared_modules
        deploy_gantt
        deploy_webhook
        deploy_attendance
        echo_info ""
        echo_info "🎉 All 3 functions deployed successfully!"
        echo_info ""
        echo_info "NEXT STEP: Set environment variables in Cloud Console:"
        echo_info "  Cloud Functions → Select function → Edit → Variables & Secrets"
        echo_info "  Variables to set: TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_CHAT_ID,"
        echo_info "                    ADMIN_CHAT_ID, SEND_INDIVIDUAL_ALERTS"
        ;;
    *)
        echo_error "Unknown command: $COMMAND"
        echo "Usage: $0 [all|copy|gantt|webhook|attendance]"
        exit 1
        ;;
esac
