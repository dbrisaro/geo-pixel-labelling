"""
render_chip.py — renderiza un GeoTIFF a PNG + JSON para Label Studio.

Label Studio no lee GeoTIFF, así que este script hace el render del lado del
servidor y deja al lado un JSON con todo lo necesario para volver de píxeles a
coordenadas cuando lleguen las etiquetas.

Usa exactamente el mismo render que la GUI: render.Chip, compartido con
flood_label.py.

Uso:
    python render_chip.py data/SAR/Bolivia_60373_S1Hand.tif chips/
    python render_chip.py escena.tif chips/ --view 2
"""

import os
import json
import argparse

import render


def render_chip(tif, out_dir, view=0, speckle=render.DEFAULT_SPECKLE):
    chip = render.Chip(tif)
    rgb, label = chip.image(view, speckle)

    png_path = os.path.join(out_dir, f'{chip.scene_id}.png')
    json_path = os.path.join(out_dir, f'{chip.scene_id}.json')
    render.save_png(rgb, png_path)

    meta = {
        'scene_id': chip.scene_id,
        'source_tif': os.path.relpath(tif),
        'png': os.path.basename(png_path),
        'image_type': chip.kind,
        'width': chip.width,
        'height': chip.height,
        'crs': chip.crs.to_string() if chip.crs else None,
        'transform': list(chip.transform)[:6],
        'pixel_size_m': chip.pixel_size_m,
        'n_bands': chip.n_bands,
        'bands_used': chip.bands_used(),
        'band_origin': chip.band_origin,
        'view': view,
        'view_label': label,
        'speckle': speckle,
    }
    with open(json_path, 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return png_path, json_path, meta


def main():
    p = argparse.ArgumentParser(
        description='Renderiza un GeoTIFF a PNG + JSON para Label Studio')
    p.add_argument('tif', help='Ruta del GeoTIFF')
    p.add_argument('out_dir', help='Carpeta de salida')
    p.add_argument('--view', type=int, default=0,
                   help='Cuál de las vistas del chip renderizar (default 0)')
    p.add_argument('--speckle', type=int, default=render.DEFAULT_SPECKLE,
                   help=f'Ventana del suavizado de speckle en SAR '
                        f'(default {render.DEFAULT_SPECKLE}, 0 = ninguno)')
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    png_path, json_path, meta = render_chip(args.tif, args.out_dir,
                                            args.view, args.speckle)
    print(f'{meta["scene_id"]}  {meta["image_type"]}  {meta["width"]}x{meta["height"]}  '
          f'vista {meta["view"]}: {meta["view_label"]}')
    print(f'  {png_path}')
    print(f'  {json_path}')


if __name__ == '__main__':
    main()
