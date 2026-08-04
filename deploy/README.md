# Hosting Label Studio

What is running, and what is still missing.

## Where it lives

An EC2 in the Suyana account, `us-east-1`:

| | |
|---|---|
| instance | name `labelstudio` |
| type | t3.medium, 30 GB gp3, Ubuntu 24.04 |
| address | not in this repository, see below |
| everything under | `/opt/labelstudio` (compose + `.env`), chips in `/srv/chips` |

The address and the instance id are not versioned: this repository is public and
they are a pointer to a machine that today has no certificate. They live in
`LS_HOST` or in `~/.config/labelstudio-host`, next to the API token, and every
script here reads them from there. Ask whoever set it up, or read it off the EC2
console.

```bash
echo 'export LS_HOST=<the ip>' >> ~/.zshrc     # or write the file
```

Around 33 USD a month while it is on. Stopped, only the disk (~2.5). Stopping it
loses nothing: the labels live in the `pgdata` volume.

## How you get in

Nothing is published to the internet: the web port is bound to `127.0.0.1`. You
reach it through an ssh tunnel, which is also what encrypts the traffic while
there is no certificate.

```bash
ssh -L 8081:localhost:80 ubuntu@"$LS_HOST"        # leave it open
```

Then `http://localhost:8081`.

**Port 8081 and not 8080** because 8080 is where the local Label Studio runs
(`flood/start_labelstudio.sh`). With both up, the browser lands on whichever one
grabbed the port and you end up labelling in the wrong place.

The port is not cosmetic: it has to match `LABEL_STUDIO_HOST` in `.env`. If they
differ, the login works and then everything answers *authentication credentials
were not provided*.

## Loading scenes to label

```bash
python flood/render_chip.py data/SAR/scene_S1.tif data/SAR/scene_S2.tif chips/
./deploy/sync_chips.sh
```

Then import `labels/tasks-hosted.json` from *Project → Import*.

The PNGs go up, the JSONs stay here. Converting the export back to coordinates
is done from the repository, same as always:

```bash
.venv-ls/bin/python flood/import_labels.py labels/export.json
```

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

Two things, and both need permissions this role does not have
(`DataScientistAdmin` can create instances but not security groups, key pairs or
DNS records):

1. **The `labelstudio.suyana.io` record** pointing at the instance — Route53 is
   denied. With a name it can have a real certificate, and with a certificate
   the team enters by web instead of by tunnel.
2. **Its own security group**, with 443 open and ssh restricted to the office.
   Today it shares a pre-existing group with another machine, so its rules are
   not ours to set — creating a group is also denied. What that group allows
   today is in the console and deliberately not written down here.

Until then, whoever labels needs an ssh account on the instance and has to open
the tunnel.
