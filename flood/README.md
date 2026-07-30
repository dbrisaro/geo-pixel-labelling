# Etiquetado de inundaciones

GUI de etiquetado manual sobre imágenes SAR (Sentinel-1) u ópticas. La app no
clasifica nada: muestra la imagen de la mejor forma posible para interpretarla a
ojo y guarda lo que dibujás.

## Uso

```bash
python flood/label.py                          # lee data/SAR/
python flood/label.py --dir data/otra_carpeta
python flood/label.py --event-id bolivia-60373
python flood/label.py --skip-labeled           # saltea escenas ya etiquetadas
```

Las rutas relativas se resuelven contra la raíz del repo, no contra `flood/`.

## Clases

| # | clase | notas |
|---|-------|-------|
| 1 | `water` | agua, sin distinguir permanente de inundación (no es confiable a ojo en una sola imagen) |
| 2 | `dry_land` | tierra seca |
| 3 | `cloud` | solo óptico |
| 4 | `radar_shadow` | sombra/layover de relieve: se ve oscuro como el agua pero no es agua. El falso positivo principal en SAR |

## Herramientas de dibujo

Teclas `z x c v`, o el panel derecho:

- **lazo** (`z`) — contorno a mano alzada, se cierra al soltar. Lo más rápido para bordes de agua.
- **polígono** (`x`) — un clic por vértice; clic derecho o doble clic cierra, `Esc` cancela.
- **pincel** (`c`) — pintás arrastrando un círculo; la rueda del mouse cambia el radio.
- **click** (`v`) — marca un píxel suelto (puntos de control).

Mientras dibujás ves el relleno del color de la clase y las hectáreas en vivo.

## Salidas

En `labels/` (raíz del repo):

- `flood_polygons.geojson` — los polígonos, en lat/lon EPSG:4326.
- `flood_labels.csv` — los clicks sueltos, con `row`/`col` y `lat`/`lon`.

Lo ya guardado para una escena se redibuja punteado al reabrirla, así no se
etiqueta dos veces lo mismo. Lo nuevo se suma, no reemplaza.

## Render

`render.py` concentra la detección de bandas, la conversión a dB, el stretch, el
despeckle y la composición de falso color. Lo usan tanto la GUI como
`render_chip.py`, así que lo que ves etiquetando es idéntico a lo que se sirve.

```bash
python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif chips/
python flood/render_chip.py escena.tif chips/ --view 2
```

Deja un PNG y un JSON al lado con CRS, transform y tamaño, que es lo que hace
falta para volver de píxeles a coordenadas cuando las etiquetas llegan desde
Label Studio (que no lee GeoTIFF).

El suavizado de speckle es solo visual: `--speckle N` cambia la ventana, `0` la
apaga. No afecta lo que se guarda.
