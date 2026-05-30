#!/usr/bin/env bash
# Upload your local out/ data (figures, views, projects, sources, imports)
# to the Render persistent disk at /data.
#
# Run once after first deploy to carry over your existing work.
# Safe to re-run -- rsync only copies what's changed.
#
# Usage:
#   bash upload_data_to_render.sh
#
# Requirements: ssh/scp/rsync in PATH (Git Bash on Windows has these).
# The Render service must be running (status: live).

RENDER_SSH="srv-d8d0b5ek1jcs738f9on0@ssh.frankfurt.render.com"
LOCAL_OUT="$(cd "$(dirname "$0")/out" && pwd)"
REMOTE_DATA="/data"

echo "=== IFU data upload to Render ==="
echo "Local:  $LOCAL_OUT"
echo "Remote: $RENDER_SSH:$REMOTE_DATA"
echo ""

# Folders to transfer (NOT viewer.html -- that's app code, not user data)
FOLDERS=(figures views projects sources imports)

SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes"

# Create remote directories first
echo "Creating remote directories..."
ssh $SSH_OPTS "$RENDER_SSH" "mkdir -p $REMOTE_DATA/figures $REMOTE_DATA/views $REMOTE_DATA/projects $REMOTE_DATA/sources $REMOTE_DATA/imports"
echo ""

for folder in "${FOLDERS[@]}"; do
  local_path="$LOCAL_OUT/$folder"
  if [ ! -d "$local_path" ]; then
    echo "  skip $folder (not found locally)"
    continue
  fi
  count=$(find "$local_path" -type f | wc -l | tr -d ' ')
  echo "  uploading $folder/ ($count files)..."
  scp -r $SSH_OPTS "$local_path/." "$RENDER_SSH:$REMOTE_DATA/$folder/"
  echo "  done."
  echo ""
done

echo "=== Done. Verify at https://ifu-api-xmm5.onrender.com/api/healthz ==="
