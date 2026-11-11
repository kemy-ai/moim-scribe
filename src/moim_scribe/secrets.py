"""Reading secret files (.hf_token, .soniox_key) the way users actually write them.

Windows PowerShell 5.1 `>` writes UTF-16 LE with a BOM and CRLF line ends; other
editors add a UTF-8 BOM or a trailing newline. A token read naively from such a
file is garbage that fails later, far from the cause. This module is a leaf
(no imports from the rest of the package) so both diarize.py and the Soniox
client can use it.
"""
from __future__ import annotations

import codecs
from pathlib import Path

# Hugging Face repos the code loads that need "Agree and access" on the Hub.
# diarize.diarize_turns loads speaker-diarization-3.1, and that pipeline in turn
# loads segmentation-3.0. The third is not named anywhere in this code: under
# pyannote.audio 4 the 3.1 pipeline also fetches its PLDA files from
# speaker-diarization-community-1 (observed in a real run as requests for
# .../speaker-diarization-community-1/.../plda/*.npz), so a token that was only
# granted the first two fails at load. docs/setup-pyannote.md lists the same
# three. The embedding model (speechbrain ECAPA) is not gated.
GATED_HF_REPOS = (
    "pyannote/speaker-diarization-3.1",
    "pyannote/segmentation-3.0",
    "pyannote/speaker-diarization-community-1",
)


def clean_secret(raw: bytes) -> str | None:
    """Decode secret-file bytes and return the first non-comment line, or None if empty."""
    if raw.startswith(codecs.BOM_UTF8):
        text = raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", errors="replace")  # consumes the BOM
    else:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\x00", "").replace("﻿", "")
    for line in text.splitlines():
        line = line.strip()
        # A user may put a note above the token ("# read token, made 2024").
        # No token or API key starts with "#".
        if line and not line.startswith("#"):
            return line
    return None


def read_secret_file(path: Path) -> str | None:
    """The cleaned content of a secret file, or None if absent, unreadable or empty."""
    try:
        return clean_secret(Path(path).read_bytes())
    except OSError:
        return None
