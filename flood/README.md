# Flood labelling

Manual labelling GUI for SAR (Sentinel-1) or optical imagery. The app does not
classify anything: it shows the image in the most interpretable way it can and
saves whatever you draw.

## Usage

```bash
python flood/label.py                          # reads data/SAR/
python flood/label.py --dir data/another_folder
python flood/label.py --event-id bolivia-60373
python flood/label.py --skip-labeled           # skips already labelled scenes
```

Relative paths are resolved against the repository root, not against `flood/`.

## Classes

| # | class | notes |
|---|-------|-------|
| 1 | `water` | water, without distinguishing permanent from flood (not reliable by eye from a single image) |
| 2 | `dry_land` | dry land |
| 3 | `cloud` | optical only |
| 4 | `radar_shadow` | terrain shadow/layover: looks dark like water but is not water. The main false positive in SAR |

## Drawing tools

Keys `z x c v`, or the right-hand panel:

- **lasso** (`z`) — freehand outline, closes when you release. Fastest for water edges.
- **polygon** (`x`) — one click per vertex; right click or double click closes, `Esc` cancels.
- **brush** (`c`) — paint by dragging a circle; the mouse wheel changes the radius.
- **click** (`v`) — marks a single pixel (control points).

While you draw you see the fill in the class colour and the hectares live.

## Outputs

Under `labels/` (repository root):

- `flood_polygons.geojson` — the polygons, in lat/lon EPSG:4326.
- `flood_labels.csv` — the single clicks, with `row`/`col` and `lat`/`lon`.

Whatever was already saved for a scene is redrawn dashed when you reopen it, so
the same thing does not get labelled twice. New work is appended, not replaced.

## Rendering

`render.py` concentrates band detection, dB conversion, stretching, despeckling
and false colour composition. Both the GUI and `render_chip.py` use it, so what
you see while labelling is identical to what gets served.

```bash
python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif chips/
python flood/render_chip.py scene.tif chips/ --view 2
python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif \
                            data/SAR/Bolivia_60373_S2Hand.tif chips/
```

It leaves a PNG and, next to it, a JSON with CRS, transform and size, which is
what is needed to go back from pixels to coordinates when the labels arrive from
Label Studio (which does not read GeoTIFF).

Given several TIFFs of the same place it builds **one** PNG with the views
stitched side by side, and the JSON carries the x offset of each panel. See
*Seeing SAR and optical together* below.

Speckle smoothing is purely visual: `--speckle N` changes the window, `0` turns
it off. It does not affect what gets saved.

## Labelling in Label Studio instead of the GUI

Same result, different window: it produces the same `flood_polygons.geojson`.
Label Studio never sees the GeoTIFF and knows nothing about coordinates — it
returns pixels, and the repository applies the `transform` that `render_chip.py`
saved.

```
data/SAR/*.tif ──render_chip.py──▶ chips/*.png + *.json
                                        │
                    make_tasks.py ──▶ tasks.json ──▶ Label Studio ──▶ export.json
                                        │                                   │
                                   (stays here) ───── import_labels.py ◀────┘
                                                              │
                                              labels/flood_polygons.geojson
```

The link between the two halves is `place_id`: it travels in the task, comes
back in the export, and with it `import_labels.py` knows which `chips/*.json` to
open. Older exports, which carry `scene_id` instead of `place_id`, still convert
the same way.

```bash
python flood/render_chip.py data/SAR/scene.tif chips/
python flood/make_tasks.py chips/
./flood/start_labelstudio.sh                      # http://localhost:8080
# label, then export in JSON format to labels/export.json
.venv-ls/bin/python flood/import_labels.py labels/export.json --dry-run
.venv-ls/bin/python flood/import_labels.py labels/export.json
```

The converter runs with `.venv-ls/bin/python` and not with anaconda, because the
RLE decoder ships with `label-studio-sdk`. It is the only script in the
repository that uses that env.

**Tools.** Label Studio has polygon, brush and magic wand; it has **no freehand
lasso**. The wand is a threshold flood fill, runs in the browser without a
model, and works well over SAR in dB because water is dark and uniform. Wand and
brush return RLE masks, the polygon returns vertices; all three end up in the
same GeoJSON.

### Seeing SAR and optical together

The optical view distinguishes things the SAR does not —vegetation, urban areas,
and the clouds themselves—, so it is worth having both views on screen while you
label. Since both share the grid (same size, CRS and transform), **a pixel is the
same place in both**: it does not matter which one you draw on, it lands on the
same point on the map.

The Label Studio screen is a single one for the whole project and cannot change
according to how many views each scene has, and you will not always have both.
That is why the side by side is resolved **beforehand**: `render_chip.py` stitches
the views into a single PNG. Label Studio sees one lone image, whether it has one
panel or four, and the config never changes.

```bash
python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif \
                            data/SAR/Bolivia_60373_S2Hand.tif chips/
python flood/make_tasks.py chips/
```

- The `place_id` comes from the common prefix of the `scene_id`s, cut by
  segments: `Bolivia_60373_S1Hand` + `Bolivia_60373_S2Hand` → `Bolivia_60373`. It
  can be forced with `--place-id`.
- Between panels goes an 8 px **magenta stripe**. The magic wand is a flood fill
  over the pixels that are visible, and without the stripe the fill would spill
  from one panel into the other. Pure magenta appears in neither of the two
  renders.
- `make_tasks.py` does **not** give a task of its own to a scene that is already
  inside a panel. Without that you would label the same ground twice.
- `import_labels.py` works out which panel each region fell into and subtracts
  the x offset before applying the transform. Each polygon ends up attributed to
  the `scene_id` of the panel you drew it on, plus the `place_id` of the place.
- A stroke that **crosses the stripe** cannot be attributed to a panel: such a
  polygon is skipped with a warning, and a mask painted over both panels warns
  that the geography will end up duplicated.

What is lost: the zoom applies to the whole image, so if you zoom right into one
panel the other goes off screen. With 512×512 chips both fit without zooming.

**Watch out for regions with no class.** If you draw before choosing the class,
Label Studio lets you save the region without a label and it is lost. In the GUI
this cannot happen because the class is a radio button that is always selected.
`import_labels.py` counts them and warns about them one by one instead of
swallowing them — if you see those warnings, those are labels that have to be
redone.

**Verification status.** The pixel → lon/lat → pixel round trip gives an error of
1e-10 px, and the per-polygon areas sum to exactly the mask. Reprojection from
projected CRSs (UTM) has not been tested yet: the Bolivia scenes come in
EPSG:4326 and do not exercise that branch.

On the panels: the same shape drawn on the left panel and on the right one gives
identical coordinates, both by polygon and by RLE mask (tested with a hole,
which returns the exact pixel count). Rendering a single TIFF still gives the
same PNG byte for byte as before the change, and an old export converts to the
same geometry.
