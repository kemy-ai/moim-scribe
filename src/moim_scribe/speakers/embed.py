"""Voice-fingerprint embedding extraction shared by enrollment and identification.

Uses speechbrain ECAPA-TDNN (spkrec-ecapa-voxceleb, 192-d, not gated on
Hugging Face). Once the model is cached locally, inference runs fully
offline: voice fingerprints are biometric data, so no network call should
be needed (or made) to process them.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import numpy as np

logger = logging.getLogger("moim_scribe.speakers.embed")

MIN_SEGMENT_SEC = 1.0  # segments shorter than this yield unreliable embeddings

_model = None
_model_dir: Path | None = None


def _device() -> str:
    # speechbrain has spotty support for non-CPU devices on some platforms and
    # these are short batches — CPU is fast enough and works everywhere.
    return "cpu"


def get_model(model_dir: Path):
    """Load the EncoderClassifier singleton (downloads to model_dir on first use).

    If the cache already exists, force offline mode (HF_HUB_OFFLINE) so that
    processing recordings never touches the network.
    """
    global _model, _model_dir
    if _model is not None and _model_dir == model_dir:
        return _model
    if model_dir.exists() and any(not _is_temp_copy(p) for p in model_dir.iterdir()):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch  # noqa: F401  (ensures a clear error if torch is missing)
    from speechbrain.inference.speaker import EncoderClassifier

    logger.info("loading ECAPA-TDNN speaker embedding model…")
    _model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(model_dir),
        run_opts={"device": _device()},
    )
    materialize_symlinks(model_dir)
    _model_dir = model_dir
    return _model


# materialize_symlinks writes each copy under "<name><TEMP_COPY_MARK><pid>-<random>"
# first. A run killed mid-copy can leave one behind; it is never a model file.
TEMP_COPY_MARK = ".copying-"


def _is_temp_copy(path: Path) -> bool:
    return TEMP_COPY_MARK in path.name or path.name.endswith(".copying")


def materialize_symlinks(model_dir: Path) -> int:
    """Replace symlinks in model_dir with real copies. Returns how many.

    speechbrain "saves" the model into the workspace as symlinks into the
    Hugging Face cache. The workspace then only looks self-contained: clear
    the cache (or move the workspace to another machine) and the next run,
    which is forced offline, fails with nothing to load. Copying is done here
    rather than through a speechbrain option so that it does not depend on
    which speechbrain version is installed.

    Never fatal: a link that cannot be copied is left as it is and logged,
    because the model that was just loaded works either way.
    """
    done = 0
    try:
        entries = [p for p in Path(model_dir).iterdir() if p.is_symlink()]
    except OSError:
        return 0
    for link in entries:
        tmp: Path | None = None
        try:
            target = link.resolve(strict=True)
            if not target.is_file():
                continue
            # Unique per process and per call: two runs loading the model at
            # the same time must not write into one temp file.
            tmp = link.with_name(
                f"{link.name}{TEMP_COPY_MARK}{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            shutil.copyfile(target, tmp)
            os.replace(tmp, link)
            tmp = None
            done += 1
        except OSError as e:
            if tmp is not None:
                # A failed copy (a full disk) must not leave a partial file.
                try:
                    tmp.unlink()
                except OSError:
                    pass
            logger.warning(
                f"could not copy {link.name} into the workspace ({type(e).__name__}); it "
                "stays a link into the Hugging Face cache, so keep that cache"
            )
    if done:
        logger.info(f"speaker model: {done} file(s) copied into {model_dir} (no longer links into the cache)")
    return done


def load_wav_mono16k(path: str | Path) -> np.ndarray:
    """Load any audio file as 16 kHz mono float32 via ffmpeg (format-agnostic)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-ar", "16000", "-ac", "1", "-f", "wav", tmp_path],
            check=True, timeout=300,
        )
        import soundfile as sf

        data, sr = sf.read(tmp_path, dtype="float32")
        assert sr == 16000
        return data
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def extract_embedding_from_segments(
    wav: np.ndarray,
    sr: int,
    segments: list[tuple[float, float]],
    model_dir: Path,
    max_segments: int | None = None,
) -> tuple[np.ndarray | None, float]:
    """Embed several (start, end) second-ranges and average them.

    Segments under MIN_SEGMENT_SEC are skipped. With max_segments set, only
    the longest N segments are used (cheaper with no accuracy loss for
    enrollment-sized inputs). Returns (mean embedding or None, seconds used).
    """
    import torch

    valid = [(s, e) for s, e in segments if (e - s) >= MIN_SEGMENT_SEC]
    if not valid:
        return None, 0.0
    if max_segments and len(valid) > max_segments:
        valid = sorted(valid, key=lambda se: se[1] - se[0], reverse=True)[:max_segments]

    model = get_model(model_dir)
    embs = []
    used_sec = 0.0
    for s, e in valid:
        i0 = max(int(s * sr), 0)
        i1 = min(int(e * sr), len(wav))
        chunk = wav[i0:i1]
        if len(chunk) < int(MIN_SEGMENT_SEC * sr):
            continue
        sig = torch.from_numpy(chunk).float().unsqueeze(0)
        with torch.no_grad():
            emb = model.encode_batch(sig)
        embs.append(emb.squeeze().cpu().numpy())
        used_sec += len(chunk) / sr  # actual audio used (keeps weighted-average denominators honest)
    if not embs:
        return None, 0.0
    return np.mean(np.stack(embs), axis=0), used_sec


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two embeddings (range -1.0 to 1.0)."""
    a, b = a.flatten(), b.flatten()
    if np.isnan(a).any() or np.isnan(b).any():
        return 0.0
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)
