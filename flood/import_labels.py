"""
flood/import_labels.py — export de Label Studio → flood_polygons.geojson

Cierra el circuito: Label Studio devuelve píxeles, este script les aplica el
transform de cada escena y escribe el mismo GeoJSON que produce la GUI. Los dos
caminos son intercambiables aguas abajo.

De dónde saca cada cosa:
    del export        la geometría, en píxeles de la imagen, y la clase
    de chips/*.json   el crs y el transform de esa escena
    el nexo           scene_id, que viaja en la tarea y vuelve en el export

Formato del export, verificado sobre uno real y no adivinado. Cada región deja
DOS entradas en `result`, unidas por el campo `id`:

    {"id": "abc", "from_name": "wand",  "type": "magicwand", "value": {"format": "rle", "rle": [...]}}
    {"id": "abc", "from_name": "label", "type": "labels",    "value": {"labels": ["water"], ...}}

La primera trae la forma, la segunda la clase. Si la persona dibuja sin elegir
clase antes, Label Studio la deja guardar y la segunda entrada no existe: esa
región queda huérfana. Acá se cuentan y se avisan en vez de tragarlas, porque
son etiquetas perdidas y conviene saberlo.

Tipos de geometría que se entienden:
    magicwand, brushlabels, brush   máscara RLE  → contornos con mask_to_polygons
    polygonlabels, polygon          vértices en % del ancho/alto de la imagen

Uso (con el venv de Label Studio, que es el que tiene el decodificador de RLE):
    .venv-ls/bin/python flood/import_labels.py labels/export.json
    .venv-ls/bin/python flood/import_labels.py labels/export.json --event-id bolivia-60373
    .venv-ls/bin/python flood/import_labels.py labels/export.json --dry-run
"""

import os
import re
import sys
import json
import argparse

# Anaconda exporta PROJ_DATA y GDAL_DATA apuntando a su propia instalación. Este
# script corre con el python de .venv-ls (el único que tiene el decodificador de
# RLE), y ese rasterio trae su propio proj.db: si le dejamos el de anaconda falla
# con "DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 6 is expected".
# Se descartan sólo las que apuntan afuera del intérprete que está corriendo, así
# no se rompe nada si algún día esto corre desde el env de anaconda.
for _var in ('PROJ_DATA', 'PROJ_LIB', 'GDAL_DATA', 'GDAL_DRIVER_PATH'):
    if _var in os.environ and not os.environ[_var].startswith(sys.prefix):
        del os.environ[_var]

import numpy as np
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


def load_chip_meta(chips_dir, scene_id):
    path = os.path.join(chips_dir, f'{scene_id}.json')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        meta = json.load(f)
    meta['_transform'] = rasterio.Affine(*meta['transform'])
    meta['_crs'] = CRS.from_string(meta['crs']) if meta.get('crs') else None
    return meta


def regions(result):
    """Junta las entradas de `result` por id: la forma con su clase.
    Devuelve (region_id, entrada_de_forma, clase_o_None)."""
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


def rle_to_rings(value, width, height):
    """RLE de Label Studio → anillos en coords de centro de píxel.

    decode_rle devuelve el mapa aplanado con 4 canales (RGBA); la máscara es
    "algún canal prendido". Se mira el alfa, que es el que marca lo pintado.
    """
    flat = decode_rle(value['rle'])
    n = width * height
    if flat.size == n * 4:
        mask = flat.reshape(height, width, 4)[:, :, 3] > 0
    elif flat.size == n:
        mask = flat.reshape(height, width) > 0
    else:
        raise ValueError(f'RLE de {flat.size} valores no encaja en {width}x{height}')
    return mask_to_polygons(mask)


def points_to_rings(value, width, height):
    """Vértices en % del ancho/alto → un anillo en coords de centro de píxel.
    El 0% es el borde izquierdo (esquina), y acá se trabaja en centros: de ahí
    el −0.5, la misma corrección que aplica mask_to_polygons."""
    ring = [(p[0] / 100.0 * width - 0.5, p[1] / 100.0 * height - 0.5)
            for p in value['points']]
    return [[ring]] if len(ring) >= 3 else []


def features_for_task(task, chips_dir, event_id, warn):
    data = task.get('data', {})
    scene_id = data.get('scene_id')
    if not scene_id:
        warn(f'tarea {task.get("id")} sin scene_id, la salteo')
        return []

    meta = load_chip_meta(chips_dir, scene_id)
    if meta is None:
        warn(f'{scene_id}: no encontré {scene_id}.json en {os.path.relpath(chips_dir, ROOT)}')
        return []

    transform, crs = meta['_transform'], meta['_crs']
    px_m = meta.get('pixel_size_m') or 0
    out = []

    for ann in task.get('annotations', []):
        if ann.get('was_cancelled'):
            continue
        for rid, r, cls in regions(ann.get('result', [])):
            w = r.get('original_width') or meta['width']
            h = r.get('original_height') or meta['height']
            if (w, h) != (meta['width'], meta['height']):
                warn(f'{scene_id}: el export dice {w}x{h} y el chip {meta["width"]}x'
                     f'{meta["height"]}; el PNG cambió después de etiquetar, la salteo')
                continue
            if r.get('image_rotation'):
                warn(f'{scene_id}: región {rid} etiquetada con la imagen rotada, la salteo')
                continue
            if cls is None:
                warn(f'{scene_id}: región {rid} sin clase (se dibujó sin elegirla), la salteo')
                continue

            t = r.get('type')
            polys = (rle_to_rings(r['value'], w, h) if t in MASK_TYPES
                     else points_to_rings(r['value'], w, h))
            if not polys:
                warn(f'{scene_id}: región {rid} quedó vacía, la salteo')
                continue

            for i, rings in enumerate(polys):
                n_px = rings_area_px(rings)
                pr = {'scene_id': scene_id,
                      'image_type': meta.get('image_type', ''),
                      'pixel_size_m': px_m,
                      'date': parse_date(scene_id),
                      'time_utc': parse_time(scene_id),
                      'event_id': event_id,
                      'image_path': meta.get('source_tif', ''),
                      'class': cls,
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
        description='Convierte un export de Label Studio al GeoJSON de la GUI')
    p.add_argument('export', help='JSON exportado de Label Studio')
    p.add_argument('--chips', default='chips',
                   help='Carpeta con los JSON de render_chip.py (default: chips/)')
    p.add_argument('--out', default='labels/flood_polygons.geojson',
                   help='GeoJSON de salida (default: labels/flood_polygons.geojson)')
    p.add_argument('--event-id', default='', help='Nombre del evento')
    p.add_argument('--dry-run', action='store_true',
                   help='Sólo informa, no escribe nada')
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
