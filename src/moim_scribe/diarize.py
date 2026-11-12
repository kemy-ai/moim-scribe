"""Diarization core: transcribe + separate speakers + identify against fingerprints.

The whole recording is (1) re-transcribed segment-by-segment by a whisper
backend and (2) speaker-diarized by pyannote, then the two are merged into
a "who said what, when" script. If fingerprints are enrolled, cluster-level
identification swaps SPEAKER_NN labels for real names, and a merge-detection
pass repairs the classic pyannote failure where two similar voices (e.g.
same-sex speakers) collapse into one cluster.

Every threshold below was tuned on long real-world multi-speaker meeting
recordings (20-90 minutes). They are deliberately kept as named module
constants: if you re-tune, change them here and nowhere else.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from .lang import detect_num_speakers
from .secrets import read_secret_file
from .speakers.db import SpeakerDB
from .stt import Segment, STTBackend

logger = logging.getLogger("moim_scribe.diarize")

PID = os.getpid()
# Temp filenames carry the PID so two concurrent runs over files in the same
# directory can never clobber each other's working audio (observed failure:
# the shorter file finishes first, truncates the shared temp wav, and the
# longer run crashes inside pyannote reading past the new end).

HEAD_WINDOW_SEC = 90  # window for the spoken attendee-count cue; head transcription uses the same value

# Deterministic collapse of whisper repetition loops. Decoding options alone
# reduce but do not eliminate them (temperature retries reopen the path), so
# a post-pass drops runs of identical text. Humans essentially never repeat
# the exact same sentence 3+ times verbatim; and when segment start times
# are near-perfectly uniform (machine-regular intervals like exactly 1.0s or
# 30.0s), confidence is higher still, so collapse harder.
DEDUPE_MIN_REPEAT = 3
DEDUPE_KEEP = 2
DEDUPE_UNIFORM_TOL_SEC = 0.15
DEDUPE_UNIFORM_KEEP = 1

# Repetition *inside* one segment — a separate axis from the run collapse above,
# which only sees consecutive identical segments and lets a character-level loop
# within a single segment pass straight through. One real recording carried a
# 669-character run of the same 3-letter fragment inside one segment, which then
# skewed that speaker's label, their talk share, and the summary built on top.
# Regression-checked over 12 recordings / 26,905 lines: 37 lines (0.14%) changed,
# every one of them a hallucinated run, no false positives.
INTRA_REPEAT_MIN = 8         # the unit must repeat 8+ times ("yeah yeah yeah yeah" survives)
INTRA_REPEAT_UNIT_MAX = 20   # longest repeating unit considered, so sentence-level loops count
INTRA_REPEAT_DROP_LEN = 40   # runs at least this long are dropped whole; shorter ones collapse
                             # to a single unit, which keeps real laughter and interjections

IDENTIFY_THRESHOLD = 0.6  # cosine similarity for cluster-mean identification.
# Originally measured as a clean split: true matches (cross-recording) 0.91-0.96,
# best false match of a non-enrolled voice 0.49.
# 🔴 Later re-measurement on a larger enrolment DB contradicted that split. False
# matches climbed to 0.593, then 0.637 and 0.646, while the lowest *correct*
# confident match sat at 0.632 — genuine and spurious had inverted, so no value
# of this threshold separates them any more. Raising it to stop the false ones
# also kills true ones.
# Read this as: the number is safe for a small enrolment DB and degrades as that
# DB grows, because an unenrolled voice's nearest neighbour gets closer with
# every person added. It is not a guarantee, and a score alone cannot be a
# guarantee. Narrowing the candidate set (comparing against who is actually
# present rather than everyone enrolled) is the axis that still works; this
# project does not implement it yet.
IDENTIFY_MAX_SEGMENTS = 40  # embed only the N longest ranges per cluster (efficiency)

# Merge detection & enrollment-based reassignment. When pyannote fuses two
# enrolled speakers into one cluster, cluster-mean identification can only
# name one of them; the segment-level pass below finds clusters whose
# segments split across 2+ enrolled names and re-labels the transcript.
MIN_EMB_SEC = 1.0    # trust floor for a single-segment embedding
MAX_EMB_SEC = 15.0   # trust ceiling — abnormally long segments are usually
# whisper hallucinations over silence (observed: repeated farewell phrases at
# exactly 30.0s intervals, even with backwards timestamps). They embed as
# nobody-in-particular and were the main source of misassignment, so they are
# excluded both as evidence and from reassignment.
MERGE_MIN_SHARE = 0.25      # a name "wins" a share of a cluster at >= this fraction
MERGE_MIN_TALK_SEC = 20.0   # minimum matched-speech sample before judging a merge
MERGE_DETECT_MAX_SEGMENTS = 2000  # effectively exhaustive; sampling "top 40 longest"
# made detection non-reproducible because whisper/pyannote boundaries shift
# between runs and a different 40 got sampled each time. Full sweep measured
# cheap (800-900 segments re-checked in 9-17s), so stability wins.
REASSIGN_UNKNOWN_THRESHOLD = 0.20  # below this best-similarity a segment is
# treated as "not one of the expected attendees" and its label is never
# touched (protects unenrolled/new speakers). Swept on real meetings: 0.35
# (derived from the cluster-mean threshold) was far too conservative for
# single noisy segments and under-restored the merged speaker; 0.20 matched
# an independently verified reference share almost exactly.
REASSIGN_MARGIN_MIN = 0.05  # if best-vs-second similarity margin is smaller,
# the segment is ambiguous — inherit the neighboring confident label instead.
# 🔴 Note what this margin structurally cannot say. It compares anchors against
# each other, so it can decide "which of these two" but never "neither of them".
# Together with the 0.20 floor above, the pass therefore assigns *some* anchor to
# every segment that clears the floor. Where all anchors really were in the room
# that is the intent. Where one was absent, an unenrolled attendee's speech is
# handed to that absent person's name wholesale — and the "at least 2 anchors"
# gate below does not catch it, because two anchors is precisely that case.
# Measurement has twice shown there is no fix on the score axis (see
# MERGE_WINNER_MIN_AVG_SIM). The remaining axis is knowing who attended —
# which is what the presence gate below (ANCHOR_PRESENCE_*) now checks.
REASSIGN_SMOOTH_MAX_SEC = 3.0  # absorb isolated one-offs (A B A -> A A A) under this length

# ── Anchor presence gate ─────────────────────────────────────────────────────
# Before reassignment labels are applied, every anchor must show segment-level
# evidence of actually being in this recording; if any anchor lacks it, the
# whole reassignment is withheld (existing labels kept). Rationale: the anchor
# set can contain people who were never in the room (see the log-line comment
# in detect_and_fix_merged_speakers), and the relative pass above then hands
# them someone else's speech. Dropping only the unverified anchor is NOT safer:
# if that anchor really was present, their speech gets absorbed by the
# remaining anchors — a new misattribution. Withholding fails closed either
# way: worst case is "no name attached", never "the wrong name attached".
ANCHOR_PRESENCE_MIN_SIM = 0.60  # a segment counts as presence evidence at this similarity
ANCHOR_PRESENCE_MIN_SEGS = 2  # fewer evidence segments than this = presence unconfirmed
# Upstream measurement (16 recordings x 26 enrolled, same computation and the
# same 1-15s segment population as this pass): a max-similarity threshold fails
# for the third time — lowest genuine attendee 0.498 (barely spoke) / 0.545
# (phone audio) sits BELOW the highest absentee 0.634. But the *count* axis
# separates cleanly: absentees peaked at 0-1 segments >= 0.60 (a lone chance
# hit is the ceiling; it never happened twice), while 36 of 39 attendee pairs
# had 2+. Cost measured on that corpus: 3 of 16 recordings flip to withheld
# (a barely-speaking or phone-audio attendee loses auto-naming). The line is
# an observation at 26 enrolled voices — re-measure as the enrolment DB grows,
# because an absentee's chance hits approach 2 as near-pairs accumulate.

MERGE_WINNER_MIN_AVG_SIM = 0.28  # a merge "winner" needs not just share but
# a minimum average similarity over its matched segments. Share alone can be
# stolen by a near-pair: two enrolled-but-unrelated voices measured as high
# as 0.55 mutual similarity, so noise can hand one of them 25%+ of a cluster
# and promote a non-attendee into the reassignment anchor set (local error
# becoming global contamination). Swept 0.20-0.32 on real merges: genuine
# merge parties averaged 0.32-0.35 (full-sweep averages run lower than
# longest-40 averages), non-attendees 0.22-0.30 — 0.28 was chosen to keep the
# former and screen the latter (the share filter being the first line).
# 🔴 A later incident contradicted that separation: genuine anchors measured
# 0.382-0.474 and spurious ones 0.383-0.428 — overlapping, so no threshold on
# this axis tells them apart. Treat 0.28 as a share-filter companion that
# removes obvious noise, not as a guard against a non-attendee becoming an
# anchor. That guard is now the presence gate (ANCHOR_PRESENCE_* above): a
# nominated anchor without segment-level evidence of being in the recording
# withholds the whole repair before any label is applied.


class SetupProblem(RuntimeError):
    """The setup, not the recording, is what stopped processing.

    scan_inbox treats it like SystemExit: the recording goes back where it was
    claimed from and the pass stops, so the user fixes the setup and nothing
    is lost to _error. It is an ordinary Exception on purpose: the hybrid step
    on the Soniox path absorbs every Exception from diarize_turns, and a model
    the token cannot reach must keep meaning "skip the improvement" there.
    """


# What the Hugging Face Hub raises when this token may not download a model:
# terms not accepted on a gated page, a wrong or revoked token (401), or no
# access (403). Matched by class name so huggingface_hub is never imported
# just to recognise its errors. LocalEntryNotFoundError is deliberately not
# here: it means "offline and not cached", which no token fixes.
_HUB_ACCESS_ERRORS = ("GatedRepoError", "RepositoryNotFoundError")
_HUB_HTTP_ERROR = "HfHubHTTPError"
_HUB_OFFLINE_ERROR = "LocalEntryNotFoundError"

MODEL_ACCESS_FIX = (
    "the Hugging Face token cannot download the pyannote models: run "
    "`moim-scribe doctor --online` to see which model page still needs \"Agree and "
    "access repository\" (or whether the token itself is refused; a proxy that "
    "blocks huggingface.co gives the same refusal), then try again"
)


def is_hub_access_error(exc: BaseException) -> bool:
    """Whether exc, or an exception it was raised from, is the Hub refusing the token."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        names = {k.__name__ for k in type(cur).__mro__}
        if _HUB_OFFLINE_ERROR in names:
            return False
        if names.intersection(_HUB_ACCESS_ERRORS):
            return True
        if _HUB_HTTP_ERROR in names:
            status = getattr(getattr(cur, "response", None), "status_code", None)
            if status in (401, 403):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def find_hf_token(data_dir: Path, arg_token: str | None = None) -> str | None:
    """The token, or None — for callers to whom a missing token is not fatal.

    Priority: argument > HF_TOKEN env > <data_dir>/.hf_token.

    get_hf_token below turns None into SystemExit, which is right for the local
    path (pyannote is not optional there). The hybrid step on the Soniox path
    needs the opposite: no token simply means no pyannote, and the transcript
    still ships. Splitting the lookup from the refusal is what makes that
    possible *structurally* — a caller that uses this function cannot raise
    SystemExit by accident, and SystemExit is the one exception class that slips
    past scan_inbox's `except Exception` and takes the whole watch loop with it.
    """
    if arg_token:
        return arg_token
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    return read_secret_file(data_dir / ".hf_token")


def get_hf_token(data_dir: Path, arg_token: str | None = None) -> str:
    """Resolve the Hugging Face token, or refuse to continue without one."""
    token = find_hf_token(data_dir, arg_token)
    if token:
        return token
    raise SystemExit(
        "No Hugging Face token found. Set HF_TOKEN, create "
        f"{data_dir / '.hf_token'}. See docs/setup-pyannote.md"
    )


def concat_audio(session: str) -> str:
    """Concatenate chunks/chunk_*.wav (or full_*.wav) into one working wav."""
    chunks = sorted(glob.glob(os.path.join(session, "chunks", "chunk_*.wav")))
    if not chunks:
        chunks = sorted(glob.glob(os.path.join(session, "full_*.wav")))
    if not chunks:
        raise SystemExit("no audio found (chunks/ or full_*.wav)")
    out = os.path.join(session, f"_diarize_full_{PID}.wav")
    listf = os.path.join(session, f"_concat_list_{PID}.txt")
    with open(listf, "w") as f:
        for c in chunks:
            # ffmpeg concat-list quoting: a literal single quote inside a
            # quoted path must be written as '\'' or the list parser breaks.
            quoted = os.path.abspath(c).replace("'", "'\\''")
            f.write(f"file '{quoted}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listf, "-c", "copy", out],
        check=True,
    )
    return out


def _remove_quietly(*paths: str) -> None:
    for f in paths:
        try:
            os.remove(f)
        except OSError:
            pass


def prepare_audio(inp: str, workdir: str) -> str:
    """Single audio file -> 16k mono wav; directory -> chunk concat.

    A failed ffmpeg run can leave a partial wav behind; it is removed here,
    since the caller never learns its name.
    """
    if os.path.isfile(inp):
        out = os.path.join(workdir, f"_diarize_full_{PID}.wav")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", inp, "-ar", "16000", "-ac", "1", out],
                check=True,
            )
        except BaseException:
            _remove_quietly(out)
            raise
        return out
    try:
        return concat_audio(inp)
    except BaseException:
        _remove_quietly(os.path.join(inp, f"_diarize_full_{PID}.wav"),
                        os.path.join(inp, f"_concat_list_{PID}.txt"))
        raise


def load_initial_prompt(glossary_path: Path) -> str | None:
    """Optional glossary file injected as the whisper initial_prompt.

    The prompt window is ~224 tokens; keep the file short — a natural
    sentence listing domain proper nouns works best. A broken or unreadable
    glossary must never kill the pipeline, so failures degrade to None.
    """
    if not glossary_path.exists():
        return None
    try:
        text = glossary_path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception as e:  # noqa: BLE001 — auxiliary feature, degrade silently
        logger.warning(f"glossary unreadable, continuing without prompt: {type(e).__name__}: {e}")
        return None


def dedupe_repeated_segments(
    segs: list[Segment],
    min_repeat: int = DEDUPE_MIN_REPEAT,
    keep: int = DEDUPE_KEEP,
    uniform_tol: float = DEDUPE_UNIFORM_TOL_SEC,
    uniform_keep: int = DEDUPE_UNIFORM_KEEP,
) -> tuple[list[Segment], int]:
    """Collapse whisper repetition loops. Pure function: returns a new list.

    Runs of identical text of length >= min_repeat are collapsed to the
    first `keep` occurrences; if the start-time intervals of the run are
    uniform within uniform_tol (the machine-regular signature), collapse to
    `uniform_keep`. Returns (cleaned segments, number removed).
    """
    if not segs:
        return list(segs), 0
    out: list[Segment] = []
    n_removed = 0
    i, n = 0, len(segs)
    while i < n:
        text = segs[i][2].strip()
        j = i
        while j < n and segs[j][2].strip() == text:
            j += 1
        run = segs[i:j]
        run_len = j - i
        if text and run_len >= min_repeat:
            intervals = [run[k + 1][0] - run[k][0] for k in range(run_len - 1)]
            uniform = bool(intervals) and (max(intervals) - min(intervals)) <= uniform_tol
            keep_n = min(uniform_keep if uniform else keep, run_len)
            out.extend(run[:keep_n])
            n_removed += run_len - keep_n
        else:
            out.extend(run)
        i = j
    return out, n_removed


INTRA_REPEAT_RE = re.compile(
    # Reads as: (.{1,20}?)\1{7,} — a shortest-possible unit of up to
    # INTRA_REPEAT_UNIT_MAX chars, then that same unit INTRA_REPEAT_MIN-1 more
    # times. Doubled braces are f-string escapes, not part of the pattern.
    rf"(.{{1,{INTRA_REPEAT_UNIT_MAX}}}?)\1{{{INTRA_REPEAT_MIN - 1},}}"
)


def strip_intra_repeat(text: str, drop_len: int = INTRA_REPEAT_DROP_LEN) -> str:
    """Clean a short-unit repetition loop from *within* one segment's text.

    A run at least drop_len long is treated as hallucination and dropped; a
    shorter one collapses to a single unit, since it may well be real laughter
    or an interjection. Fewer than INTRA_REPEAT_MIN repeats is left alone.
    """
    def repl(m: re.Match) -> str:
        return "" if len(m.group(0)) >= drop_len else m.group(1)

    return INTRA_REPEAT_RE.sub(repl, text).strip()


def strip_intra_repeats(segs: list[Segment]) -> tuple[list[Segment], int]:
    """Apply strip_intra_repeat across segments, dropping any left empty.

    Pure function: returns (cleaned segments, number of segments dropped).
    """
    out: list[Segment] = []
    n_dropped = 0
    for s, e, t in segs:
        c = strip_intra_repeat(t)
        if c:
            out.append((s, e, c))
        else:
            n_dropped += 1
    return out, n_dropped


def transcribe_segments(
    backend: STTBackend, audio: str, language: str, initial_prompt: str | None = None
) -> list[Segment]:
    """Backend transcription + deterministic repetition-loop collapse (two axes)."""
    segs = backend.transcribe(audio, language=language, initial_prompt=initial_prompt)
    deduped, n_removed = dedupe_repeated_segments(segs)
    if n_removed:
        logger.info(
            f"collapsed whisper repetition loops: {n_removed} segments removed "
            f"({DEDUPE_MIN_REPEAT}+ identical in a row)"
        )
    cleaned, n_intra = strip_intra_repeats(deduped)
    if n_intra:
        logger.info(f"dropped {n_intra} segments that were nothing but an in-segment repeat loop")
    return cleaned


def load_diarization_pipeline(token: str):
    """Load the pyannote pipeline, or raise SetupProblem if the Hub refuses the token.

    Separate from diarize_turns so the local path can call it BEFORE any
    transcription starts. Loaded inside the worker thread, a refused token only
    surfaced after whisper had finished the whole recording; the recording was
    then put back, and under `watch` the next pass transcribed it again, and
    again. Loading first costs a few seconds once the model is cached and makes
    a refused token cost nothing.
    """
    # pyannote.audio 4.x phones home usage metrics by default. This tool
    # processes private conversations, so telemetry is opt-in: users who want
    # to support pyannote's stats can export PYANNOTE_METRICS_ENABLED=true.
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")
    import torch
    from pyannote.audio import Pipeline

    # A token that exists but may not download a model is the likeliest
    # first-run mistake (terms not accepted on one of the three model pages),
    # and the preflight cannot see it offline. Said as a SetupProblem, it puts
    # the recording back instead of sending every one of them to _error.
    try:
        pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
    except Exception as e:
        if is_hub_access_error(e):
            raise SetupProblem(f"{MODEL_ACCESS_FIX} ({type(e).__name__})") from e
        raise
    if pipe is None:
        # Some pyannote versions print the access problem and return None.
        raise SetupProblem(MODEL_ACCESS_FIX)
    if torch.backends.mps.is_available():
        pipe.to(torch.device("mps"))
    elif torch.cuda.is_available():
        pipe.to(torch.device("cuda"))
    return pipe


def diarize_turns(
    audio: str, token: str, num_speakers: int | None, pipe=None
) -> list[tuple[float, float, str]]:
    """Run pyannote speaker diarization. num_speakers=None lets pyannote estimate.

    A fixed speaker count matters for small meetings: auto-estimation tends
    to over-split 2-person conversations into 3-4 clusters. `pipe` is a
    pipeline already loaded with load_diarization_pipeline; without it the
    pipeline is loaded here.
    """
    import numpy
    import soundfile
    import torch

    if pipe is None:
        pipe = load_diarization_pipeline(token)
    # In memory, not as a path: pyannote.audio 4 decodes paths with torchcodec,
    # which needs FFmpeg's shared libraries, and the usual Windows FFmpeg build
    # is static. `audio` is always the 16 kHz mono wav from prepare_audio, so
    # soundfile reads it; the turns measured identical to the path call.
    samples, sample_rate = soundfile.read(audio, dtype="float32", always_2d=True)
    wav = {
        "waveform": torch.from_numpy(numpy.ascontiguousarray(samples.T)),  # (channel, time)
        "sample_rate": sample_rate,
    }
    out = pipe(wav, num_speakers=num_speakers) if num_speakers else pipe(wav)
    # pyannote 4.x: DiarizeOutput.speaker_diarization is the Annotation
    ann = out.speaker_diarization if hasattr(out, "speaker_diarization") else out
    return [(t.start, t.end, spk) for t, _, spk in ann.itertracks(yield_label=True)]


def speaker_at(turns: list[tuple[float, float, str]], t: float) -> str:
    """Speaker label containing (or nearest to) time t."""
    for s, e, spk in turns:
        if s <= t <= e:
            return spk
    best, bestd = "UNKNOWN", 1e9
    for s, e, spk in turns:
        d = min(abs(t - s), abs(t - e))
        if d < bestd:
            best, bestd = spk, d
    return best


def relabel_by_turns(
    merged: list[dict], turns: list[tuple[float, float, str]]
) -> tuple[list[dict], dict]:
    """Replace segment speaker labels with the boundaries from a diarizer.

    This is the hybrid path: **text from one engine, speaker boundaries from
    another**. It exists because the two engines fail in opposite directions,
    which one recording made unambiguous — the same 5-speaker meeting, run both
    ways:

        cloud STT : 56k characters, proper nouns intact / 5 speakers collapsed
                    into 2-3 blocks
        pyannote  : all 5 speakers separated correctly  / proper nouns lost

    A second recording reproduced it. Using either engine alone gives up one of
    those columns every time; running both and taking the better half of each
    gives up neither.

    Mapping is by **maximum overlap**, not by looking up the speaker at each
    segment's start time. A segment is an interval, and its edges never line up
    with turn edges, so a start-time lookup hands every boundary-straddling
    segment to whoever was speaking just before it. Segments that overlap no
    turn at all (they fall in a gap between turns) keep the label they arrived
    with — inventing one would be worse than leaving it unclassified.

    Returns (new list, stats). The input is not mutated.
    """
    if not turns:
        return merged, {"n_relabeled": 0, "n_kept": len(merged), "n_turn_speakers": 0}
    # Sort here rather than documenting an assumption. The `break` below is only
    # correct on time-ordered turns, and that ordering is an implementation
    # detail of pyannote's itertracks. Out of order, the loop would quietly map
    # some segments and skip others, and the symptom — "speakers are sometimes
    # wrong" — points nowhere near this line. One sort of a few thousand turns
    # costs nothing.
    turns = sorted(turns, key=lambda x: (x[0], x[1]))
    out, n_relabeled, n_kept = [], 0, 0
    for m in merged:
        s, e = m["start"], m["end"]
        best_spk, best_ov = None, 0.0
        for ts, te, spk in turns:
            if te <= s:
                continue
            if ts >= e:
                break  # turns are time-ordered; nothing later can overlap
            ov = min(e, te) - max(s, ts)
            if ov > best_ov:
                best_spk, best_ov = spk, ov
        if best_spk is None:
            n_kept += 1
            out.append(m)
        else:
            n_relabeled += 1
            out.append({**m, "speaker": best_spk})
    return out, {
        "n_relabeled": n_relabeled,
        "n_kept": n_kept,
        "n_turn_speakers": len({spk for _, _, spk in turns}),
    }


def identify_speakers(
    wav, sr: int, merged: list[dict], db_embeddings: dict, model_dir: Path,
    threshold: float = IDENTIFY_THRESHOLD,
) -> tuple[list[dict], dict, dict]:
    """Replace SPEAKER_NN labels with enrolled names via cluster-mean matching.

    wav/db_embeddings are loaded once by the caller and shared with the
    merge-detection pass (avoids duplicate disk I/O). Returns
    (relabeled merged, {SPEAKER_NN: name}, {SPEAKER_NN: [(name, score), ...]}).
    """
    from .speakers.embed import cosine_similarity, extract_embedding_from_segments

    if not db_embeddings:
        logger.info("fingerprint DB empty, skipping identification (labels stay SPEAKER_NN)")
        return merged, {}, {}

    by_speaker: dict[str, list[tuple[float, float]]] = {}
    for m in merged:
        by_speaker.setdefault(m["speaker"], []).append((m["start"], m["end"]))

    logger.info(
        # Count, not the roll call. These are the names of everyone whose voice
        # is enrolled, most of whom have nothing to do with this recording, and
        # a log file outlives the run and gets pasted into bug reports.
        f"identifying speakers... ({len(db_embeddings)} enrolled)"
    )

    mapping: dict[str, str] = {}
    sims_log: dict[str, list] = {}
    for spk, segments in by_speaker.items():
        emb, used_sec = extract_embedding_from_segments(
            wav, sr, segments, model_dir, max_segments=IDENTIFY_MAX_SEGMENTS
        )
        if emb is None:
            continue
        scored = sorted(
            ((name, cosine_similarity(emb, e)) for name, e in db_embeddings.items()),
            key=lambda kv: -kv[1],
        )
        best_name, best_score = scored[0]
        sims_log[spk] = scored
        if best_score >= threshold:
            mapping[spk] = best_name
            logger.info(
                f"  {spk} -> {best_name} (similarity {best_score:.3f} >= {threshold}, "
                f"{used_sec:.1f}s basis)"
            )
        else:
            logger.info(
                f"  {spk} -> no match (best {best_score:.3f} < {threshold}, label kept)"
            )

    if not mapping:
        return merged, mapping, sims_log

    for m in merged:
        if m["speaker"] in mapping:
            m["speaker"] = mapping[m["speaker"]]
    return merged, mapping, sims_log


def _segment_sims(
    wav, sr: int, start: float, end: float, anchors: dict, model_dir: Path,
) -> dict | None:
    """Embed a single range and return its similarity to every anchor.

    The computation half of _segment_identify, split out because the presence
    gate needs the full similarity dict, not just the winner. Called thousands
    of times per recording, so it does zero disk I/O: anchors are passed in,
    audio is already in memory. Returns None when embedding fails.
    """
    from .speakers.embed import cosine_similarity, extract_embedding_from_segments

    emb, _used = extract_embedding_from_segments(wav, sr, [(start, end)], model_dir)
    if emb is None:
        return None
    return {name: cosine_similarity(emb, e) for name, e in anchors.items()}


def _pick_best(sims_d: dict, unknown_threshold: float) -> tuple[str | None, float, float]:
    """(best name or None below threshold, best score, margin best-vs-second)."""
    sims = sorted(sims_d.items(), key=lambda kv: -kv[1])
    best_name, best_score = sims[0]
    margin = best_score - (sims[1][1] if len(sims) > 1 else -1.0)
    if best_score < unknown_threshold:
        return None, best_score, margin
    return best_name, best_score, margin


def _segment_identify(
    wav, sr: int, start: float, end: float, anchors: dict, unknown_threshold: float,
    model_dir: Path,
) -> tuple[str | None, float, float]:
    """Embed a single range and argmax against pre-loaded anchor embeddings.

    _segment_sims + _pick_best, same signature and behavior as before the
    split (detect_cluster_merges still calls this form).
    """
    sims_d = _segment_sims(wav, sr, start, end, anchors, model_dir)
    if sims_d is None:
        return None, 0.0, 0.0
    return _pick_best(sims_d, unknown_threshold)


def detect_cluster_merges(
    wav, sr: int, merged: list[dict], raw_labels: list[str], db: dict, model_dir: Path,
    min_share: float = MERGE_MIN_SHARE,
    min_talk_sec: float = MERGE_MIN_TALK_SEC,
    unknown_threshold: float = REASSIGN_UNKNOWN_THRESHOLD,
    max_sample_segments: int = MERGE_DETECT_MAX_SEGMENTS,
    min_avg_sim: float = MERGE_WINNER_MIN_AVG_SIM,
) -> dict:
    """Find pyannote clusters whose segments split across 2+ enrolled names.

    Cluster-mean identification cannot see a merge (one mean, one name), so
    this pass checks every eligible segment of every cluster individually.
    A name "wins" a cluster share only with BOTH enough share (min_share)
    and enough average similarity (min_avg_sim) — the AND guards against
    near-pair contamination described at MERGE_WINNER_MIN_AVG_SIM.

    Returns {raw_label: {"talk": {name: sec}, "winners": [...], "avg_sim": {...}}}
    for suspect clusters only.
    """
    by_cluster: dict[str, list[dict]] = {}
    for m, raw in zip(merged, raw_labels):
        by_cluster.setdefault(raw, []).append(m)

    suspects: dict = {}
    for raw, segs in by_cluster.items():
        cand = [s for s in segs if MIN_EMB_SEC <= (s["end"] - s["start"]) <= MAX_EMB_SEC]
        if len(cand) > max_sample_segments:
            cand.sort(key=lambda s: s["end"] - s["start"], reverse=True)
            cand = cand[:max_sample_segments]
        talk: Counter = Counter()
        score_sum: Counter = Counter()
        score_n: Counter = Counter()
        for s in cand:
            name, score, _margin = _segment_identify(
                wav, sr, s["start"], s["end"], db, unknown_threshold, model_dir
            )
            if name:
                talk[name] += s["end"] - s["start"]
                score_sum[name] += score
                score_n[name] += 1
        total = sum(talk.values())
        if total < min_talk_sec:
            continue
        avg_sim = {n: score_sum[n] / score_n[n] for n in talk}
        winners = [n for n, v in talk.items() if v / total >= min_share and avg_sim[n] >= min_avg_sim]
        if len(winners) >= 2:
            suspects[raw] = {
                "talk": dict(talk),
                "winners": winners,
                "avg_sim": {n: round(v, 3) for n, v in avg_sim.items()},
            }
    return suspects


def reassign_with_anchors(
    wav, sr: int, merged: list[dict], anchors: dict, model_dir: Path,
    unknown_threshold: float = REASSIGN_UNKNOWN_THRESHOLD,
    margin_min: float = REASSIGN_MARGIN_MIN,
    smooth_max_sec: float = REASSIGN_SMOOTH_MAX_SEC,
) -> tuple[list[str], dict]:
    """Re-label every segment by argmax over the attendee anchor set.

    Segments below unknown_threshold (not an expected attendee) and
    segments longer than MAX_EMB_SEC (hallucination suspects) keep their
    existing label untouched. Ambiguous segments (margin below margin_min)
    inherit the nearest confident neighbor; isolated one-offs shorter than
    smooth_max_sec sandwiched between the same speaker are absorbed —
    but never across kept (protected) segments.

    Returns (new labels, stats).
    """
    n = len(merged)
    assigned: list[str | None] = [None] * n
    keep = [False] * n  # True = protected: unenrolled or too-long, label untouched
    n_conf = n_keep_unregistered = n_keep_toolong = 0
    # Presence evidence per anchor: segments at >= ANCHOR_PRESENCE_MIN_SIM,
    # counted regardless of rank. Two similar attendees can leave one of them
    # permanently in second place; dropping runner-up evidence would make this
    # a different statistic from the one the separation line was measured on.
    presence: Counter = Counter()

    for i, s in enumerate(merged):
        dur = s["end"] - s["start"]
        if dur < MIN_EMB_SEC:
            continue  # too short — filled by inheritance below
        if dur > MAX_EMB_SEC:
            keep[i] = True
            n_keep_toolong += 1
            continue
        sims_d = _segment_sims(wav, sr, s["start"], s["end"], anchors, model_dir)
        if sims_d is None:
            name, margin = None, 0.0
        else:
            for n2, v in sims_d.items():
                if v >= ANCHOR_PRESENCE_MIN_SIM:
                    presence[n2] += 1
            name, _score, margin = _pick_best(sims_d, unknown_threshold)
        if name is None:
            keep[i] = True
            n_keep_unregistered += 1
            continue
        if margin >= margin_min:
            assigned[i] = name
            n_conf += 1
        # margin too small: leave None, inheritance fills it

    last = None
    for i in range(n):
        if keep[i]:
            continue
        if assigned[i] is None:
            assigned[i] = last
        else:
            last = assigned[i]
    nxt = None
    for i in range(n - 1, -1, -1):
        if keep[i]:
            continue
        if assigned[i] is None:
            assigned[i] = nxt
        else:
            nxt = assigned[i]

    n_smooth = 0
    for i in range(1, n - 1):
        if keep[i - 1] or keep[i] or keep[i + 1]:
            continue
        if (
            assigned[i - 1] is not None
            and assigned[i - 1] == assigned[i + 1]
            and assigned[i] != assigned[i - 1]
            and (merged[i]["end"] - merged[i]["start"]) < smooth_max_sec
        ):
            assigned[i] = assigned[i - 1]
            n_smooth += 1

    new_labels = []
    for i, s in enumerate(merged):
        if keep[i] or assigned[i] is None:
            new_labels.append(s["speaker"])
        else:
            new_labels.append(assigned[i])

    stats = {
        "n_total": n,
        "n_confident": n_conf,
        "n_keep_unregistered": n_keep_unregistered,
        "n_keep_toolong": n_keep_toolong,
        "n_smooth": n_smooth,
        # Per-anchor presence-evidence counts — the caller's withhold decision
        # and the post-hoc diagnosis record. Anchors with zero evidence are
        # listed as 0 explicitly (absence must not be silent).
        "anchor_presence": {name: int(presence.get(name, 0)) for name in anchors},
    }
    return new_labels, stats


def detect_and_fix_merged_speakers(
    wav, sr: int, merged: list[dict], raw_labels: list[str], id_mapping: dict, db: dict,
    model_dir: Path,
) -> tuple[list[dict], dict]:
    """Merge detection + (when triggered) enrollment-based reassignment.

    Pure in effect until the very end: all computation happens first, and
    merged is only mutated once the new labels are fully decided, so a
    partial failure leaves the original labels intact (the caller also
    wraps this in try/except — the pipeline must always finish).

    Returns (merged, info) with info = {triggered, suspects, anchors, stats}
    plus presence_unconfirmed (anchor names) when the presence gate withheld
    the reassignment — triggered stays False and labels are untouched then.
    """
    info: dict = {"triggered": False, "suspects": {}, "anchors": [], "stats": {}}

    if len(db) < 2 or len(set(raw_labels)) < 2:
        return merged, info  # a merge needs 2+ enrolled and 2+ clusters

    suspects = detect_cluster_merges(wav, sr, merged, raw_labels, db, model_dir)
    info["suspects"] = {
        raw: {
            "talk": {n: round(v, 1) for n, v in d["talk"].items()},
            "winners": d["winners"],
            "avg_sim": d["avg_sim"],
        }
        for raw, d in suspects.items()
    }
    if not suspects:
        return merged, info

    for raw, d in suspects.items():
        who = ", ".join(
            f"{n}({v:.0f}s, sim {d['avg_sim'].get(n, 0):.2f})"
            for n, v in sorted(d["talk"].items(), key=lambda kv: -kv[1])
        )
        logger.info(
            f"merge suspect: {raw} (identified as {id_mapping.get(raw, raw)}) "
            f"splits into {who} -> anchor candidates {d['winners']}"
        )

    anchor_names = set(id_mapping.values())
    for d in suspects.values():
        anchor_names |= set(d["winners"])
    anchor_names &= set(db.keys())
    if len(anchor_names) < 2:
        logger.info("merge suspected but fewer than 2 anchors, keeping labels")
        return merged, info

    info["anchors"] = sorted(anchor_names)
    logger.info(
        # Count, not names. The anchor set is assembled by comparing against the
        # whole enrolment DB, so it can list people who were never in this
        # recording — the worst names to leave sitting in a log.
        f"enrollment reassignment triggered, {len(info['anchors'])} anchors "
        f"(re-checking all {len(merged)} segments)"
    )

    anchors = {n: db[n] for n in anchor_names}
    before: Counter = Counter()
    for m in merged:
        before[m["speaker"]] += m["end"] - m["start"]

    new_labels, stats = reassign_with_anchors(wav, sr, merged, anchors, model_dir)
    info["stats"] = stats

    # Presence gate: check BEFORE the labels are applied. If any anchor lacks
    # segment-level evidence of being in this recording, withhold the whole
    # reassignment (see the ANCHOR_PRESENCE_* comment for why withholding, not
    # dropping the anchor). Unconfirmed does not mean absent — a barely-speaking
    # or phone-audio attendee looks the same; they just keep their raw label.
    presence = stats.get("anchor_presence", {})
    unconfirmed = sorted(
        n for n in anchor_names if presence.get(n, 0) < ANCHOR_PRESENCE_MIN_SEGS
    )
    if unconfirmed:
        info["presence_unconfirmed"] = unconfirmed
        logger.info(
            # Count, not names, for the same reason as the trigger line above.
            f"presence unconfirmed for {len(unconfirmed)}/{len(anchor_names)} anchors "
            f"(fewer than {ANCHOR_PRESENCE_MIN_SEGS} segments at >= "
            f"{ANCHOR_PRESENCE_MIN_SIM} similarity), reassignment withheld, labels kept"
        )
        return merged, info

    info["triggered"] = True
    for m, new in zip(merged, new_labels):
        m["speaker"] = new

    after: Counter = Counter()
    for m in merged:
        after[m["speaker"]] += m["end"] - m["start"]
    tot_b, tot_a = sum(before.values()) or 1, sum(after.values()) or 1
    logger.info("talk share before/after reassignment:")
    for name in sorted(set(before) | set(after), key=lambda n: -after.get(n, 0)):
        logger.info(
            f"  {name}: {before.get(name, 0) / tot_b * 100:4.0f}% -> "
            f"{after.get(name, 0) / tot_a * 100:4.0f}%"
        )
    logger.info(
        f"reassignment stats: {stats['n_confident']} confident, "
        f"{stats['n_keep_unregistered']} kept (unenrolled), "
        f"{stats['n_keep_toolong']} kept (too long), {stats['n_smooth']} smoothed "
        f"(of {stats['n_total']})"
    )

    return merged, info


# Leftover noise labels. A label that is a SPEAKER_NN cluster, matched nobody,
# and holds at most this much speech in total is counted as noise, not as a
# person. Typical source: the hybrid step leaves segments that fall in a gap
# between pyannote turns with the label they arrived with, so a fragment of
# 0.2-0.6 s can surface as its own "speaker" and turn a two-person call into
# "four attendees". The transcript keeps the label; only the counting changes.
# 1.0 s is a judgement, not a measured break: upstream, unidentified leftover
# labels were spread smoothly from under a second to over thirty, with no gap
# to cut at. 1.0 covers the observed noise fragments while being the value
# least likely to drop a real person who answered with one short word.
LEFTOVER_NOISE_MAX_SEC = 1.0
_SPEAKER_NN_RE = re.compile(r"SPEAKER_\d+")


def _mark_leftover_noise(speakers_data: list) -> int:
    """Flag leftover noise labels with leftover_noise=True; return how many.

    A label qualifies only when all three hold: it is a SPEAKER_NN label (a
    real name is never noise), it was not identified, and its total speech is
    <= LEFTOVER_NOISE_MAX_SEC. Mutates the entries it flags.
    """
    n = 0
    for e in speakers_data:
        if (_SPEAKER_NN_RE.fullmatch(str(e.get("label", "")))
                and not e.get("identified")
                and float(e.get("talk_sec") or 0.0) <= LEFTOVER_NOISE_MAX_SEC):
            e["leftover_noise"] = True
            n += 1
    return n


def run_mixed_check(
    wav, sr: int, merged: list[dict], raw_labels: list[str], id_mapping: dict,
    db: dict, model_dir: Path, reassigned: bool = False,
) -> dict | None:
    """Two-voice suspicion on raw clusters (speakers.mixed). Display only.

    Shared by both engines through identify_and_repair. Never changes a label
    and never raises: any failure is logged and returns None, so a broken
    display step ends as "no display" and the run carries on. Set
    MOIM_SCRIBE_MIXED_CHECK=0 to skip it (it costs one embedding per long
    block).
    """
    if os.environ.get("MOIM_SCRIBE_MIXED_CHECK") == "0":
        logger.info("two-voice check skipped (MOIM_SCRIBE_MIXED_CHECK=0)")
        return None
    if wav is None or not merged:
        logger.info("two-voice check skipped - no audio loaded (identification did not run)")
        return None
    try:
        from .speakers import mixed
        from .speakers.embed import extract_embedding_from_segments

        def embed_fn(w, rate, segs):
            return extract_embedding_from_segments(w, rate, segs, model_dir)

        t = time.time()
        info = mixed.check_mixed_clusters(wav, sr, merged, raw_labels, embed_fn, id_mapping, db)
        info["reassigned"] = bool(reassigned)
        for line in mixed.format_suspect_lines(info):
            logger.info(line)
        logger.info(f"  two-voice check took {time.time() - t:.0f}s")
        return info
    except (Exception, SystemExit) as e:  # noqa: BLE001 - display step, never fatal
        # SystemExit too: on the cloud path it would escape the watch loop's
        # Exception handler and stop the service over a diagnostic.
        logger.warning(f"two-voice check failed (display only, run continues): "
                       f"{type(e).__name__}: {e}")
        return None


def write_outputs(
    session: str,
    prefix: str,
    merged: list[dict],
    id_mapping: dict,
    id_sims: dict,
    merge_info: dict,
    mixed_info: dict | None = None,
) -> tuple[str, str, str, dict]:
    """Write the three-file output contract and return it.

    The single definition of the output schema: every engine funnels through
    here and nothing else writes these files. Deliberately free of run-specific
    state (timings, temp-file cleanup) so it can be driven from a plain fixture
    with neither whisper nor pyannote installed.

    Returns (jsonl_path, txt_path, speakers_path, speakers_json). The caller
    attaches the *_path keys onto that dict; this function does not, so the
    schema it owns stays free of local filesystem paths — the dict travels into
    summarizer prompts, and a home directory is user-identifying.

    mixed_info is the two-voice check result (run_mixed_check) or None. It is
    written as the diagnostic block `mixed_check_diag` and as a per-speaker
    `mixed_suspect_diag` flag; speaker_stats keeps both out of prompts.
    """
    talk: Counter = Counter()
    for m in merged:
        talk[m["speaker"]] += m["end"] - m["start"]

    out_jsonl = os.path.join(session, prefix + "transcript_diarized.jsonl")
    out_txt = os.path.join(session, prefix + "transcript_diarized.txt")
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for m in merged:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(out_txt, "w", encoding="utf-8") as f:
        for m in merged:
            f.write(f"[{m['start']:>6.1f}s] {m['speaker']}: {m['text']}\n")

    total_talk = sum(talk.values()) or 1.0
    raw_of = {v: k for k, v in id_mapping.items()}
    # "identified" = the final label is a real enrolled name. After a merge
    # reassignment, a speaker can carry a real name via the anchor set even
    # though cluster-mean matching (id_mapping) never saw them.
    identified_names = set(id_mapping.values()) | set(merge_info.get("anchors", []))
    mixed_suspects = set((mixed_info or {}).get("suspects") or {})
    speakers_data = []
    for spk, t_sec in talk.most_common():
        entry = {
            "label": spk,
            "talk_sec": round(t_sec, 1),
            "share": round(t_sec / total_talk, 2),
            "identified": spk in identified_names,
        }
        raw_label = raw_of.get(spk)
        if raw_label:
            entry["raw_speaker"] = raw_label
            scored = id_sims.get(raw_label, [])
            if scored:
                entry["match_score"] = round(scored[0][1], 3)
        # Two-voice suspicion (display only): this label's raw cluster split
        # into two voices. Diagnostic - a summarizer that saw it would start
        # second-guessing names on its own.
        if spk in mixed_suspects or (raw_label and raw_label in mixed_suspects):
            entry["mixed_suspect_diag"] = True
        speakers_data.append(entry)

    n_noise = _mark_leftover_noise(speakers_data)
    speakers_json: dict = {
        # People, not labels: leftover noise labels are not counted (see
        # _mark_leftover_noise). Identical to the label count when there is no
        # noise; when there is, n_labels_total keeps the list length.
        "n_speakers": len(speakers_data) - n_noise,
        "speakers": speakers_data,
    }
    if n_noise:
        speakers_json["n_labels_total"] = len(speakers_data)
    if mixed_info:
        speakers_json["mixed_check_diag"] = mixed_info
    if merge_info.get("triggered") or merge_info.get("presence_unconfirmed"):
        # An allow-list, not a passthrough: putting a field on merge_info does
        # NOT surface it here. Adding one means editing this dict too, and then
        # deciding whether it belongs in a prompt — "anchors" and "suspects" are
        # built by comparing against the whole enrolment DB, so they can name
        # people who were not in the room. tests/test_diarize_outputs.py pins
        # the key set at all three levels (top, speakers[], merge_reassign) so
        # neither an omission nor an unreviewed addition can be silent.
        # The block is also written when the presence gate withheld the pass
        # (triggered stays False) — that is the record of WHY nothing was
        # renamed, which would otherwise vanish.
        speakers_json["merge_reassign"] = {
            # Stated rather than implied by the block's presence: this is the one
            # field here a summarizer prompt may see (speaker_stats keeps it and
            # drops the rest), and audit tooling filters on it.
            "triggered": bool(merge_info.get("triggered")),
            "method": f"enrollment_reassign(ECAPA argmax, margin>={REASSIGN_MARGIN_MIN})",
            "anchors": merge_info["anchors"],
            "suspects": merge_info["suspects"],
            "stats": merge_info["stats"],
            # Withhold reason — names on purpose: this is the post-hoc diagnosis
            # record ("why did nobody get named?"), and speaker_stats drops
            # everything in this block except "triggered" before any prompt.
            "presence_unconfirmed": merge_info.get("presence_unconfirmed", []),
        }
    out_speakers = os.path.join(session, prefix + "speakers.json")
    with open(out_speakers, "w", encoding="utf-8") as f:
        json.dump(speakers_json, f, ensure_ascii=False, indent=2)
    logger.info(f"speakers.json -> {out_speakers}")
    return out_jsonl, out_txt, out_speakers, speakers_json


def identify_and_repair(
    audio: str,
    merged: list[dict],
    raw_labels: list[str],
    speaker_db: SpeakerDB,
    model_dir: Path,
    identify: bool = True,
) -> tuple[list[dict], dict, dict, dict]:
    """Put enrolled names on cluster labels, then repair clusters that merged
    two enrolled people into one.

    Extracted so both diarization paths share it: the local one arrives here
    with pyannote's SPEAKER_NN labels, the cloud one with the provider's own
    speaker ids. Neither knows who those labels *are* — that is this function's
    job, and it depends on nothing about how the labels were produced.

    Every failure here is absorbed. Transcription is the paid, unrepeatable
    part of the work; naming is an enhancement on top of it, so a broken
    fingerprint DB or a missing model must degrade to SPEAKER_NN rather than
    lose the transcript.

    Returns (merged, id_mapping, id_sims, merge_info) — merged is mutated in
    place by the callees and also returned, matching the previous inline flow.
    """
    id_mapping: dict = {}
    id_sims: dict = {}
    merge_info: dict = {"triggered": False, "suspects": {}, "anchors": [], "stats": {}}
    if not identify:
        logger.info("identification disabled (merge detection skipped too)")
        return merged, id_mapping, id_sims, merge_info

    from .speakers.embed import load_wav_mono16k

    wav = sr = None
    t3 = time.time()
    db = speaker_db.load_all_embeddings()
    if not db:
        logger.info("fingerprint DB empty, skipping identification")
    else:
        try:
            # Identification failing must never kill transcription — absorb
            # and continue with SPEAKER_NN labels.
            wav = load_wav_mono16k(audio)
            sr = 16000
            merged, id_mapping, id_sims = identify_speakers(wav, sr, merged, db, model_dir)
        except Exception as e:  # noqa: BLE001 — pipeline must finish
            logger.warning(f"identification failed, keeping SPEAKER_NN labels: {type(e).__name__}: {e}")
    logger.info(f"  identification took {time.time() - t3:.0f}s")

    if id_mapping:
        t4 = time.time()
        try:
            merged, merge_info = detect_and_fix_merged_speakers(
                wav, sr, merged, raw_labels, id_mapping, db, model_dir
            )
        except Exception as e:  # noqa: BLE001 — pipeline must finish
            logger.warning(f"merge detection failed, keeping labels: {type(e).__name__}: {e}")
        status = "merge detection (triggered)" if merge_info.get("triggered") else "merge detection (clean)"
        logger.info(f"  {status} took {time.time() - t4:.0f}s")

    # After naming and repair, on the raw clusters. Carried on merge_info under
    # "mixed_check" so the return shape stays the same; write_outputs never
    # copies it into merge_reassign (that block is an allow-list) - callers
    # pass it explicitly as mixed_info.
    merge_info["mixed_check"] = run_mixed_check(
        wav, sr, merged, raw_labels, id_mapping, db, model_dir,
        reassigned=bool(merge_info.get("triggered")),
    )
    return merged, id_mapping, id_sims, merge_info


def run_diarization(
    input_path: str,
    stt_backend: STTBackend,
    language: str,
    speaker_db: SpeakerDB,
    model_dir: Path,
    hf_token: str,
    glossary_path: Path | None = None,
    num_speakers: int | None = None,
    default_speakers: int | None = None,
    identify: bool = True,
    keep_audio: bool = False,
) -> dict:
    """Full diarization for one file or chunk directory.

    Outputs next to the input (file: <stem>_ prefix; directory: inside it):
      <prefix>transcript_diarized.jsonl   segment records
      <prefix>transcript_diarized.txt     "[  12.3s] Name: text" script
      <prefix>speakers.json               per-speaker stats summary

    Speaker-count priority: num_speakers argument > spoken cue in the first
    90 seconds ("참석자 다섯 명" / "five participants") > default_speakers
    (None = let pyannote estimate). Returns the speakers.json dict with an
    extra "txt_path"/"jsonl_path" for callers, and "speaker_count_source"
    ("given" / "spoken" / "default" / "estimated") with "speaker_count" (the
    count used, None when estimated) so the summary can say where the
    attendee count came from. Those two are not written to speakers.json.
    """
    if os.path.isfile(input_path):
        session = os.path.dirname(os.path.abspath(input_path)) or "."
        prefix = os.path.splitext(os.path.basename(input_path))[0] + "_"
    else:
        session = input_path
        prefix = ""

    t0 = time.time()
    logger.info("preparing audio...")
    audio = prepare_audio(input_path, session)
    logger.info(f"  audio ready in {time.time() - t0:.0f}s")

    # The working wav is a full decoded copy of the recording: it must not
    # outlive this call whether the run succeeds or raises.
    try:
        # Before the head clip and before whisper: see load_diarization_pipeline.
        pipe = load_diarization_pipeline(hf_token)

        initial_prompt = load_initial_prompt(glossary_path) if glossary_path else None
        if initial_prompt:
            logger.info(f"glossary prompt active ({len(initial_prompt)} chars)")

        if num_speakers is not None:
            num_spk = num_speakers or None  # 0 -> None (pyannote auto)
            count_source = "given" if num_spk else "estimated"
            logger.info(f"speaker count: {num_spk or 'auto'} (explicit)")
        else:
            th = time.time()
            head_wav = os.path.join(session, f"_diarize_head_{PID}.wav")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", audio,
                     "-t", str(HEAD_WINDOW_SEC), "-c", "copy", head_wav],
                    check=True,
                )
                head_segs = transcribe_segments(stt_backend, head_wav, language, initial_prompt)
            finally:
                try:
                    os.remove(head_wav)
                except OSError:
                    pass
            detected = detect_num_speakers(language, head_segs, window_sec=HEAD_WINDOW_SEC)
            logger.info(f"  head transcription for spoken cue took {time.time() - th:.0f}s")
            if detected:
                num_spk = detected
                count_source = "spoken"
                # The file name beats the spoken cue, and the cue beats
                # default_speakers, so only the file name can correct it.
                example = "회의 4명" if language == "ko" else "meeting 4p"
                logger.info(
                    f"speaker count: {num_spk} (spoken cue; if that is wrong, put the count "
                    f"in the file name, e.g. \"{example}\", and process it again)"
                )
            else:
                num_spk = default_speakers or None
                count_source = "default" if num_spk else "estimated"
                if num_spk:
                    logger.info(f"no spoken cue -> default_speakers {num_spk}")
                else:
                    # Measured on a real 27-minute recording of three people:
                    # estimation produced seven labels. Not an error, but the
                    # user should hear it once per recording, and how to avoid it.
                    example = "회의 4명" if language == "ko" else "meeting 4p"
                    logger.warning(
                        "no speaker count in the file name and none spoken: estimating. "
                        "Estimation often splits one person into several labels. For a "
                        f"reliable result put the count in the file name, e.g. \"{example}\""
                    )

        # whisper and pyannote are independent until the merge step: run them in
        # parallel. They share the GPU, so each is slower than solo, but the total
        # beats sequential.
        t1 = time.time()
        logger.info(f"transcription + diarization starting (speakers={num_spk or 'auto'})")
        from concurrent.futures import ThreadPoolExecutor

        def _timed_diarize():
            td = time.time()
            r = diarize_turns(audio, hf_token, num_speakers=num_spk, pipe=pipe)
            logger.info(f"  pyannote took {time.time() - td:.0f}s (inside parallel window)")
            return r

        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_timed_diarize)
        try:
            segs = transcribe_segments(stt_backend, audio, language, initial_prompt)
            logger.info(f"  whisper: {len(segs)} segments ({time.time() - t1:.0f}s)")
            turns = fut.result()
        except Exception:
            # If whisper fails, don't wait for pyannote: propagate fast so the
            # caller can quarantine the file.
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        ex.shutdown(wait=True)
        n_clusters = len({s for *_, s in turns})
        logger.info(f"  pyannote: {n_clusters} speaker clusters ({time.time() - t1:.0f}s parallel window)")

        merged = []
        for st, en, txt in segs:
            spk = speaker_at(turns, (st + en) / 2)
            merged.append({"start": round(st, 1), "end": round(en, 1), "speaker": spk, "text": txt})
        # Snapshot raw cluster labels (index-aligned with merged) BEFORE renaming,
        # so merge detection still knows each segment's original cluster.
        raw_labels = [m["speaker"] for m in merged]

        merged, id_mapping, id_sims, merge_info = identify_and_repair(
            audio, merged, raw_labels, speaker_db, model_dir, identify
        )

        out_jsonl, out_txt, out_speakers, speakers_json = write_outputs(
            session, prefix, merged, id_mapping, id_sims, merge_info,
            mixed_info=merge_info.get("mixed_check"),
        )
        logger.info(f"done -> {out_txt} ({time.time() - t0:.0f}s total)")
    finally:
        if not keep_audio:
            _remove_quietly(audio, os.path.join(session, f"_concat_list_{PID}.txt"))

    speakers_json["txt_path"] = out_txt
    speakers_json["jsonl_path"] = out_jsonl
    speakers_json["stats_path"] = out_speakers
    speakers_json["speaker_count_source"] = count_source
    speakers_json["speaker_count"] = num_spk
    return speakers_json
