#!/usr/bin/env bash
#
# Levanta Label Studio local en http://localhost:8080
#
# Las dos variables habilitan que sirva los PNG de chips/ directamente del disco,
# con la raíz del repo como document root. Así las tareas que arma make_tasks.py
# pueden apuntar a /data/local-files/?d=chips/escena.png
#
# OJO: las variables NO alcanzan. El endpoint /data/local-files/ además exige que
# el proyecto tenga una Local Storage registrada cuyo path sea prefijo del archivo
# pedido; si no la encuentra devuelve 404 pelado. Ver
# io_storages/localfiles/views.py, la query sobre LocalFilesImportStorage.
#
# Una vez por proyecto, en la UI:
#   Settings → Cloud Storage → Add Source Storage → Local files
#   Absolute local path: <raíz del repo>/chips
#   File Name Filter: ^$        (para que un sync accidental no importe nada)
#   Guardar SIN sincronizar: el sync crearía una tarea por PNG y duplicaría las
#   que ya importó make_tasks.py.
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
