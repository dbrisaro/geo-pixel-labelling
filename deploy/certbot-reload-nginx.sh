#!/bin/sh
#
# Goes in /etc/letsencrypt/renewal-hooks/deploy/ on the instance.
#
# certbot renews on its own with a systemd timer, but nginx reads the
# certificate once, when it starts. Without this the renewal happens in
# November, nobody notices, and the site goes on serving the expired one until
# a browser refuses it.
#
# It is a `deploy` hook: certbot only runs it when a certificate was actually
# renewed, not on every check.
docker compose -f /opt/labelstudio/docker-compose.yml exec -T web nginx -s reload
