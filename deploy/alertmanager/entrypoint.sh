#!/bin/sh
# Render the Alertmanager config with the Slack webhook from the environment, then start Alertmanager.
set -eu
sed "s|__SLACK_WEBHOOK_URL__|${SLACK_WEBHOOK_URL}|" /etc/alertmanager/alertmanager.tmpl.yml > /tmp/alertmanager.yml
exec /bin/alertmanager --config.file=/tmp/alertmanager.yml --storage.path=/alertmanager
