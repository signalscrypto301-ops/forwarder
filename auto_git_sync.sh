#!/bin/bash
PROJECT_DIR="/root/Forwarder"
LOG_FILE="$PROJECT_DIR/auto_sync.log"

cd "$PROJECT_DIR" || exit 1
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

# SAFETY SAFEGUARD: Automatic remote pushing is disabled to prevent accidental leaks
# of sessions, databases, environment files, or client media to GitHub.
# Use standard CI/CD deployment pipelines (Git -> Server) instead of Server -> Git.

STATUS=$(git status --porcelain)

if [ -n "$STATUS" ]; then
    echo "[$TIMESTAMP] WARNING: Uncommitted local changes detected in $PROJECT_DIR." >> "$LOG_FILE"
    echo "$STATUS" >> "$LOG_FILE"
    echo "[$TIMESTAMP] Note: Automatic git add/push disabled for security." >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] CLEAN: Working tree clean in $PROJECT_DIR." >> "$LOG_FILE"
fi

