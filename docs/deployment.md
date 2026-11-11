# Running moim-scribe in the background

`moim-scribe watch` in a terminal is fine for daily use. For a
start-at-login setup, use your OS scheduler. All three templates run the
lighter `scan-inbox` (single pass) on an interval — simpler and more robust
than supervising a long-lived watcher, and the pipeline's claim/lock design
makes overlapping runs harmless.

The `moim-scribe` command exists inside the virtual environment you installed
it into (the README install blocks create `~/moim-scribe-env`). A scheduler does
not activate that environment, so give it the full path of the executable
instead: `which moim-scribe` (macOS/Linux) or `where moim-scribe` (Windows) with
the environment active, for example `/Users/USER/moim-scribe-env/bin/moim-scribe`
(`%USERPROFILE%\moim-scribe-env\Scripts\moim-scribe.exe` on Windows). The
`~/.local/bin/moim-scribe` used below is only where `pipx` or
`pip install --user` would put it; replace it with your path.

## macOS — launchd

`~/Library/LaunchAgents/com.moim-scribe.plist` (replace USER and paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.moim-scribe</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USER/.local/bin/moim-scribe</string>
    <string>scan-inbox</string>
    <string>/Users/USER/MyMeetings</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/moim-scribe.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.moim-scribe.plist
```

Note: launchd runs with a minimal PATH. Use absolute paths, and make sure the
directories of `ffmpeg` and `claude` (the default summarizer) are visible: add an
`EnvironmentVariables` dict whose `PATH` lists them, for example
`/opt/homebrew/bin:/Users/USER/.local/bin:/usr/local/bin:/usr/bin:/bin`
(Homebrew's ffmpeg is usually in `/opt/homebrew/bin`; Claude Code is usually in
`~/.local/bin`; check with `which ffmpeg claude`). If a tool is missing,
`scan-inbox` logs the setup problem, exits 1, and leaves every recording where
it is.

A launchd job that reads a protected folder can fail with "Operation not
permitted" in its log. For `~/Documents` this is a documented case: grant Full
Disk Access to the program that runs the job (System Settings, Privacy &
Security, Full Disk Access; add the Python in your virtual environment or the
executable above) and reload the job, or keep the workspace in a folder that is
not protected. For a workspace or inbox in a cloud-synced folder
(`~/Library/CloudStorage`, iCloud Drive), check by running the job once
(`launchctl kickstart gui/$(id -u)/com.moim-scribe`) and reading its log (the
`StandardErrorPath` file above) before relying on it.

## Linux — systemd user timer

`~/.config/systemd/user/moim-scribe.service`:

```ini
[Unit]
Description=moim-scribe inbox pass

[Service]
Type=oneshot
ExecStart=%h/.local/bin/moim-scribe scan-inbox %h/MyMeetings
```

`~/.config/systemd/user/moim-scribe.timer`:

```ini
[Unit]
Description=moim-scribe every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now moim-scribe.timer
```

## Windows — Task Scheduler

```powershell
$action  = New-ScheduledTaskAction -Execute "moim-scribe.exe" `
           -Argument "scan-inbox C:\Users\USER\MyMeetings"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
           -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "moim-scribe" -Action $action -Trigger $trigger
```

## Cloud-synced inboxes (Drive, Dropbox, OneDrive)

Pointing the workspace inbox at a cloud-synced folder works and is a nice
phone-to-desktop bridge (record on the phone, it appears in the inbox).
Two things the pipeline already handles, so you don't have to:

- files that exist but are still downloading are skipped until the size is
  stable AND the file actually decodes AND, on macOS, at least 95% of it is
  physically on disk or the whole file reads back. The third check is the one
  that catches a partial download: a file still being fetched reports its
  final size from the start and its first 0.1s decodes, so a recording that
  was two-thirds downloaded passed the first two and was transcribed as if it
  were whole. A complete file can also occupy fewer blocks than its size (a
  compressed file, some network volumes); that is why a full read clears it
  instead of being skipped on every pass. On Linux and Windows the block
  check is not applied at all (Windows does not report blocks)
- a file is claimed by an atomic move before processing, so a sync client
  re-uploading results can't cause double processing

One thing to configure on your side: make sure the inbox folder is set to
"always keep on this device" (or equivalent) if your sync client supports
online-only placeholder files.

## Laptop sleep

A sleeping laptop processes nothing. If you expect recordings to arrive
while the lid is closed, either let the machine wake for scheduled tasks
(macOS: `pmset repeat wake`, Windows: "wake the computer to run this task")
or accept that processing starts at the next wake — the inbox queue makes
that safe.
