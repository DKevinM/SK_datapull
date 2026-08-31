#!/bin/bash
set -e

source /opt/airquality/venv/bin/activate

set -a
source /opt/airquality/config/intelligence.env
set +a

cd /opt/airquality/github/SK_datapull

LOCKFILE="/opt/airquality/locks/sk_datapull_git.lock"
mkdir -p "$(dirname "$LOCKFILE")"

(
  flock -w 600 200

  git fetch origin
  git pull --rebase origin main

  python web/PA_SK_pull.py

  git add data/SK_PA_sensors.csv

  if git diff --cached --quiet; then
      echo "No changes to commit."
  else
      git commit -m "Update SK PurpleAir sensors"
      for attempt in 1 2 3; do
          if git push origin main; then
              break
          fi
          echo "push rejected (attempt $attempt/3); rebasing onto latest and retrying..."
          git pull --rebase origin main
      done
  fi
) 200>"$LOCKFILE"
