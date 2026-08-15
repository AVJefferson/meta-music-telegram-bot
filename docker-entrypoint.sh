#!/bin/sh
set -eu

APP_USER="${APP_USER:-app}"

# Named volumes are created root-owned, so the first start has to fix ownership
# before dropping privileges. APP_UID matches the local Bot API server's uid so
# the bot can delete downloaded files from the shared bot-api volume.
if [ "$(id -u)" = "0" ]; then
    chown -R "${APP_USER}:${APP_USER}" /data || echo "entrypoint: could not chown /data" >&2
    exec setpriv --reuid="${APP_USER}" --regid="${APP_USER}" --init-groups "$@"
fi

exec "$@"
