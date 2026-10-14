"""
flood/import_labels.py — Label Studio export → flood_polygons.geojson

Closes the circuit: Label Studio returns pixels, this script applies each scene's
transform to them and writes the same GeoJSON the GUI produces. The two paths are
interchangeable downstream.

Where each thing comes from:
    from the export    the geometry, in image pixels, and the class
    from chips/*.json  the crs, the transform and the panels of that place
    the link           place_id, which travels in the task and comes back in the export

A PNG can have several views of the same place stitched side by side (built by
render_chip.py). Label Studio knows nothing about that: it returns pixels of the
whole PNG. Here we work out which panel each region fell into, subtract that
panel's x offset, and only then apply the transform. Since all the panels share
the grid, it does not matter which one you drew on: it lands on the same place on
the map.

Single-view chips are treated as one lone panel with offset 0, so older exports
(the ones carrying scene_id instead of place_id) still convert along the same
path.

Export format, verified against a real one and not guessed. Each region leaves
TWO entries in `result`, joined by the `id` field:

    {"id": "abc", "from_name": "wand",  "type": "magicwand", "value": {"format": "rle", "rle": [...]}}
    {"id": "abc", "from_name": "label", "type": "labels",    "value": {"labels": ["water"], ...}}

The first carries the shape, the second the class. If the person draws without
choosing a class first, Label Studio lets them save it and the second entry does
not exist: that region is left orphaned. Here they are counted and warned about
instead of being swallowed, because they are lost labels and it is worth knowing.

Geometry types that are understood:
    magicwand, brushlabels, brush   RLE mask  → outlines via mask_to_polygons
    polygonlabels, polygon          vertices as % of the image width/height

Usage (with the Label Studio venv, which is the one that has the RLE decoder):
    .venv-ls/bin/python flood/import_labels.py labels/export.json
    .venv-ls/bin/python flood/import_labels.py labels/export.json --event-id bolivia-60373
    .venv-ls/bin/python flood/import_labels.py labels/export.json --dry-run
"""

import os
import re
import sys
import json
import argparse

# Anaconda exports PROJ_DATA and GDAL_DATA pointing at its own installation. This
# script runs with the .venv-ls python (the only one that has the RLE decoder),
# and that rasterio ships its own proj.db: if we leave anaconda's in place it
# fails with "DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 6 is
# expected". Only the ones pointing outside the running interpreter are dropped,
# so nothing breaks if one day this runs from the anaconda env.
for _var in ('PROJ_DATA', 'PROJ_LIB', 'GDAL_DATA', 'GDAL_DRIVER_PATH'):
    if _var in os.environ and not os.environ[_var].startswith(sys.prefix):
        del os.environ[_var]

import rasterio
from rasterio.crs import CRS
from label_studio_sdk.converter.brush import decode_rle

from geometry import mask_to_polygons, rings_to_coords, rings_area_px

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MASK_TYPES = {'magicwand', 'brushlabels', 'brush'}
POLY_TYPES = {'polygonlabels', 'polygon'}


def parse_date(name):
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', name)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    m = re.search(r'(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])', name)
    if m:
        s = m.group(0)
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return ''


def parse_time(name):
    m = re.search(r'T(\d{2})(\d{2})(\d{2})(?:\D|$)', name)
    return f'{m.group(1)}:{m.group(2)}:{m.group(3)}' if m else ''


def load_chip_meta(chips_dir, place_id):
    path = os.path.join(chips_dir, f'{place_id}.json')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        meta = json.load(f)
    meta['_transform'] = rasterio.Affine(*meta['transform'])
    meta['_crs'] = CRS.from_string(meta['crs']) if meta.get('crs') else None
    return meta


def panels_of(meta):
    """The panels of the chip. A single-view chip is one panel with offset 0, so
    the rest of the code does not distinguish the two cases."""
    if meta.get('panels'):
        return meta['panels']
    return [{'scene_id': meta.get('scene_id') or meta.get('place_id', ''),
             'source_tif': meta.get('source_tif', ''),
             'image_type': meta.get('image_type', ''),
             'x_offset': 0,
             'width': meta['width'],
             'height': meta['height']}]


def panel_for_ring(ring, panels):
    """Which panel a ring falls into, or None if it crosses two or lands on the
    stripe. The rings come in centre coords, so the panel starting at column x0
    and measuring w covers [x0 − 0.5, x0 + w − 0.5]."""
    xs = [v[0] for v in ring]
    lo, hi = min(xs), max(xs)
    for pan in panels:
        x0, w = pan['x_offset'], pan['width']
        if lo >= x0 - 0.5 - 1e-6 and hi <= x0 + w - 0.5 + 1e-6:
            return pan
    return None


def regions(result):
    """Joins the `result` entries by id: the shape with its class.
    Returns (region_id, shape_entry, class_or_None)."""
    labels = {}
    for r in result:
        if r.get('type') == 'labels':
            got = r.get('value', {}).get('labels') or []
            if got:
                labels[r.get('id')] = got[0]
    for r in result:
        t = r.get('type')
        if t in MASK_TYPES or t in POLY_TYPES:
            rid = r.get('id')
            cls = labels.get(rid) or (r.get('value', {}).get(t) or [None])[0]
            yield rid, r, cls


def has_water_of(task):
    """The answer to has_water, from the first annotation that was not cancelled,
    or None if nobody answered it."""
    for ann in task.get('annotations', []):
        if ann.get('was_cancelled'):
            continue
        for r in ann.get('result', []):
            if r.get('from_name') == 'has_water':
                got = r.get('value', {}).get('choices') or []
                if got:
                    return got[0]
    return None


def rle_to_mask(value, width, height):
    """Label Studio RLE → boolean mask of the whole PNG.

    decode_rle returns the flattened map with 4 channels (RGBA); the mask is
    "some channel on". We look at the alpha, which is the one that marks what was
    painted.
    """
    flat = decode_rle(value['rle'])
    n = width * height
    if flat.size == n * 4:
        return flat.reshape(height, width, 4)[:, :, 3] > 0
    if flat.size == n:
        return flat.reshape(height, width) > 0
    raise ValueError(f'RLE de {flat.size} valores no encaja en {width}x{height}')


def points_to_ring(value, width, height):
    """Vertices as % of the width/height → a ring in pixel centre coords.
    0% is the left edge (corner), and here we work in centres: hence the −0.5,
    the same correction mask_to_polygons applies."""
    return [(p[0] / 100.0 * width - 0.5, p[1] / 100.0 * height - 0.5)
            for p in value['points']]


def polygons_by_panel(r, w, h, panels, place_id, rid, warn):
    """Geometry of a region → [(panel, polygons in pixels of THAT panel)].

    A mask is clipped per panel: each piece already ends up in panel coords. A
    polygon is assigned whole to the panel that contains it, because splitting it
    would mean inventing vertices at the cut.

    Returns None if it already warned about the reason, so the caller does not
    warn again."""
    t = r.get('type')
    if t in MASK_TYPES:
        mask = rle_to_mask(r['value'], w, h)
        trozos = []
        for pan in panels:
            sub = mask[:, pan['x_offset']:pan['x_offset'] + pan['width']]
            if sub.any():
                trozos.append((pan, mask_to_polygons(sub)))
        if len(trozos) > 1:
            warn(f'{place_id}: región {rid} pintada sobre {len(trozos)} paneles; '
                 f'como son el mismo terreno la geografía va a quedar repetida, '
                 f'conviene rehacerla en uno solo')
        return trozos

    ring = points_to_ring(r['value'], w, h)
    if len(ring) < 3:
        return []
    pan = panel_for_ring(ring, panels)
    if pan is None:
        warn(f'{place_id}: región {rid} cruza el borde entre paneles o cae en la '
             f'franja separadora, la salteo')
        return None
    x0 = pan['x_offset']
    return [(pan, [[[(x - x0, y) for x, y in ring]]])]


def features_for_task(task, chips_dir, event_id, warn):
    data = task.get('data', {})
    place_id = data.get('place_id') or data.get('scene_id')
    if not place_id:
        warn(f'tarea {task.get("id")} sin place_id ni scene_id, la salteo')
        return []

    meta = load_chip_meta(chips_dir, place_id)
    if meta is None:
        warn(f'{place_id}: no encontré {place_id}.json en {os.path.relpath(chips_dir, ROOT)}')
        return []

    transform, crs = meta['_transform'], meta['_crs']
    px_m = meta.get('pixel_size_m') or 0
    panels = panels_of(meta)
    has_water = has_water_of(task)
    out = []

    for ann in task.get('annotations', []):
        if ann.get('was_cancelled'):
            continue
        for rid, r, cls in regions(ann.get('result', [])):
            w = r.get('original_width') or meta['width']
            h = r.get('original_height') or meta['height']
            if (w, h) != (meta['width'], meta['height']):
                warn(f'{place_id}: el export dice {w}x{h} y el chip {meta["width"]}x'
                     f'{meta["height"]}; el PNG cambió después de etiquetar, la salteo')
                continue
            if r.get('image_rotation'):
                warn(f'{place_id}: región {rid} etiquetada con la imagen rotada, la salteo')
                continue
            if cls is None:
                warn(f'{place_id}: región {rid} sin clase (se dibujó sin elegirla), la salteo')
                continue

            t = r.get('type')
            trozos = polygons_by_panel(r, w, h, panels, place_id, rid, warn)
            if trozos is None:
                continue
            if not any(polys for _, polys in trozos):
                warn(f'{place_id}: región {rid} quedó vacía, la salteo')
                continue

            for pan, polys in trozos:
                scene_id = pan['scene_id']
                for i, rings in enumerate(polys):
                    n_px = rings_area_px(rings)
                    pr = {'place_id': place_id,
                          'scene_id': scene_id,
                          'image_type': pan.get('image_type', ''),
                          'pixel_size_m': px_m,
                          'date': parse_date(scene_id),
                          'time_utc': parse_time(scene_id),
                          'event_id': event_id,
                          'image_path': pan.get('source_tif', ''),
                          'class': cls,
                          'has_water': has_water,
                          'n_pixels': n_px,
                          'area_ha': round(n_px * px_m ** 2 / 1e4, 3),
                          'geom_id': f'{scene_id}#{rid}' + (f'-{i}' if len(polys) > 1 else ''),
                          'source': 'label-studio',
                          'tool': t,
                          'annotator': ann.get('completed_by')}
                    out.append({'type': 'Feature', 'properties': pr,
                                'geometry': {'type': 'Polygon',
                                             'coordinates': rings_to_coords(transform, crs, rings)}})
    return out


def main():
    p = argparse.ArgumentParser(
        description="Converts a Label Studio export to the GUI's GeoJSON")
    p.add_argument('export', help='JSON exported from Label Studio')
    p.add_argument('--chips', default='chips',
                   help='Folder with the JSONs from render_chip.py (default: chips/)')
    p.add_argument('--out', default='labels/flood_polygons.geojson',
                   help='Output GeoJSON (default: labels/flood_polygons.geojson)')
    p.add_argument('--event-id', default='', help='Name of the event')
    p.add_argument('--dry-run', action='store_true',
                   help='Only reports, writes nothing')
    args = p.parse_args()

    resolve = lambda q: q if os.path.isabs(q) else os.path.join(ROOT, q)
    chips_dir, out = resolve(args.chips), resolve(args.out)

    with open(resolve(args.export)) as f:
        tasks = json.load(f)
    if isinstance(tasks, dict):
        tasks = [tasks]

    warnings = []
    warn = warnings.append
    feats = []
    for t in tasks:
        feats += features_for_task(t, chips_dir, args.event_id, warn)

    for w in warnings:
        print(f'  ⚠ {w}')

    by_class = {}
    for ft in feats:
        by_class[ft['properties']['class']] = by_class.get(ft['properties']['class'], 0) + 1
    print(f'\n{len(feats)} polígono(s) desde {len(tasks)} tarea(s)'
          f'{", " + str(len(warnings)) + " aviso(s)" if warnings else ""}')
    for c, n in sorted(by_class.items()):
        print(f'   {c:<14} {n}')

    if args.dry_run:
        print('\n--dry-run: no escribo nada')
        return
    if not feats:
        raise SystemExit('No hay nada que escribir.')

    fc = {'type': 'FeatureCollection', 'features': []}
    if os.path.exists(out):
        with open(out) as f:
            fc = json.load(f)
        fc.setdefault('features', [])
        have = {ft.get('properties', {}).get('geom_id') for ft in fc['features']}
        nuevas = [ft for ft in feats if ft['properties']['geom_id'] not in have]
        if len(nuevas) < len(feats):
            print(f'   {len(feats) - len(nuevas)} ya estaban en el geojson, no las repito')
        feats = nuevas
    fc['features'] += feats

    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(fc, f)
    os.replace(tmp, out)
    print(f'\n→ {os.path.relpath(out, ROOT)}  ({len(fc["features"])} features en total)')


if __name__ == '__main__':
    main()
