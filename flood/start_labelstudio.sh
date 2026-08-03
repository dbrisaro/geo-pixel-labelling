#!/usr/bin/env bash
#
# Starts Label Studio locally on http://localhost:8080
#
# The two variables enable serving the chips/ PNGs straight off disk, with the
# repository root as document root. That way the tasks make_tasks.py builds can
# point at /data/local-files/?d=chips/scene.png
#
# WATCH OUT: the variables are NOT enough. The /data/local-files/ endpoint also
# requires the project to have a registered Local Storage whose path is a prefix
# of the requested file; if it does not find one it returns a bare 404. See
# io_storages/localfiles/views.py, the query over LocalFilesImportStorage.
#
# Once per project, in the UI:
#   Settings → Cloud Storage → Add Source Storage → Local files
#   Absolute local path: <repository root>/chips
#   File Name Filter: ^$        (so an accidental sync imports nothing)
#   Save WITHOUT syncing: the sync would create one task per PNG and duplicate
#   the ones make_tasks.py already imported.
#
# This is only for local testing: serving the filesystem is a security risk and
# must not survive into hosting (there the PNGs go to a real URL).
#
# The Label Studio database lives in ~/Library/Application Support/label-studio,
# outside the repository. The real labels come out of the export, not that
# database.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true
export LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT="$ROOT"

exec "$ROOT/.venv-ls/bin/label-studio" start --port 8080 "$@"
