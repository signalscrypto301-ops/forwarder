#!/bin/bash
PROJECT_DIR="/root/Forwarder"
LOG_FILE="$PROJECT_DIR/auto_sync.log"

cd "$PROJECT_DIR" || exit 1
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

STATUS=$(git status --porcelain)

if [ -n "$STATUS" ]; then
    echo "[$TIMESTAMP] Changes detected in /root/Forwarder. Staging..." >> "$LOG_FILE"
    git add .
    COMMIT_MSG="Auto-sync Forwarder: $TIMESTAMP - Updated forwarder code and configurations"
    git commit -m "$COMMIT_MSG" >> "$LOG_FILE" 2>&1
    git push origin main >> "$LOG_FILE" 2>&1
    echo "[$TIMESTAMP] SUCCESS: Pushed to GitHub: $COMMIT_MSG" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] CLEAN: No changes in /root/Forwarder." >> "$LOG_FILE"
fi
