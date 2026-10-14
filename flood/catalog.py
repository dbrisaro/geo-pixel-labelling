"""
flood/catalog.py — what is there to label in a folder of scenes, and bring it.

The folder where the imagery lives is not this repository: it is a Sen1Floods11
style directory, local or on another machine, with one TIFF per view and per
place:

    Colombia_1318962_S1Weak.tif     the SAR image
    Colombia_1318962_LabelHand.tif  a label that ALREADY exists

The catalogue groups by place and says, for each one, which views it has. That
matters because the three kinds of file mean three different things:

    imagery without a reference label   this is work to be done
    imagery with a reference label      already labelled by whoever published it;
                                        useful for measuring how the team labels,
                                        not for labelling blindly
    reference label without imagery     nothing to do, there is nothing to look at

In the folder this was written against, 446 of the 599 files were labels with no
imagery. The catalogue exists so that does not get confused with 599 pending
scenes.

Usage (from the repository root):
    python flood/catalog.py suyanahub:suyana/floods
    python flood/catalog.py data/floods --out labels/catalog.csv
    python flood/catalog.py suyanahub:suyana/floods --fetch data/floods --limit 3
    python flood/catalog.py data/floods --render chips/

SOURCE is a local folder or `host:path`, where `host` is anything ssh
understands (a name from ~/.ssh/config included).
"""

import os
import csv
import glob
import shlex
import argparse
import subprocess
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Suffix of the file name -> what that view is. Sen1Floods11 uses Hand for the
# split labelled by hand and Weak for the one labelled by threshold.
IMAGERY = ('S1Hand', 'S1Weak', 'S2Hand', 'S2Weak')
REFERENCE = ('LabelHand', 'LabelWeak', 'JRCWaterHand')


def split_name(filename):
    """Colombia_1318962_S1Weak.tif -> ('Colombia_1318962', 'S1Weak')"""
    stem = os.path.splitext(filename)[0]
    place, _, view = stem.rpartition('_')
    return (place, view) if place else (stem, '')


def listdir(source):
    """File names in SOURCE, which can be local or host:path."""
    if ':' in source and not os.path.isdir(source):
        host, _, path = source.partition(':')
        out = subprocess.run(['ssh', host, f'ls -1 {shlex.quote(path)}'],
                             capture_output=True, text=True, check=True)
        return [n for n in out.stdout.split('\n') if n.endswith('.tif')]
    d = source if os.path.isabs(source) else os.path.join(ROOT, source)
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, '*.tif')))


def build(source):
    views = defaultdict(dict)
    for name in listdir(source):
        place, view = split_name(name)
        views[place][view] = name

    rows = []
    for place in sorted(views):
        got = views[place]
        imagery = [v for v in IMAGERY if v in got]
        reference = [v for v in REFERENCE if v in got]
        rows.append({
            'place_id': place,
            'country': place.rsplit('_', 1)[0],
            'imagery': ' '.join(imagery),
            'reference': ' '.join(reference),
            'status': ('to_label' if imagery and not reference else
                       'has_reference' if imagery else 'no_imagery'),
            'files': [got[v] for v in imagery],
        })
    return rows


def fetch(source, rows, dest, limit=0):
    """Brings down the imagery of the given places. Only the TIFFs of the views
    that are imagery: the reference labels are not needed to label."""
    if limit:
        rows = rows[:limit]
    files = [f for r in rows for f in r['files']]
    if not files:
        raise SystemExit('There is nothing to bring down.')

    dest_abs = dest if os.path.isabs(dest) else os.path.join(ROOT, dest)
    os.makedirs(dest_abs, exist_ok=True)

    # One source argument per file, and no --info=progress2: macOS ships
    # openrsync ("rsync 2.6.9 compatible"), which has neither that flag nor
    # --files-from. Several sources on the same host is what works everywhere.
    if ':' in source and not os.path.isdir(source):
        host, _, path = source.partition(':')
        sources = [f'{host}:{path}/{f}' for f in files]
    else:
        src_abs = source if os.path.isabs(source) else os.path.join(ROOT, source)
        sources = [os.path.join(src_abs, f) for f in files]

    subprocess.run(['rsync', '-a'] + sources + [dest_abs + '/'], check=True)
    print(f'{len(files)} TIFF(s) -> {os.path.relpath(dest_abs, ROOT)}')
    return dest


def render(rows, source_dir, out_dir, limit=0, speckle=None):
    """One PNG + JSON per place. Each place goes on its own: these scenes have a
    single view, so there is no panel to stitch."""
    import render
    import render_chip

    if speckle is None:
        speckle = render.DEFAULT_SPECKLE

    src_abs = source_dir if os.path.isabs(source_dir) \
        else os.path.join(ROOT, source_dir)
    out_abs = out_dir if os.path.isabs(out_dir) else os.path.join(ROOT, out_dir)
    os.makedirs(out_abs, exist_ok=True)

    done = 0
    for row in rows:
        tifs = [os.path.join(src_abs, f) for f in row['files']]
        tifs = [t for t in tifs if os.path.exists(t)]
        if not tifs:
            continue
        _, _, meta = render_chip.render_chip(tifs, out_abs, speckle=speckle)
        print(f'  {meta["place_id"]:<28} {meta["width"]}x{meta["height"]}  '
              f'{meta["view_label"]}')
        done += 1
        if limit and done >= limit:
            break
    print(f'{done} chip(s) -> {os.path.relpath(out_abs, ROOT)}')


def main():
    p = argparse.ArgumentParser(
        description='Catalogue of scenes available to label')
    p.add_argument('source', help='Folder with the TIFFs: local or host:path')
    p.add_argument('--out', default='labels/catalog.csv',
                   help='Where to write the catalogue (default labels/catalog.csv)')
    p.add_argument('--status', default='to_label',
                   choices=['to_label', 'has_reference', 'no_imagery', 'all'],
                   help='Which places --fetch and --render act on (default to_label)')
    p.add_argument('--fetch', default='',
                   help='Bring the imagery down to this folder')
    p.add_argument('--render', dest='render_dir', default='',
                   help='Render PNG + JSON into this folder, reading from --fetch '
                        'if it was given, or from SOURCE if it is local')
    p.add_argument('--limit', type=int, default=0,
                   help='Only the first N places (to try things out)')
    p.add_argument('--speckle', type=int, default=None,
                   help='Window of the speckle smoothing while rendering. These '
                        'S1Weak scenes come noisy; raising it makes the wand '
                        'behave better. Purely visual.')
    args = p.parse_args()

    rows = build(args.source)

    counts = defaultdict(int)
    for r in rows:
        counts[r['status']] += 1
    print(f'{len(rows)} place(s) in {args.source}')
    print(f'  to_label       {counts["to_label"]:>4}  imagery without a reference label')
    print(f'  has_reference  {counts["has_reference"]:>4}  already labelled at source')
    print(f'  no_imagery     {counts["no_imagery"]:>4}  only a label, nothing to look at')

    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['place_id', 'country', 'imagery',
                                          'reference', 'status'])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    print(f'\ncatalogue -> {os.path.relpath(out, ROOT)}')

    selected = rows if args.status == 'all' \
        else [r for r in rows if r['status'] == args.status]

    if args.fetch:
        print(f'\nbringing down {args.status} ({len(selected)} place(s))')
        fetch(args.source, selected, args.fetch, args.limit)

    if args.render_dir:
        src = args.fetch or args.source
        print(f'\nrendering from {src}')
        render(selected, src, args.render_dir, args.limit, args.speckle)


if __name__ == '__main__':
    main()
