"""
flood/render_chip.py — renders one or more GeoTIFFs to PNG + JSON for Label Studio.

Label Studio does not read GeoTIFF, so this script does the rendering on the
server side and leaves a JSON next to it with everything needed to go back from
pixels to coordinates when the labels arrive.

It uses exactly the same rendering as the GUI: render.Chip, shared with
flood/label.py.

Given several TIFFs of the same place it builds ONE PNG with the views stitched
side by side. The Label Studio screen is a single one for the whole project and
cannot change according to how many views each scene has, so the comparison is
resolved here: Label Studio always sees one lone image, whether it has one panel
or four.

The views have to share the grid (same size, CRS and transform). That is what
makes drawing on any of the panels land on the same place on the map; if they do
not share it, this script refuses instead of lying about the geometry.

Between panels goes a magenta stripe. The magic wand is a flood fill over the
pixels that are visible, and without the stripe the fill would spill from one
panel into the other.

Usage:
    python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif chips/
    python flood/render_chip.py scene.tif chips/ --view 2
    python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif \\
                                data/SAR/Bolivia_60373_S2Hand.tif chips/
"""

import os
import json
import argparse

import numpy as np

import render

SEP_PX = 8
SEP_RGB = (1.0, 0.0, 1.0)


def provenance_path(path):
    """Where the TIFF came from, for the JSON and from there for the GeoJSON.

    Relative while it stays under the working directory (`data/SAR/scene.tif`,
    which is how it reads on a laptop), absolute as soon as it does not: on the
    server the imagery lives in /srv and the working directory is the repo, and
    a `../../srv/...` is a worse pointer than the full path."""
    rel = os.path.relpath(path)
    return rel if not rel.startswith(os.pardir) else os.path.abspath(path)


def place_id_of(scene_ids):
    """Common prefix, cutting by segments and not by letters: the prefix of
    Bolivia_60373_S1Hand and Bolivia_60373_S2Hand is Bolivia_60373, not
    Bolivia_60373_S."""
    if len(scene_ids) == 1:
        return scene_ids[0]
    partes = [s.split('_') for s in scene_ids]
    comun = []
    for trozos in zip(*partes):
        if len(set(trozos)) > 1:
            break
        comun.append(trozos[0])
    return '_'.join(comun) or scene_ids[0]


def same_grid(chips):
    a = chips[0]
    for c in chips[1:]:
        if (c.width, c.height) != (a.width, a.height):
            return f'{c.scene_id} es {c.width}x{c.height} y {a.scene_id} es {a.width}x{a.height}'
        if (c.crs and c.crs.to_string()) != (a.crs and a.crs.to_string()):
            return f'{c.scene_id} está en {c.crs} y {a.scene_id} en {a.crs}'
        if not np.allclose(list(c.transform)[:6], list(a.transform)[:6],
                           rtol=0, atol=1e-12):
            return f'{c.scene_id} y {a.scene_id} no comparten el transform'
    return ''


def render_chip(tifs, out_dir, views=None, speckle=render.DEFAULT_SPECKLE,
                place_id='', sep=SEP_PX):
    chips = [render.Chip(t) for t in tifs]
    views = list(views or []) or [0]
    if len(views) == 1:
        views = views * len(chips)
    if len(views) != len(chips):
        raise SystemExit(f'Pasaste {len(chips)} TIFF y {len(views)} vista(s): '
                         f'poné una vista por TIFF, o una sola para todos')

    problema = same_grid(chips)
    if problema:
        raise SystemExit(f'Las vistas no comparten la grilla ({problema}).\n'
                         f'Un panel pegado sólo tiene sentido si un píxel es el '
                         f'mismo lugar en todas. Renderizalas por separado.')

    place_id = place_id or place_id_of([c.scene_id for c in chips])
    rendered = [c.image(v, speckle) for c, v in zip(chips, views)]

    height = chips[0].height
    total_w = sum(c.width for c in chips) + sep * (len(chips) - 1)
    canvas = np.zeros((height, total_w, 3), np.float64)

    panels, x = [], 0
    for chip, (rgb, label), view in zip(chips, rendered, views):
        canvas[:, x:x + chip.width] = rgb
        panels.append({
            'scene_id': chip.scene_id,
            'source_tif': provenance_path(chip.path),
            'image_type': chip.kind,
            'x_offset': x,
            'width': chip.width,
            'height': chip.height,
            'n_bands': chip.n_bands,
            'bands_used': chip.bands_used(),
            'band_origin': chip.band_origin,
            'view': view,
            'view_label': label,
        })
        x += chip.width
        if x < total_w:
            canvas[:, x:x + sep] = SEP_RGB
            x += sep

    a = chips[0]
    meta = {
        'place_id': place_id,
        'png': f'{place_id}.png',
        'width': total_w,
        'height': height,
        'crs': a.crs.to_string() if a.crs else None,
        'transform': list(a.transform)[:6],
        'pixel_size_m': a.pixel_size_m,
        'speckle': speckle,
        'sep_px': sep if len(chips) > 1 else 0,
        'panels': panels,
    }
    if len(chips) == 1:
        meta['scene_id'] = a.scene_id
        meta['source_tif'] = panels[0]['source_tif']
        meta['image_type'] = a.kind
        meta['n_bands'] = a.n_bands
        meta['bands_used'] = a.bands_used()
        meta['band_origin'] = a.band_origin
        meta['view'] = views[0]
        meta['view_label'] = panels[0]['view_label']

    png_path = os.path.join(out_dir, meta['png'])
    json_path = os.path.join(out_dir, f'{place_id}.json')
    render.save_png(canvas, png_path)
    with open(json_path, 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return png_path, json_path, meta


def main():
    p = argparse.ArgumentParser(
        description='Renders one or more GeoTIFFs of the same place to PNG + JSON')
    p.add_argument('tifs', nargs='+', help='Paths of the GeoTIFFs')
    p.add_argument('out_dir', help='Output folder')
    p.add_argument('--view', type=int, nargs='+', default=[0],
                   help='View of each TIFF, in the same order (default 0 for all)')
    p.add_argument('--place-id', default='',
                   help='Name of the place (default: common prefix of the scene_ids)')
    p.add_argument('--speckle', type=int, default=render.DEFAULT_SPECKLE,
                   help=f'Window of the speckle smoothing in SAR '
                        f'(default {render.DEFAULT_SPECKLE}, 0 = none)')
    p.add_argument('--sep', type=int, default=SEP_PX,
                   help=f'Width of the separating stripe between panels '
                        f'(default {SEP_PX})')
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    png_path, json_path, meta = render_chip(args.tifs, args.out_dir, args.view,
                                            args.speckle, args.place_id, args.sep)
    print(f'{meta["place_id"]}  {meta["width"]}x{meta["height"]}  '
          f'{len(meta["panels"])} panel(es)')
    for pan in meta['panels']:
        print(f'   x={pan["x_offset"]:<5} {pan["scene_id"]:<28} '
              f'{pan["image_type"]:<7} vista {pan["view"]}: {pan["view_label"]}')
    print(f'  {png_path}')
    print(f'  {json_path}')


if __name__ == '__main__':
    main()
