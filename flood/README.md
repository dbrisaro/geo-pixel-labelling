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
python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif \
                            data/SAR/Bolivia_60373_S2Hand.tif chips/
```

Deja un PNG y un JSON al lado con CRS, transform y tamaño, que es lo que hace
falta para volver de píxeles a coordenadas cuando las etiquetas llegan desde
Label Studio (que no lee GeoTIFF).

Con varios TIFF del mismo lugar arma **un** PNG con las vistas pegadas una al lado
de la otra, y el JSON lleva el corrimiento en x de cada panel. Ver *Ver el SAR y la
óptica juntos* más abajo.

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

El nexo entre las dos mitades es `place_id`: viaja en la tarea, vuelve en el
export, y con él `import_labels.py` sabe qué `chips/*.json` abrir. Los exports
viejos, que traen `scene_id` en vez de `place_id`, se siguen convirtiendo igual.

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

### Ver el SAR y la óptica juntos

En la óptica se distinguen cosas que en el SAR no —vegetación, urbano, y las nubes
mismas—, así que conviene tener las dos vistas a la vista mientras etiquetás. Como
las dos comparten la grilla (mismo tamaño, CRS y transform), **un píxel es el mismo
lugar en las dos**: da igual en cuál dibujes, cae en el mismo punto del mapa.

La pantalla de Label Studio es una sola para todo el proyecto y no puede cambiar
según cuántas vistas tenga cada escena, y no siempre vas a tener las dos. Por eso
el lado a lado se resuelve **antes**: `render_chip.py` pega las vistas en un solo
PNG. Label Studio ve una imagen sola, tenga un panel o cuatro, y el config no
cambia nunca.

```bash
python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif \
                            data/SAR/Bolivia_60373_S2Hand.tif chips/
python flood/make_tasks.py chips/
```

- El `place_id` sale del prefijo común de los `scene_id` cortando por segmentos:
  `Bolivia_60373_S1Hand` + `Bolivia_60373_S2Hand` → `Bolivia_60373`. Se puede
  forzar con `--place-id`.
- Entre paneles va una **franja magenta** de 8 px. La varita mágica es un flood
  fill sobre los píxeles que se ven, y sin la franja el relleno se pasaría de un
  panel al otro. El magenta puro no aparece en ninguno de los dos renders.
- `make_tasks.py` **no** le hace tarea propia a una escena que ya está dentro de un
  panel. Sin eso etiquetarías dos veces el mismo terreno.
- `import_labels.py` averigua en qué panel cayó cada región y le resta el
  corrimiento en x antes de aplicar el transform. Cada polígono queda atribuido al
  `scene_id` del panel donde lo dibujaste, más el `place_id` del lugar.
- Un trazo que **cruza la franja** no se puede atribuir a un panel: un polígono así
  se saltea con aviso, y una máscara pintada sobre los dos paneles avisa que la
  geografía va a quedar repetida.

Lo que se pierde: el zoom es de la imagen entera, así que si te acercás mucho a un
panel el otro se va de pantalla. Con chips de 512×512 los dos entran sin zoom.

**Cuidado con las regiones sin clase.** Si dibujás antes de elegir la clase,
Label Studio te deja guardar la región sin etiqueta y se pierde. En la GUI no
puede pasar porque la clase es un radio siempre seleccionado. `import_labels.py`
las cuenta y las avisa una por una en vez de tragárselas — si ves esos avisos,
son etiquetas que hay que rehacer.

**Estado de la verificación.** El ida y vuelta píxel → lon/lat → píxel da un
error de 1e-10 px, y las áreas por polígono suman exactamente la máscara. La
reproyección desde CRS proyectados (UTM) todavía no se probó: las escenas de
Bolivia vienen en EPSG:4326 y no ejercitan esa rama.

Sobre los paneles: la misma forma dibujada en el panel izquierdo y en el derecho da
coordenadas idénticas, tanto por polígono como por máscara RLE (probado con un
agujero, que devuelve el conteo de píxeles exacto). Renderizar un TIFF solo sigue
dando el PNG byte a byte igual que antes del cambio, y un export viejo se convierte
en la misma geometría.
