#!/bin/sh
set -e

SCHEDULE=${CRON_SCHEDULE:-"0 9,18 * * *"}
echo "$SCHEDULE python /app/main.py" > /app/crontab
exec "$@"
