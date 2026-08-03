"""
flood/make_tasks.py — builds the tasks.json that gets imported into Label Studio.

This script walks a folder of chips (the PNG + JSON that render_chip.py left) and
writes one task per place.

Each task carries the place_id inside it. Label Studio returns it as is in the
export, and that is what later makes it possible to find the JSON with the crs,
the transform and the panels of that place in order to go back from pixels to
lon/lat.

If a chip has several panels (several views stitched into one PNG), the scenes
that compose it do NOT generate a task of their own: they are already inside the
panel. Without that the same ground would be labelled twice and the export would
bring two truths for the same pixels.

The image URL is the only thing that changes between local and hosted:

    local     /data/local-files/?d=chips/place.png
              requires LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true and
              LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT pointing at the repo root
    hosted    --base-url https://wherever/chips

Usage (from the repository root):
    python flood/make_tasks.py chips/
    python flood/make_tasks.py chips/ --base-url https://wherever/chips
    python flood/make_tasks.py chips/ --out labels/tasks.json
"""

import os
import json
import glob
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def image_url(png_path, base_url):
    if base_url:
        return f'{base_url.rstrip("/")}/{os.path.basename(png_path)}'
    rel = os.path.relpath(png_path, ROOT).replace(os.sep, '/')
    return f'/data/local-files/?d={rel}'


def load_metas(chips_dir):
    metas = []
    for meta_path in sorted(glob.glob(os.path.join(chips_dir, '*.json'))):
        with open(meta_path) as f:
            meta = json.load(f)
        if 'png' not in meta or not (meta.get('place_id') or meta.get('scene_id')):
            continue
        metas.append(meta)
    return metas


def panels_of(meta):
    if meta.get('panels'):
        return meta['panels']
    return [{'scene_id': meta.get('scene_id', ''),
             'image_type': meta.get('image_type', ''),
             'view_label': meta.get('view_label', '')}]


def make_tasks(chips_dir, base_url=''):
    metas = load_metas(chips_dir)

    dentro_de_panel = set()
    for meta in metas:
        if len(panels_of(meta)) > 1:
            dentro_de_panel.update(p['scene_id'] for p in panels_of(meta))

    tasks = []
    for meta in metas:
        place_id = meta.get('place_id') or meta['scene_id']
        panels = panels_of(meta)
        if len(panels) == 1 and panels[0]['scene_id'] in dentro_de_panel:
            print(f'  {panels[0]["scene_id"]} ya está dentro de un panel, no le hago tarea')
            continue

        png_path = os.path.join(chips_dir, meta['png'])
        if not os.path.exists(png_path):
            print(f'  aviso: falta el PNG de {place_id}, la salteo')
            continue

        tipos = list(dict.fromkeys(p['image_type'] for p in panels if p['image_type']))
        tasks.append({'data': {
            'image': image_url(png_path, base_url),
            'place_id': place_id,
            'image_type': ' + '.join(tipos),
            'view_label': ' | '.join(f'{p["scene_id"]}: {p["view_label"]}'
                                     for p in panels) if len(panels) > 1
                          else panels[0]['view_label'],
        }})
    return tasks


def main():
    p = argparse.ArgumentParser(
        description='Builds the tasks.json to import into Label Studio')
    p.add_argument('chips_dir', nargs='?', default='chips',
                   help='Folder with the PNG + JSON from render_chip.py (default: chips/)')
    p.add_argument('--base-url', default='',
                   help='Public URL where the PNGs are served. Without this it uses '
                        "Label Studio's local file serving.")
    p.add_argument('--out', default='',
                   help='Output file (default: <chips_dir>/tasks.json)')
    args = p.parse_args()

    chips_dir = args.chips_dir if os.path.isabs(args.chips_dir) \
        else os.path.join(ROOT, args.chips_dir)
    if not os.path.isdir(chips_dir):
        raise SystemExit(f'No existe la carpeta {chips_dir}')

    tasks = make_tasks(chips_dir, args.base_url)
    if not tasks:
        raise SystemExit(f'No encontré chips en {chips_dir}. '
                         f'Corré primero flood/render_chip.py')

    out = args.out or os.path.join(chips_dir, 'tasks.json')
    if not os.path.isabs(out):
        out = os.path.join(ROOT, out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

    print(f'\n{len(tasks)} tarea(s) → {os.path.relpath(out, ROOT)}')
    for t in tasks:
        d = t['data']
        print(f'   {d["place_id"]:<20} {d["image_type"]:<17} {d["image"]}')


if __name__ == '__main__':
    main()
