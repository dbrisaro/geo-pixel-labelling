"""
deploy/import_tasks.py — imports tasks into the hosted Label Studio, without
repeating what is already in there.

Label Studio's own import does not deduplicate: importing the same file twice
leaves the same place twice, and then the same ground gets labelled twice by two
different people. Since the scenes arrive in batches, this script asks the
project which place_ids it already has and only sends the new ones.

It talks to Label Studio over https, at labelstudio.suyana.io. No tunnel: the
site is on the web now.

deploy/daily.py does this on the instance every morning and does it better,
because it also renders what is missing first. This one is for importing a
batch by hand from a laptop, without waiting for the timer.

The token is not in the repository: LS_TOKEN or ~/.config/labelstudio-token.

Usage (from the repository root):
    python deploy/import_tasks.py                        # labels/tasks-hosted.json
    python deploy/import_tasks.py --project 1 --dry-run
"""

import os
import json
import argparse
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN_FILE = os.path.expanduser('~/.config/labelstudio-token')


def token():
    tok = os.environ.get('LS_TOKEN', '')
    if not tok and os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE).read().strip()
    if not tok:
        raise SystemExit(f'No token. Put it in LS_TOKEN or in {TOKEN_FILE} '
                         f'(Account & Settings -> Access token).')
    return tok


def call(url, tok, payload=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Token ' + tok,
                 'Content-Type': 'application/json'},
        method='POST' if payload is not None else 'GET')
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def existing_places(base, tok, project):
    """place_ids already in the project. Paginated: with 100 scenes it does not
    fit in one page."""
    places, page = set(), 1
    while True:
        data = call(f'{base}/api/tasks/?project={project}&page={page}'
                    f'&page_size=200&fields=all', tok)
        tasks = data['tasks'] if isinstance(data, dict) else data
        if not tasks:
            return places
        for t in tasks:
            d = t.get('data') or {}
            places.add(d.get('place_id') or d.get('scene_id'))
        if isinstance(data, dict) and not data.get('next'):
            return places
        page += 1


def main():
    p = argparse.ArgumentParser(
        description='Imports tasks into Label Studio without duplicating places')
    p.add_argument('tasks', nargs='?', default='labels/tasks-hosted.json',
                   help='File from make_tasks.py (default labels/tasks-hosted.json)')
    p.add_argument('--url', default=os.environ.get('LS_URL', 'https://labelstudio.suyana.io'),
                   help='Label Studio address (default https://labelstudio.suyana.io)')
    p.add_argument('--project', type=int, default=1, help='Project id (default 1)')
    p.add_argument('--dry-run', action='store_true',
                   help='Says what it would import and does not send anything')
    args = p.parse_args()

    path = args.tasks if os.path.isabs(args.tasks) else os.path.join(ROOT, args.tasks)
    tasks = json.load(open(path))
    tok = token()
    base = args.url.rstrip('/')

    already = existing_places(base, tok, args.project)
    nuevas = [t for t in tasks
              if (t['data'].get('place_id') or t['data'].get('scene_id')) not in already]
    repetidas = len(tasks) - len(nuevas)

    print(f'{len(tasks)} task(s) in {os.path.relpath(path, ROOT)}')
    print(f'  already in project {args.project}: {repetidas}')
    print(f'  to import:                        {len(nuevas)}')
    for t in nuevas:
        print(f'    {t["data"].get("place_id") or t["data"].get("scene_id")}')

    if not nuevas or args.dry_run:
        return

    res = call(f'{base}/api/projects/{args.project}/import', tok, nuevas)
    print(f'\nimported: {res.get("task_count")}  '
          f'duplicates rejected: {res.get("duplicate_count", 0)}')


if __name__ == '__main__':
    main()
