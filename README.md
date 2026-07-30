# Etiquetado

Herramientas de etiquetado manual sobre imágenes satelitales. El foco activo son
las **inundaciones**; el etiquetado de sargazo quedó archivado.

```
flood/                  ← activo: GUI de etiquetado de inundaciones (SAR y óptico)
  label.py              app de etiquetado
  render.py             render visual compartido (dB, stretch, despeckle, falso color)
  render_chip.py        GeoTIFF → PNG + JSON, para Label Studio

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

- Etiquetado de inundaciones: en uso.
- Hosting: en curso — la idea es servir el etiquetado con Label Studio en vez de
  la GUI local. `flood/render_chip.py` es la pieza que prepara los chips para eso.
- Sargazo: archivado en `archive/sargassum/`, funciona pero no se está tocando.
