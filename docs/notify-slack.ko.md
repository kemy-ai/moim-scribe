# Slack 알림 (선택)

moim-scribe에는 Slack 연결이 들어 있지 않습니다. 녹음이 인원수를 기다리고 있을 때 Slack 메시지를 받고 싶다면, 명령 두 개로 직접 연결할 수 있습니다. 설정하지 않으면 아무것도 켜지지 않습니다.

**컴퓨터 밖으로 나가는 것.** 이 메시지에는 기다리는 녹음의 **파일 이름**이 들어갑니다. 파일 이름은 회의 제목이나 사람 이름인 경우가 많습니다. 이 이름은 Slack과 그 채널을 볼 수 있는 모든 사람에게 갑니다. 이 스크립트는 녹음 안의 내용은 보내지 않습니다. 비공개 채널을 쓰고, 제목이 민감하면 이 기능을 쓰지 마세요.

## 쓰는 명령 두 개

| 명령 | 하는 일 |
|---|---|
| `moim-scribe waiting` | 인원수를 기다리는 녹음마다 한 줄을 출력합니다. 파일 이름, 탭, 남은 시간 순서입니다(예: `team-sync.m4a` 다음에 `3:00 left`). 기다리는 녹음이 없으면 표준 출력에는 아무것도 나오지 않습니다(안내 한 줄은 오류 출력으로 나갑니다). 항상 종료 코드 0으로 끝납니다 |
| `moim-scribe count <N> [녹음]` | 인원수를 알려 줍니다. `녹음`을 생략하면 가장 최근에 기다리기 시작한 녹음에, 파일 이름 일부를 주면 그 녹음에 적용됩니다. 작업 폴더는 `--workspace 폴더`로 지정합니다 |

## 1단계: Slack 수신 주소 만들기

Slack에서 내 워크스페이스에 앱을 만들고, Incoming Webhooks를 켠 다음 채널 하나에 연결합니다(Slack 공식 안내: "Sending messages using incoming webhooks"). `https://hooks.slack.com/`으로 시작하는 주소가 나옵니다. 이 주소는 비밀번호처럼 다루세요. 주소를 아는 사람은 누구나 그 채널에 글을 올릴 수 있습니다. Git 저장소 안의 파일에 적지 마세요.

## 2단계: 스크립트

같은 스크립트가 저장소에 [examples/notify-slack.sh](examples/notify-slack.sh)로 들어 있습니다. `curl`과 POSIX 셸이 필요합니다(macOS, Linux. 윈도우는 WSL이나 Git Bash). 녹음이 기다리기 시작할 때 메시지를 한 번 보내고, 같은 녹음에 대해서는 다음 실행에서 다시 보내지 않습니다.

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

먼저 직접 실행해 보세요.

```bash
export SLACK_WEBHOOK_URL='https://hooks.slack.com/...'   # 내 주소
MOIM_WORKSPACE=~/MyMeetings sh notify-slack.sh
```

## 3단계: 1분마다 실행하기

스크립트는 한 번 확인하고 끝나므로 주기적으로 실행되게 해야 합니다. 예를 들어 cron(`crontab -e`)은 이렇게 씁니다.

```
* * * * * SLACK_WEBHOOK_URL='https://hooks.slack.com/...' MOIM_WORKSPACE=/home/USER/MyMeetings /home/USER/notify-slack.sh
```

스케줄러는 가상환경을 켜 주지 않으므로 `moim-scribe`는 전체 경로로 지정하거나 crontab에 `PATH`를 적어야 합니다. [deployment.md](deployment.md)를 참고하세요.

이 스크립트의 한계:

- 녹음을 파일 이름으로 구분합니다. 이름이 같은 녹음 둘이 동시에 기다리면 하나로 셉니다.
- 컴퓨터를 다시 켜면 보낸 기록(임시 폴더에 있음)이 사라져, 기다리는 녹음을 한 번 더 알릴 수 있습니다.
- Slack이 메시지를 받지 않으면 `curl`의 오류 메시지와 함께 실패하고, 다음 실행에서 다시 시도합니다.

## Slack에서 답장으로 인원수를 주려면

Slack에서 답장을 써도 moim-scribe에는 전달되지 않습니다. 그러려면 Slack 봇을 직접 만들어 돌려야 합니다. 봇이 할 일은 moim-scribe가 있는 컴퓨터에서 `moim-scribe count <N> <파일 이름 일부> --workspace <폴더>`를 실행하는 것뿐입니다. 봇이 없으면 터미널에서 인원수를 주거나, 작업 폴더에서 Claude에게 말하세요(README 참고).

## 대기 시간 늘리기

녹음은 기본으로 3분 기다립니다. 알림을 받고 답해야 한다면 짧은 시간입니다. `config.yaml`에서 늘리세요. 예:

```yaml
speaker_count_wait_sec: 600
```

`watch`는 60초마다 inbox를 확인하므로, 인원수를 준 뒤 실제로 시작하기까지 최대 1분쯤 걸릴 수 있습니다.

`scan-inbox`를 스케줄러로 돌린다면 `--no-wait`를 붙이거나 `speaker_count_wait_sec: 0`으로 두세요. 그렇지 않으면 실행할 때마다 그 자리에서 기다립니다([deployment.md](deployment.md) 참고).
