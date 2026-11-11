"""Folder watching: filesystem events (watchdog) + a periodic fallback scan.

Events give immediacy; the interval scan is the safety net for anything
events miss (cloud-sync quirks, network drives, missed notifications).
Both paths funnel into the same scan_inbox(), whose claim-and-lock design
makes duplicate triggers harmless.
"""
from __future__ import annotations

import logging
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import AUDIO_EXTS, Config
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
    ffmpeg is enough, with no restart.
    """
    reporter = SetupReporter(repeat_every=SETUP_REPEAT_EVERY_PASSES)
    wake = threading.Event()
    observer = Observer()
    observer.schedule(_AudioEventHandler(wake), str(cfg.inbox_dir), recursive=True)
    observer.start()
    logger.info(
        f"watching {cfg.inbox_dir} (event-driven + {interval_sec:.0f}s fallback scan). Ctrl-C to stop."
    )
    try:
        while True:
            scan_inbox(cfg, reporter=reporter)
            wake.clear()
            wake.wait(timeout=interval_sec)
    except KeyboardInterrupt:
        logger.info("stopping watcher")
    finally:
        observer.stop()
        observer.join()
