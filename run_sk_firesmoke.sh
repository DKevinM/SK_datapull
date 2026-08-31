#!/bin/bash
set -e

source /opt/airquality/venv/bin/activate

cd /opt/airquality/github/SK_datapull

LOCKFILE="/opt/airquality/locks/sk_datapull_git.lock"
mkdir -p "$(dirname "$LOCKFILE")"

(
  flock -w 600 200

  git fetch origin
  git pull --rebase origin main

  python scripts/fetch_firesmoke.py

  git add data/firesmoke_now.png data/firesmoke_6h.png data/firesmoke_12h.png data/firesmoke_24h.png
  git add data/firesmoke_now.geojson data/firesmoke_6h.geojson data/firesmoke_12h.geojson data/firesmoke_24h.geojson

  if git diff --cached --quiet; then
      echo "No changes to commit."
  else
      git commit -m "Update SK FireSmoke overlay"
      for attempt in 1 2 3; do
          if git push origin main; then
              break
          fi
          echo "push rejected (attempt $attempt/3); rebasing onto latest and retrying..."
          git pull --rebase origin main
      done
  fi
) 200>"$LOCKFILE"
