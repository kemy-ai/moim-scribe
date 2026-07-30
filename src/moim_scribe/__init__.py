"""moim-scribe — local-first voice pipeline for recurring meetings.

Watch a folder for recordings, transcribe and diarize them (locally by
default, or via Soniox if configured), match speakers against an accumulating
voice-fingerprint database, and summarize with per-group templates. The more
often the same people meet, the more accurate speaker attribution becomes.
"""

__version__ = "0.1.0"
