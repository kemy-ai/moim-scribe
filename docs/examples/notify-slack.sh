#!/bin/sh
# Post to Slack when recordings are waiting for a speaker count.
# Run it on a schedule (see docs/notify-slack.md). POSIX sh + curl.
#
#   SLACK_WEBHOOK_URL  required: your Slack Incoming Webhook URL (keep it secret)
#   MOIM_WORKSPACE     optional: the workspace folder (default: current folder)
#   NOTIFIED_FILE      optional: where to remember what was already sent
set -eu

: "${SLACK_WEBHOOK_URL:?set SLACK_WEBHOOK_URL to your Slack Incoming Webhook URL}"
if [ -n "${MOIM_WORKSPACE:-}" ]; then
    cd "$MOIM_WORKSPACE"
fi
STATE="${NOTIFIED_FILE:-${TMPDIR:-/tmp}/moim-scribe-notified-$(id -u)}"

waiting=$(moim-scribe waiting)
if [ -z "$waiting" ]; then
    : > "$STATE"          # nothing waits: forget, so a later recording is announced again
    exit 0
fi

# One key per waiting recording: its file name, the part of the line before the tab.
# The countdown after the tab changes on every call, so it is not part of the key:
# a recording is announced once while it waits.
old=""
[ -f "$STATE" ] && old=$(cat "$STATE")
now_keys=""
new_lines=""
while IFS= read -r line; do
    key=$(printf '%s' "$line" | cut -f 1 | cksum | cut -d ' ' -f 1)
    now_keys="$now_keys$key
"
    if ! printf '%s\n' "$old" | grep -qx "$key"; then
        new_lines="$new_lines$line
"
    fi
done <<EOF_WAITING
$waiting
EOF_WAITING

if [ -z "$new_lines" ]; then
    printf '%s' "$now_keys" > "$STATE"
    exit 0
fi

# Escape for JSON: backslash, double quote; tab and CR become spaces; one "\n" per line.
text=$(printf 'Waiting for a speaker count (give it with: moim-scribe count N):\n%s' "$new_lines" \
    | tr '\t\r' '  ' \
    | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/$/\\n/' \
    | tr -d '\n')
payload="{\"text\": \"$text\"}"

# The address goes in on stdin so it does not show in the process list.
printf 'url = "%s"\n' "$SLACK_WEBHOOK_URL" \
    | curl -fsS --max-time 30 -K - -X POST -H 'Content-Type: application/json' --data "$payload" \
        -o /dev/null

printf '%s' "$now_keys" > "$STATE"    # only after Slack accepted it: a failed send is retried
