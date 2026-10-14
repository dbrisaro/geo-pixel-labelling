"""
deploy/daily.py — one full turn of the circuit, thought to run on a timer.

There are two machines and it is worth keeping them apart:

    suyanahub   where the imagery lives and where the results have to end up.
                It is hers, it is the one that gets backed up, and it is the
                only one anybody uploads anything to.
    the EC2     where Label Studio runs. A worker: it can be stopped, rebuilt
                or thrown away without losing anything that is not derived.

Both have the same three folders, and this script is the only thing that moves
between them:

    floods_labeling/unlabeled/   the TIFFs. Filled on suyanahub, by hand, by her
                    chips/       PNG + JSON, made here. ONLY on the EC2: it is
                                 what nginx serves, and it regenerates from the
                                 TIFF whenever it is needed
                    labeled/     one GeoJSON per place, made here and pushed
                                 back to suyanahub

One turn, in order:

    1. bring down the TIFFs that suyanahub has and the EC2 does not
    2. render whatever has no chip
    3. create the tasks the project does not have
    4. pull the finished annotations, convert them to lon/lat
    5. push the GeoJSONs back to suyanahub

There is no database and nothing to remember between runs. A place is pending if
it has no chip, a task is new if the project does not have its place_id, and an
annotation is unconverted if its file in labeled/ is missing or carries a
different updated_at. Running this twice in a row does nothing the second time.

A place counts as labelled with ONE annotation. The day several people label the
same image, this is the place where the consensus goes, before writing.

Usage (from the repository root):
    python deploy/daily.py --dry-run
    python deploy/daily.py
    python deploy/daily.py --root /somewhere/else --project 2
    python deploy/daily.py --mirror ''          # no suyanahub, only local

The address and the token come from LS_URL / LS_TOKEN or from
~/.config/labelstudio-token, same as the rest of deploy/. The other machine
comes from FLOODS_MIRROR or --mirror, and is never hardcoded here.
"""

import os
import sys
import json
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'flood'))

# Same cleanup import_labels does, and for the same reason (the comment is there).
# It has to happen HERE, before render_chip pulls rasterio in: whoever imports it
# first is the one that fixes which proj.db gets used.
for _var in ('PROJ_DATA', 'PROJ_LIB', 'GDAL_DATA', 'GDAL_DRIVER_PATH'):
    if _var in os.environ and not os.environ[_var].startswith(sys.prefix):
        del os.environ[_var]

import catalog
import render_chip
import make_tasks
import import_labels

import import_tasks

FLOODS_ROOT = os.environ.get('FLOODS_ROOT', '/srv/suyana/floods_labeling')

# The other machine, as `user@host:/path/to/floods_labeling`. Empty means working
# alone against the local folders, which is what --dry-run tests and what a
# laptop does.
FLOODS_MIRROR = os.environ.get('FLOODS_MIRROR', '')

# nginx publishes chips/ under /chips/, on the same origin as Label Studio. It is
# relative on purpose: it keeps working the day the tunnel becomes a domain.
BASE_URL = '/chips'


def rsync(sources, dest, dry, verbo):
    """rsync -a, or says what it would move. Returns how many files it moved.

    One source per file and not --include/--exclude on purpose: the same reason
    catalog.py gives, that this has to keep working when the other end is a mac
    with openrsync, which does not implement the filter rules."""
    if not sources:
        return 0
    cmd = ['rsync', '-a', '--out-format=%n'] + (['-n'] if dry else []) + sources + [dest]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    movidos = [l for l in out.split('\n') if l.strip() and not l.endswith('/')]
    for name in movidos:
        print(f'  {verbo[dry]} {os.path.basename(name)}')
    return len(movidos)


def pull_unlabeled(mirror, unlabeled, dry):
    """The TIFFs that suyanahub has and this machine does not.

    rsync compares by name and time, so what is already down does not travel
    again. It never deletes: a scene taken out of suyanahub keeps its chip, its
    task and its labels here, which is the whole point of not deleting.

    The names are listed first and passed one by one, instead of a `*.tif`: a
    glob that matches nothing makes rsync exit with 23, and an empty unlabeled/
    on suyanahub would take the whole turn down with it."""
    names = catalog.listdir(f'{mirror}/unlabeled')
    sources = [f'{mirror}/unlabeled/{n}' for n in names]
    return rsync(sources, unlabeled + '/', dry, ('traigo', 'traería'))


def push_labeled(mirror, labeled, dry):
    """The GeoJSONs, back to suyanahub. That folder is the point of all this."""
    if not os.listdir(labeled):
        return 0
    return rsync([labeled + '/'], f'{mirror}/labeled/', dry, ('subo', 'subiría'))


def places_in(unlabeled):
    """{place_id: [tif paths]}, the imagery only.

    The views of a place are stitched into a single chip, so they travel
    together. Reference labels (LabelHand and company) are not imagery and are
    left out. A name that does not end in a known view is a place of its own:
    without that, `Colombia_1318962.tif` would be read as the view `1318962` of
    a place called `Colombia` and would be lumped in with every other Colombia.
    """
    grupos = {}
    for name in sorted(os.listdir(unlabeled)):
        if not name.endswith('.tif'):
            continue
        place, view = catalog.split_name(name)
        if view in catalog.REFERENCE:
            continue
        if view not in catalog.IMAGERY:
            place = os.path.splitext(name)[0]
        grupos.setdefault(place, []).append(os.path.join(unlabeled, name))
    return grupos


def render_pending(unlabeled, chips, speckle, dry):
    """Renders the places that have no chip. Returns how many it made."""
    hechos = 0
    for place, tifs in sorted(places_in(unlabeled).items()):
        if os.path.exists(os.path.join(chips, f'{place}.json')):
            continue
        if dry:
            print(f'  renderizaría {place} ({len(tifs)} vista/s)')
            hechos += 1
            continue
        # A place that cannot be rendered —views on different grids, a broken
        # TIFF— must not take the run down with it: tomorrow it would break at
        # the same place and nothing else would ever get through.
        try:
            _, _, meta = render_chip.render_chip(tifs, chips, speckle=speckle,
                                                 place_id=place)
        except (Exception, SystemExit) as e:
            print(f'  ⚠ {place}: no lo pude renderizar, lo dejo para después\n'
                  f'    {str(e).splitlines()[0]}')
            continue
        print(f'  {place:<28} {meta["width"]}x{meta["height"]}  '
              f'{len(meta["panels"])} panel(es)')
        hechos += 1
    return hechos


def push_tasks(chips, url, tok, project, dry):
    """Creates the tasks that the project does not have. Returns how many."""
    tasks = make_tasks.make_tasks(chips, BASE_URL)
    if not tasks:
        return 0

    ya = import_tasks.existing_places(url, tok, project)
    nuevas = [t for t in tasks if t['data']['place_id'] not in ya]
    if not nuevas:
        return 0

    for t in nuevas:
        print(f'  {"importaría" if dry else "importo"} {t["data"]["place_id"]}')
    if dry:
        return len(nuevas)

    res = import_tasks.call(f'{url}/api/projects/{project}/import', tok, nuevas)
    return res.get('task_count', len(nuevas))


def fetch_annotated(url, tok, project):
    """The tasks with annotations, as the export button gives them.

    download_all_tasks=false is the default and is what we want: the ones nobody
    labelled yet have nothing to convert.
    """
    return import_tasks.call(
        f'{url}/api/projects/{project}/export'
        f'?exportType=JSON&download_all_tasks=false', tok)


def place_of(task):
    data = task.get('data') or {}
    return data.get('place_id') or data.get('scene_id') or ''


def last_update(task):
    """When the annotations of the task were last touched, to know whether the
    GeoJSON already written is still the current one."""
    return max((a.get('updated_at') or '' for a in task.get('annotations', [])),
               default='')


def written_update(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f).get('updated_at')


def convert(tasks, chips, labeled, event_id, dry, warn):
    """Writes labeled/<place>.geojson for each annotated place that has none, or
    whose annotation changed since it was written.

    A place answered with has_water and nothing drawn (a dry scene, or one
    nobody could tell) is written too, with no features: that answer is a label
    as much as a polygon is."""
    hechos = 0
    for task in tasks:
        place = place_of(task)
        if not place:
            warn(f'tarea {task.get("id")} sin place_id ni scene_id, la salteo')
            continue
        if not task.get('annotations'):
            continue
        out = os.path.join(labeled, f'{place}.geojson')
        updated = last_update(task)
        if written_update(out) == updated:
            continue

        feats = import_labels.features_for_task(task, chips, event_id, warn)
        has_water = import_labels.has_water_of(task)
        if not feats and has_water is None:
            warn(f'{place}: la anotación no dejó ningún polígono ni respuesta a '
                 f'has_water, no escribo nada')
            continue
        if dry:
            print(f'  escribiría {place}.geojson ({len(feats)} polígono/s, '
                  f'has_water={has_water})')
            hechos += 1
            continue

        tmp = out + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'type': 'FeatureCollection', 'place_id': place,
                       'has_water': has_water, 'updated_at': updated,
                       'features': feats}, f)
        os.replace(tmp, out)
        print(f'  {place}.geojson  {len(feats)} polígono(s), has_water={has_water}')
        hechos += 1
    return hechos


def main():
    p = argparse.ArgumentParser(
        description='One turn of the circuit: renders, imports tasks and brings '
                    'the finished labels back')
    p.add_argument('--root', default=FLOODS_ROOT,
                   help=f'Folder with unlabeled/, chips/ and labeled/ '
                        f'(default {FLOODS_ROOT})')
    p.add_argument('--url', default=os.environ.get('LS_URL', 'https://labelstudio.suyana.io'),
                   help='Label Studio address (default https://labelstudio.suyana.io, '
                        'which /etc/hosts on the instance sends to 127.0.0.1)')
    p.add_argument('--project', type=int, default=1, help='Project id (default 1)')
    p.add_argument('--event-id', default='', help='Name of the event, goes in the GeoJSON')
    p.add_argument('--speckle', type=int, default=None,
                   help='Window of the speckle smoothing while rendering')
    p.add_argument('--mirror', default=FLOODS_MIRROR,
                   help='The other machine, as user@host:/path/to/floods_labeling. '
                        'Empty means work alone against the local folders.')
    p.add_argument('--dry-run', action='store_true',
                   help='Says what it would do and touches nothing')
    args = p.parse_args()

    unlabeled = os.path.join(args.root, 'unlabeled')
    chips = os.path.join(args.root, 'chips')
    labeled = os.path.join(args.root, 'labeled')
    if not os.path.isdir(unlabeled):
        raise SystemExit(f'No existe {unlabeled}')
    if not args.dry_run:
        os.makedirs(chips, exist_ok=True)
        os.makedirs(labeled, exist_ok=True)

    speckle = args.speckle
    if speckle is None:
        import render
        speckle = render.DEFAULT_SPECKLE

    tok = import_tasks.token()
    url = args.url.rstrip('/')
    mirror = args.mirror.rstrip('/')
    warnings = []

    n_tif = 0
    if mirror:
        print(f'bringing down what {mirror.split(":")[0]} has and this machine does not')
        n_tif = pull_unlabeled(mirror, unlabeled, args.dry_run)

    print('\nrendering what has no chip')
    n_chips = render_pending(unlabeled, chips, speckle, args.dry_run)

    print('\nimporting the tasks the project does not have')
    n_tasks = push_tasks(chips, url, tok, args.project, args.dry_run) \
        if os.path.isdir(chips) else 0

    print('\nbringing back what is already labelled')
    tasks = fetch_annotated(url, tok, args.project)
    n_geo = convert(tasks, chips, labeled, args.event_id, args.dry_run,
                    warnings.append)

    n_subidos = 0
    if mirror:
        print(f'\nsending the labels back to {mirror.split(":")[0]}')
        n_subidos = push_labeled(mirror, labeled, args.dry_run)

    for w in warnings:
        print(f'  ⚠ {w}')
    print(f'\n{n_tif} tif(s), {n_chips} chip(s), {n_tasks} tarea(s), '
          f'{n_geo} geojson(s), {n_subidos} subido(s)'
          f'{", " + str(len(warnings)) + " aviso(s)" if warnings else ""}')
    if args.dry_run:
        print('--dry-run: no toqué nada')


if __name__ == '__main__':
    main()
