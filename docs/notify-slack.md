# Slack notifications (optional)

moim-scribe has no Slack connection built in. If you want a Slack message when a recording is waiting for its speaker count, you can attach one yourself with two commands. Nothing here is on unless you set it up.

**What leaves your machine.** A message like this contains the **file name** of the waiting recording, and a file name is often a meeting title or a person's name. It goes to Slack and to whoever can read that channel. Nothing from inside the recording is sent by this script. Use a private channel, and leave this out if titles are sensitive.

## The two commands

| command | what it does |
|---|---|
| `moim-scribe waiting` | prints one line per recording that is waiting for a speaker count: the file name, a tab, and the time left (for example `team-sync.m4a` then `3:00 left`). With nothing waiting it prints nothing on stdout (a note goes to stderr). It always exits 0 |
| `moim-scribe count <N> [recording]` | gives the speaker count. Without `recording` it applies to the most recent waiting recording; with part of a file name it finds that one. Use `--workspace DIR` to name the workspace folder |

## Step 1: get a Slack Incoming Webhook

In Slack, create an app for your workspace, turn on Incoming Webhooks and add one for a channel (Slack's own guide: "Sending messages using incoming webhooks"). You get an address that starts with `https://hooks.slack.com/`. Treat it like a password: anyone who has it can post to that channel. Do not put it in a file inside a Git repository.

## Step 2: the script

The same script is in the repository as [examples/notify-slack.sh](examples/notify-slack.sh). It needs `curl` and a POSIX shell (macOS and Linux; on Windows use WSL or Git Bash). It sends one message when a recording starts waiting, and does not send it again for the same recording on later runs.

```sh
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
```

Run it by hand to try it:

```bash
export SLACK_WEBHOOK_URL='https://hooks.slack.com/...'   # your own address
MOIM_WORKSPACE=~/MyMeetings sh notify-slack.sh
```

## Step 3: run it every minute

The script looks once and exits, so schedule it. For example, with cron (`crontab -e`):

```
* * * * * SLACK_WEBHOOK_URL='https://hooks.slack.com/...' MOIM_WORKSPACE=/home/USER/MyMeetings /home/USER/notify-slack.sh
```

Use the full path to `moim-scribe` (or set `PATH` in the crontab), because a scheduler does not activate your virtual environment; see [deployment.md](deployment.md).

Limits of this script:

- It tells apart recordings by file name. Two waiting recordings with the same name count as one.
- If the machine restarts, the memory of what was sent (in the temp folder) may be gone and a waiting recording can be announced a second time.
- If Slack does not accept a message, the script fails with a message from `curl` and tries again on the next run.

## Give the count from Slack?

Replying in Slack does not reach moim-scribe by itself. That needs a Slack bot, which you build and run yourself. The bot only has to run `moim-scribe count <N> <part of the file name> --workspace <folder>` on the machine where moim-scribe runs. Without a bot, give the count from a terminal, or tell Claude in the workspace folder (see the README).

## Raise the wait

A recording waits 3 minutes by default. If a message has to reach you and you have to answer, that is short. Raise it in `config.yaml`, for example:

```yaml
speaker_count_wait_sec: 600
```

`watch` checks the inbox every 60 seconds, so after you give a count it can take up to a minute to start.

If you run `scan-inbox` from a scheduler, give it `--no-wait` or set `speaker_count_wait_sec: 0`; otherwise each run waits in place (see [deployment.md](deployment.md)).
