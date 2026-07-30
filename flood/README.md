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

## Etiquetar en Label Studio en vez de la GUI

Mismo resultado, otra ventana: sale el mismo `flood_polygons.geojson`. Label
Studio nunca ve el GeoTIFF ni sabe nada de coordenadas — devuelve píxeles, y el
repo les aplica el `transform` que guardó `render_chip.py`.

```
data/SAR/*.tif ──render_chip.py──▶ chips/*.png + *.json
                                        │
                    make_tasks.py ──▶ tasks.json ──▶ Label Studio ──▶ export.json
                                        │                                   │
                                   (se queda acá) ──── import_labels.py ◀───┘
                                                              │
                                              labels/flood_polygons.geojson
```

El nexo entre las dos mitades es `scene_id`: viaja en la tarea, vuelve en el
export, y con él `import_labels.py` sabe qué `chips/*.json` abrir.

```bash
python flood/render_chip.py data/SAR/escena.tif chips/
python flood/make_tasks.py chips/
./flood/start_labelstudio.sh                      # http://localhost:8080
# etiquetar, exportar en formato JSON a labels/export.json
.venv-ls/bin/python flood/import_labels.py labels/export.json --dry-run
.venv-ls/bin/python flood/import_labels.py labels/export.json
```

El conversor corre con `.venv-ls/bin/python` y no con anaconda, porque el
decodificador de RLE viene con `label-studio-sdk`. Es el único script del repo
que usa ese env.

**Herramientas.** Label Studio tiene polígono, pincel y varita mágica; **no tiene
lazo a mano alzada**. La varita es flood fill por umbral, corre en el navegador
sin modelo, y sobre SAR en dB funciona bien porque el agua es oscura y uniforme.
Varita y pincel devuelven máscaras RLE, el polígono devuelve vértices; los tres
terminan en el mismo GeoJSON.

**Cuidado con las regiones sin clase.** Si dibujás antes de elegir la clase,
Label Studio te deja guardar la región sin etiqueta y se pierde. En la GUI no
puede pasar porque la clase es un radio siempre seleccionado. `import_labels.py`
las cuenta y las avisa una por una en vez de tragárselas — si ves esos avisos,
son etiquetas que hay que rehacer.

**Estado de la verificación.** El ida y vuelta píxel → lon/lat → píxel da un
error de 1e-10 px, y las áreas por polígono suman exactamente la máscara. La
reproyección desde CRS proyectados (UTM) todavía no se probó: las escenas de
Bolivia vienen en EPSG:4326 y no ejercitan esa rama.
