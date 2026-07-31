"""
flood/render_chip.py — renderiza uno o varios GeoTIFF a PNG + JSON para Label Studio.

Label Studio no lee GeoTIFF, así que este script hace el render del lado del
servidor y deja al lado un JSON con todo lo necesario para volver de píxeles a
coordenadas cuando lleguen las etiquetas.

Usa exactamente el mismo render que la GUI: render.Chip, compartido con
flood/label.py.

Con varios TIFF del mismo lugar arma UN PNG con las vistas pegadas una al lado de
la otra. La pantalla de Label Studio es una sola para todo el proyecto y no puede
cambiar según cuántas vistas tenga cada escena, así que la comparación se resuelve
acá: Label Studio siempre ve una imagen sola, tenga un panel o cuatro.

Las vistas tienen que compartir la grilla (mismo tamaño, CRS y transform). Es lo
que hace que dibujar en cualquiera de los paneles caiga en el mismo lugar del
mapa; si no la comparten, este script se niega en vez de mentir la geometría.

Entre paneles va una franja magenta. La varita mágica es un flood fill sobre los
píxeles que se ven, y sin la franja el relleno se pasaría de un panel al otro.

Uso:
    python flood/render_chip.py data/SAR/Bolivia_60373_S1Hand.tif chips/
    python flood/render_chip.py escena.tif chips/ --view 2
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


def place_id_of(scene_ids):
    """Prefijo común, cortando por segmentos y no por letras: el prefijo de
    Bolivia_60373_S1Hand y Bolivia_60373_S2Hand es Bolivia_60373, no
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
            'source_tif': os.path.relpath(chip.path),
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
        description='Renderiza uno o varios GeoTIFF del mismo lugar a PNG + JSON')
    p.add_argument('tifs', nargs='+', help='Rutas de los GeoTIFF')
    p.add_argument('out_dir', help='Carpeta de salida')
    p.add_argument('--view', type=int, nargs='+', default=[0],
                   help='Vista de cada TIFF, en el mismo orden (default 0 para todos)')
    p.add_argument('--place-id', default='',
                   help='Nombre del lugar (default: prefijo común de los scene_id)')
    p.add_argument('--speckle', type=int, default=render.DEFAULT_SPECKLE,
                   help=f'Ventana del suavizado de speckle en SAR '
                        f'(default {render.DEFAULT_SPECKLE}, 0 = ninguno)')
    p.add_argument('--sep', type=int, default=SEP_PX,
                   help=f'Ancho de la franja separadora entre paneles '
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
