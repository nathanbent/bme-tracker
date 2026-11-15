#!/bin/bash
# Pull the latest version and restart:  bash update.sh
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart bme-tracker
echo "Updated to $(git log -1 --format='%h %s')"
