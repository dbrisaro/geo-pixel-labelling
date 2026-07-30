#!/usr/bin/env bash
#
# Levanta Label Studio local en http://localhost:8080
#
# Las dos variables habilitan que sirva los PNG de chips/ directamente del disco,
# con la raíz del repo como document root. Así las tareas que arma make_tasks.py
# pueden apuntar a /data/local-files/?d=chips/escena.png
#
# Esto es sólo para probar local: servir el filesystem es un riesgo de seguridad
# y no debe sobrevivir al hosting (ahí los PNG van a una URL real).
#
# La base de Label Studio queda en ~/Library/Application Support/label-studio,
# fuera del repo. Las etiquetas de verdad salen del export, no de esa base.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true
export LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT="$ROOT"

exec "$ROOT/.venv-ls/bin/label-studio" start --port 8080 "$@"
