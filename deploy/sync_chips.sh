#!/usr/bin/env bash
#
# Copies the rendered chips to the server and prints the tasks.json to import.
#
# Only the PNGs travel: nginx serves those. The JSON with the crs and the
# transform STAYS here, because it is this repository that converts pixels back
# to lon/lat when the export arrives. The server never needs to know about
# coordinates.
#
#   ./deploy/sync_chips.sh                 chips/ to the usual host
#   ./deploy/sync_chips.sh 1.2.3.4         another host
#
# The address is not in the repository: it comes from LS_HOST or from
# ~/.config/labelstudio-host, same as the API token.
#
# After this, import labels/tasks-hosted.json from the Label Studio UI
# (Project -> Import), or with the API. The image paths inside are relative
# (/chips/place.png), so they keep working when the tunnel is replaced by a
# domain.

set -euo pipefail

HOST_FILE="$HOME/.config/labelstudio-host"
HOST="${1:-${LS_HOST:-$( [ -f "$HOST_FILE" ] && cat "$HOST_FILE" )}}"
USER="${LS_USER:-ubuntu}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "$HOST" ]; then
    echo "No host. Pass it as an argument, or put it in LS_HOST or $HOST_FILE."
    exit 1
fi

cd "$ROOT"

if ! compgen -G 'chips/*.png' > /dev/null; then
    echo "There are no PNGs in chips/. Run flood/render_chip.py first."
    exit 1
fi

# One source per file instead of --include/--exclude: macOS ships openrsync,
# which does not implement the filter rules.
rsync -a chips/*.png "$USER@$HOST:/srv/chips/"

python3 flood/make_tasks.py chips/ --base-url /chips --out labels/tasks-hosted.json
