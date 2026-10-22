#!/bin/zsh
set -eu
umask 077
export PATH="/Library/TeX/texbin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="${JOB_ALERTS_REPO:-$HOME/Work/Jobs/job_alerts}"
cd "$REPO_DIR"
exec "$REPO_DIR/.venv/bin/python" "$REPO_DIR/scripts/run_automation.py" "$@"
