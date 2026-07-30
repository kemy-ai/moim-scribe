"""Diarization by way of Soniox, as an alternative to whisper + pyannote.

The local path runs two models and merges their output; this one makes a single
request that comes back already diarized. What happens *after* that is
identical, and deliberately so: identify_and_repair and write_outputs are
imported from diarize.py rather than reimplemented, so both engines produce the
same three files with the same schema, and a fix to speaker identification
reaches both at once.

🔴 The recording leaves the machine here. Choosing this engine means uploading
the audio to Soniox, and the summarizer choice is a separate question — with
`stt: soniox` and `summarizer: none` the audio goes out and the transcript
stays home; the reverse is also possible. docs/privacy.md states both.

Two things the local path has and this one cannot:
  - **Speaker count is not adjustable.** Soniox exposes diarization as a single
    boolean and detects up to 15 speakers on its own; there is no equivalent of
    pyannote's num_speakers. A count in the filename or spoken at the start of
    the recording is therefore ignored, and this module says so in the log
    rather than letting the hint appear to work.
  - **No glossary in the prompt sense.** The same glossary text is sent as
    Soniox's `context` field, which is close but not the same mechanism.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from .diarize import identify_and_repair, load_initial_prompt, write_outputs
from .speakers.db import SpeakerDB
from .stt import soniox_client

logger = logging.getLogger("moim_scribe.diarize_cloud")

UNKNOWN_SPEAKER_LABEL = "UNKNOWN"  # matches diarize.speaker_at's fallback


def _audio_duration_sec(path: str) -> float | None:
    """Duration via ffprobe, or None if it cannot be determined.

    Used only to refuse a file that exceeds Soniox's fixed 300-minute cap
    before uploading it — the upload would otherwise spend the bandwidth and
    the wait before the server rejects it. Unknown duration proceeds: guessing
    the file is too long and refusing it would be worse than trying.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if r.returncode != 0:
            return None
        return float(json.loads(r.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def _label_map(speaker_ids: set) -> dict:
    """Map Soniox speaker ids onto this project's SPEAKER_NN labels.

    Soniox returns ids like "1", "2"; everything downstream — the lenses, the
    label-confidence header, the enroll instructions — talks about SPEAKER_NN.
    Renaming here keeps that vocabulary true on both engines instead of leaking
    a provider detail into what the user reads.

    Numeric ids sort numerically so SPEAKER_00 is Soniox's first speaker rather
    than whichever id happens to sort first as a string.
    """
    def sort_key(v):
        try:
            return (0, int(v), "")
        except (TypeError, ValueError):
            return (1, 0, str(v))

    return {sid: f"SPEAKER_{i:02d}" for i, sid in enumerate(sorted(speaker_ids, key=sort_key))}


def normalize_segments(merged: list[dict]) -> list[dict]:
    """Bring Soniox segments into the shape the rest of the code expects.

    Three rules, and each exists because the local path never produces the
    thing being fixed:
      1. Empty text is dropped, matching transcribe_segments, which keeps only
         segments whose text survives .strip().
      2. speaker=None becomes UNKNOWN. Soniox can leave silence and special
         tokens unattributed even with diarization on; pyannote always assigns
         something. Left as None it would reach the outputs as "None:" in the
         txt and a null label in speakers.json.
      3. Provider ids become SPEAKER_NN (see _label_map).

    Must run before identification, so that identify_speakers and write_outputs
    group by the same label space the local path uses.

    Returns a new list; the input is not mutated.
    """
    kept = [m for m in merged if m.get("text", "").strip()]
    labels = _label_map({m["speaker"] for m in kept if m.get("speaker") is not None})
    out = []
    for m in kept:
        m = dict(m)
        sid = m.get("speaker")
        m["speaker"] = UNKNOWN_SPEAKER_LABEL if sid is None else labels[sid]
        out.append(m)
    return out


def run_cloud_diarization(
    input_path: str,
    api_key: str,
    speaker_db: SpeakerDB,
    model_dir: Path,
    region: str = soniox_client.DEFAULT_REGION,
    glossary_path: Path | None = None,
    language_hints: list[str] | None = None,
    num_speakers: int | None = None,
    identify: bool = True,
) -> dict:
    """Transcribe and diarize one file through Soniox, then identify speakers.

    Writes the same three files as run_diarization, next to the input:
      <prefix>transcript_diarized.jsonl
      <prefix>transcript_diarized.txt
      <prefix>speakers.json

    num_speakers is accepted and ignored — see the module docstring. It is in
    the signature so the caller can pass whatever it parsed without knowing
    which engine is active, and so the log can name the hint being dropped.

    🔴 Output is written before the remote copies are deleted. Transcription is
    the paid, unrepeatable step; if cleanup hangs or fails, the local files are
    already on disk.
    """
    if not os.path.isfile(input_path):
        raise ValueError(
            f"the Soniox path takes a single file, not a directory: {input_path}"
        )

    session = os.path.dirname(os.path.abspath(input_path)) or "."
    prefix = os.path.splitext(os.path.basename(input_path))[0] + "_"

    t0 = time.time()
    duration = _audio_duration_sec(input_path)
    if duration is not None and duration > soniox_client.MAX_AUDIO_MINUTES * 60:
        raise ValueError(
            f"{duration / 60:.0f} minutes exceeds Soniox's {soniox_client.MAX_AUDIO_MINUTES}-minute "
            "limit, which cannot be raised. Split the recording, or set stt: mlx "
            "in config.yaml to transcribe this one locally."
        )

    if num_speakers:
        logger.info(
            f"speaker count hint ({num_speakers}) ignored — Soniox detects speakers "
            "itself and takes no count. Use a local backend when the count matters."
        )

    context = load_initial_prompt(glossary_path) if glossary_path else None
    if context:
        logger.info(f"glossary sent as context ({len(context)} chars)")

    base_url = soniox_client.region_base_url(region)
    logger.info(f"Soniox: region={region}, model={soniox_client.DEFAULT_MODEL}"
                + (f", {duration / 60:.0f} min" if duration else ""))

    file_id: str | None = None
    transcription_id: str | None = None
    try:
        t_up = time.time()
        logger.info("uploading…")
        file_id = soniox_client.upload_file(input_path, base_url, api_key)
        logger.info(f"  uploaded in {time.time() - t_up:.0f}s")

        transcription_id = soniox_client.create_transcription(
            file_id, base_url, api_key,
            language_hints=language_hints, diarization=True, context=context,
        )
        t_wait = time.time()
        soniox_client.wait_for_completion(transcription_id, base_url, api_key)
        logger.info(f"  transcribed in {time.time() - t_wait:.0f}s")

        tokens = soniox_client.get_transcript(transcription_id, base_url, api_key).get("tokens", [])
        merged = normalize_segments(soniox_client.tokens_to_segments(tokens))
        if not merged:
            raise RuntimeError(
                "Soniox returned no usable segments — the recording may be silent "
                "or in a format it could not decode"
            )
        n_clusters = len({m["speaker"] for m in merged})
        logger.info(f"  {len(merged)} segments, {n_clusters} speakers")

        # Snapshot before names replace ids, so merge detection can still see
        # which segments came from the same provider cluster.
        raw_labels = [m["speaker"] for m in merged]
        merged, id_mapping, id_sims, merge_info = identify_and_repair(
            input_path, merged, raw_labels, speaker_db, model_dir, identify
        )

        out_jsonl, out_txt, out_speakers, speakers_json = write_outputs(
            session, prefix, merged, id_mapping, id_sims, merge_info
        )
        logger.info(f"done -> {out_txt} ({time.time() - t0:.0f}s total)")
    finally:
        # Always, including on failure: an upload that is never deleted keeps
        # counting against the 10 GB / 1,000 file cap, and a half-finished run
        # is exactly when it would be forgotten.
        soniox_client.cleanup(file_id, transcription_id, base_url, api_key)

    speakers_json["txt_path"] = out_txt
    speakers_json["jsonl_path"] = out_jsonl
    speakers_json["stats_path"] = out_speakers
    return speakers_json
