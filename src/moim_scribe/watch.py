"""Folder watching: filesystem events (watchdog) + a periodic fallback scan.

Events give immediacy; the interval scan is the safety net for anything
events miss (cloud-sync quirks, network drives, missed notifications).
Both paths funnel into the same scan_inbox(), whose claim-and-lock design
makes duplicate triggers harmless.
"""
from __future__ import annotations

import logging
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import AUDIO_EXTS, Config
from .headcount import WAIT_POLL_SEC, WaitTracker
from .pipeline import SetupReporter, scan_inbox

logger = logging.getLogger("moim_scribe.watch")

FALLBACK_INTERVAL_SEC = 60.0
# While the setup is broken every pass finds the same problems. Say them once,
# again whenever they change, and otherwise only every this many passes (about
# an hour at the default interval), so the log stays readable.
SETUP_REPEAT_EVERY_PASSES = 60


class _AudioEventHandler(FileSystemEventHandler):
    def __init__(self, wake: threading.Event):
        self.wake = wake

    def on_any_event(self, event):
        path = getattr(event, "dest_path", "") or getattr(event, "src_path", "")
        if isinstance(path, bytes):
            path = path.decode(errors="ignore")
        # Wake on known audio extensions, and also on names carrying no
        # extension at all — a stripped extension is precisely the case an
        # extension filter cannot see, and classify_audio() in the scan makes
        # the real decision. The 60s fallback scan would find it regardless;
        # this keeps the response immediate.
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if path.lower().endswith(AUDIO_EXTS) or "." not in name:
            self.wake.set()


def watch(cfg: Config, interval_sec: float = FALLBACK_INTERVAL_SEC) -> None:
    """Block forever, scanning on events and every interval_sec as fallback.

    A setup problem (scan_inbox returns "setup_problems") does not stop the
    loop: every pass checks again, so saving the missing token or installing
    ffmpeg is enough, with no restart. Neither does an exception escaping a
    pass: it is logged and the next pass runs. Only Ctrl-C stops it.

    While recordings wait for their speaker count, it also looks every
    WAIT_POLL_SEC whether a count arrived or a wait ran out (a count is a file
    outside inbox/, so no event announces it) and passes again at once; the
    rule is headcount.WaitTracker, the one `scan-inbox` follows. With nothing
    waiting it sleeps until an event or interval_sec, as before.
    """
    reporter = SetupReporter(repeat_every=SETUP_REPEAT_EVERY_PASSES)
    tracker = WaitTracker(cfg, rescan_sec=interval_sec)
    wake = threading.Event()
    observer = Observer()
    observer.schedule(_AudioEventHandler(wake), str(cfg.inbox_dir), recursive=True)
    observer.start()
    logger.info(
        f"watching {cfg.inbox_dir} (event-driven + {interval_sec:.0f}s fallback scan). Ctrl-C to stop."
    )
    try:
        while True:
            waiting = False
            try:
                result = scan_inbox(cfg, reporter=reporter)
                waiting = isinstance(result, dict) and bool(result.get("waiting"))
            except Exception as e:  # one bad pass must not end watching
                # scan_inbox keeps per-file failures to itself; anything that
                # still gets out is a bug or an environment problem, and the
                # next pass may well succeed. KeyboardInterrupt is not an
                # Exception, so Ctrl-C still stops the loop.
                logger.error(
                    f"a pass stopped on an unexpected error, watching continues: "
                    f"{type(e).__name__}: {e}"
                )
                logger.debug("traceback of the failed pass", exc_info=True)
            wake.clear()
            if not waiting:
                wake.wait(timeout=interval_sec)
                continue
            tracker.after_pass(time.time())
            while not wake.wait(timeout=WAIT_POLL_SEC):
                if tracker.due(time.time()):
                    break
    except KeyboardInterrupt:
        logger.info("stopping watcher")
    finally:
        observer.stop()
        observer.join()
