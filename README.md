# Etiquetado

Herramientas de etiquetado manual sobre imágenes satelitales. El foco activo son
las **inundaciones**; el etiquetado de sargazo quedó archivado.

```
flood/                  ← activo: GUI de etiquetado de inundaciones (SAR y óptico)
  label.py              app de etiquetado
  render.py             render visual compartido (dB, stretch, despeckle, falso color)
  geometry.py           píxeles ↔ lon/lat, compartido por la GUI y el conversor
  render_chip.py        GeoTIFF → PNG + JSON, para Label Studio
  make_tasks.py         chips/ → tasks.json, lo que se importa en Label Studio
  import_labels.py      export de Label Studio → flood_polygons.geojson
  labeling_config.xml   la pantalla de etiquetado (4 clases, 3 herramientas)
  start_labelstudio.sh  levanta Label Studio local en :8080

data/                   imágenes de entrada
  SAR/                  escenas Sentinel-1 (default de flood/label.py)

labels/                  salidas del etiquetado (geojson + csv)
chips/                   PNG + JSON renderizados (no versionado)

archive/sargassum/       pausado: GUI de sargazo con overlay de FAI, ver su README
```

## Instalación

```bash
pip install -r requirements.txt
```

Python 3.9+. Los scripts se corren **desde la raíz del repo**:

```bash
python flood/label.py
```

Ver [flood/README.md](flood/README.md) para las clases, herramientas de dibujo,
formato de salida y opciones.

## Estado

- Etiquetado de inundaciones con la GUI local: en uso.
- Circuito con Label Studio: **funcionando de punta a punta en local**. Falta
  hostearlo (URL pública, usuarios, dónde viven los PNG). Ver
  [flood/README.md](flood/README.md).
- Sargazo: archivado en `archive/sargassum/`, funciona pero no se está tocando.

El etiquetado corre con el python de anaconda. `import_labels.py` es la única
excepción: usa `.venv-ls/`, un venv aparte que se creó para no meter Label Studio
y sus dependencias en el env que corre la GUI.
