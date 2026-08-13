# Hosting Label Studio

What is running, and what is still missing.

## Where it lives

An EC2 in the Suyana account, `us-east-1`:

| | |
|---|---|
| instance | name `labelstudio` |
| type | t3.medium, 30 GB gp3, Ubuntu 24.04 |
| name | **https://labelstudio.suyana.io** |
| the code | `/opt/etiquetado`, and `/opt/labelstudio` for compose + `.env` |
| the data | `/srv/suyana/floods_labeling/` |

The ssh address and the instance id are still not versioned, because this
repository is public. They live in `LS_HOST` or in `~/.config/labelstudio-host`,
next to the API token, and every script here reads them from there.

```bash
echo 'export LS_HOST=<the ip>' >> ~/.zshrc     # or write the file
```

Around 33 USD a month while it is on. Stopped, only the disk (~2.5). Stopping it
loses the labels nothing —they live in the `pgdata` volume— but **it does lose
the address**: see the elastic IP at the end.

## How you get in

By web, like any other site: **https://labelstudio.suyana.io**. Port 80 only
answers the Let's Encrypt challenge and redirects everything else to https.

Free registration is closed, so the login page being public does not let anybody
in; people come in by invitation, see *Accounts* below.

The certificate is Let's Encrypt, issued with `certbot --webroot -w /srv/certbot`
and renewed on its own by `certbot.timer`. nginx reads the certificate when it
starts, so a renewal on its own would not be picked up: that is what
`deploy/certbot-reload-nginx.sh` is for, installed at
`/etc/letsencrypt/renewal-hooks/deploy/`. `sudo certbot renew --dry-run` says
whether the renewal still works; it skips the hook, so to test that one, run it
by hand.

**The ssh tunnel is no longer the way in.** It still works for poking at the
machine, but `LABEL_STUDIO_HOST` in `.env` now says
`https://labelstudio.suyana.io`, and entering by a different address gives a
login that works followed by *authentication credentials were not provided* on
everything else.

## The daily circuit

There are **two machines**, and it is worth keeping them apart:

| | |
|---|---|
| **suyanahub** | where the imagery lives and where the results have to end up. Hers, backed up, the only one anybody uploads to |
| **the EC2** | where Label Studio runs. A worker: it can be stopped, rebuilt or thrown away without losing anything that is not derived |

Both have the same three folders at `/srv/suyana/floods_labeling/`, and
`deploy/daily.py` is the only thing that moves between them:

```
unlabeled/     the TIFFs. Filled on suyanahub, by hand
chips/         PNG + JSON. ONLY on the EC2: it is what nginx serves, and it
               regenerates from the TIFF whenever it is needed
labeled/       one <place>.geojson per finished place, made on the EC2 and
               pushed back to suyanahub
```

`chips/` exists on suyanahub too and stays empty on purpose. They are derived
data —the same TIFF renders byte for byte the same on both machines— so
mirroring them would duplicate a couple of MB per scene to save a command.

One turn, in order: bring down the TIFFs suyanahub has and the EC2 does not,
render whatever has no chip, create the tasks the project does not have, pull the
finished annotations and convert them to lon/lat, push the GeoJSONs back to
suyanahub.

```bash
python deploy/daily.py --dry-run     # says what it would do, touches nothing
python deploy/daily.py
```

There is no database and nothing to remember between runs. A place is pending if
it has no chip, a task is new if the project does not have its `place_id`, and an
annotation is unconverted if there is no file for it in `labeled/`. Running it
twice in a row does nothing the second time, so a turn that failed halfway just
gets picked up by the next one.

A place counts as labelled with **one** annotation. The day several people label
the same image, the consensus goes inside `convert()`, before writing.

### Setting it up on the instance

Already done, this is what was done. The code lives at `/opt/etiquetado` with its
own venv. One env and not two: the laptop splits anaconda from `.venv-ls`
because of the GUI, and here there is no GUI.

```bash
sudo install -d -o ubuntu -g ubuntu /srv/suyana/floods_labeling/{unlabeled,chips,labeled}
sudo install -d -o ubuntu -g ubuntu /opt/etiquetado
sudo apt-get install -y python3.12-venv        # Ubuntu ships python3 without venv
cd /opt/etiquetado && python3 -m venv .venv
.venv/bin/pip install -r deploy/requirements-server.txt
```

The code got there with `rsync`, not with a clone, because it is not pushed
anywhere yet:

```bash
rsync -a --exclude __pycache__ flood/ ubuntu@"$LS_HOST":/opt/etiquetado/flood/
rsync -a --exclude __pycache__ deploy/ ubuntu@"$LS_HOST":/opt/etiquetado/deploy/
```

That is also how you update it. The day this is pushed, `/opt/etiquetado` can
become a real clone and updating turns into `git pull`.

The token goes in `~/.config/labelstudio-token` on the instance, same as
everywhere else, `chmod 600`. The address is not needed: from the machine itself
Label Studio is nginx on `localhost:80`, which is the default.

### Reaching suyanahub without anybody there

The timer runs at seven in the morning with nobody logged in, so the instance
needs its own key — an agent forwarded from a laptop is not there at that hour.

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519_suyanahub    # on the EC2
```

The public half goes into `~/.ssh/authorized_keys` on suyanahub, prefixed with
`restrict`: it is a key without a passphrase sitting on a server, so it gets no
pty, no port forwarding and no agent forwarding, which is all rsync needs. And
`~/.ssh/config` on the EC2 carries the alias:

```
Host suyanahub
  Hostname <the address>
  User jupyter-daniela
  IdentityFile ~/.ssh/id_ed25519_suyanahub
  IdentitiesOnly yes
```

The alias is what goes in `FLOODS_MIRROR` in the service file
(`suyanahub:/srv/suyana/floods_labeling`), so the real address stays out of this
repository, same as `LS_HOST`.

Neither direction ever deletes. A scene taken out of suyanahub keeps its chip,
its task and its labels on the EC2, which is the point: withdrawing a file must
not throw away work already done on it.

Then the timer:

```bash
sudo cp deploy/floods-daily.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now floods-daily.timer
systemctl list-timers floods-daily.timer
journalctl -u floods-daily.service -n 50    # what the last turn did
```

Being `oneshot`, systemd will not start a second turn while one is running.

### Naming of the scenes

The views of a place travel together into a single chip, and that grouping comes
from the file name, Sen1Floods11 style: `Place_id_View.tif`. `S1Hand`, `S1Weak`,
`S2Hand` and `S2Weak` are imagery; `LabelHand`, `LabelWeak` and `JRCWaterHand`
are reference labels and are ignored, there is nothing to label in them. A name
that does not end in a known view is taken as a place of its own, one TIFF one
chip.

### Labelling from the laptop, one scene at a time

The circuit is the normal path now. What is still there for a one-off —trying a
render out, converting an export by hand— is the local GUI and
`flood/import_labels.py`, which do not touch the instance. See
[flood/README.md](../flood/README.md).

`sync_chips.sh` is gone: it existed to carry PNGs from a laptop to `/srv/chips`,
and there is nothing to carry when the rendering happens where the images
already are.

## Accounts

Free registration is closed (`LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK`). New
people come in through *Organization → People → Invite link*.

On a **new database** that setting has to start as `false`, otherwise the first
account cannot be created either: `/user/signup/` answers 403 and there is
nobody to invite you. Create the owner account, then put it back to `true` and
`docker compose up -d app`.

## Operating it

```bash
ssh ubuntu@"$LS_HOST"
cd /opt/labelstudio
sudo docker compose ps
sudo docker compose logs -f app
sudo docker compose up -d          # after editing docker-compose.yml or .env
sudo docker compose pull && sudo docker compose up -d    # upgrade Label Studio
```

## Two things that are decided differently from local

**The chips are served by nginx, not by Label Studio.**
`LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED` hands the process the whole
filesystem; it is fine on a laptop and has no place on a server. nginx publishes
`/srv/chips` under `/chips/` and that is it.

They are served **from the same origin** as Label Studio on purpose. The magic
wand reads the image pixels from the browser, and an image from another domain
comes back tainted: the wand stops working. Same origin means no CORS to
configure and no `crossOrigin` in the labeling config.

**API tokens.** Since 1.23 Label Studio ships with the classic tokens disabled,
and the API answers *legacy token authentication has been disabled*. It is
turned on in *Organization → API tokens*, which is how this project was created
and its tasks imported.

## What is missing, and needs the AWS administrator

One thing: **an elastic IP**. The record `labelstudio.suyana.io` points at the
instance's ordinary public address, which AWS hands back if the instance is ever
stopped. Start it again and it gets a different one, the name resolves to
nothing, and nobody can get in until somebody edits the record by hand. The
certificate survives, so it is a five minute fix, but it has to be noticed
first — and the whole point of stopping the instance was that it cost nothing to
do.

There is already an unassociated elastic IP in the account. `ec2:AllocateAddress`
and `ec2:AssociateAddress` are the two permissions this role does not have.

Everything else that used to be on this list is done: the security group turned
out to already allow 80, 443 and 22 (what kept this private was Docker binding
nginx to `127.0.0.1`, not the firewall), Route53 is writable now, and the
certificate came from Let's Encrypt without needing ACM, which is still denied.

**The security group is still shared** with another machine, so its rules are not
ours to tighten. Creating our own is allowed now; what stopped that being done
along with the rest is that restricting ssh needs a decision about whose
addresses get in, which is not a decision to take alone.
