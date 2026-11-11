"""The speaker model in the workspace must be real files, not links.

Found in a clean-install run: `models/spkrec-ecapa-voxceleb/*.ckpt` were all
symlinks into the Hugging Face cache. With the cache gone, the next run (which
is forced offline) had nothing to load, while the docs said the model lives in
the workspace.
"""
import logging
import sys

import pytest

from moim_scribe.speakers.embed import materialize_symlinks

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


def _cache_and_model_dir(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    model = tmp_path / "ws" / "models" / "ecapa"
    model.mkdir(parents=True)
    return cache, model


def test_links_become_real_files_with_the_same_bytes(tmp_path):
    cache, model = _cache_and_model_dir(tmp_path)
    (cache / "blob1").write_bytes(b"weights-1" * 1000)
    (cache / "blob2").write_bytes(b"norm")
    (model / "embedding_model.ckpt").symlink_to(cache / "blob1")
    (model / "mean_var_norm_emb.ckpt").symlink_to(cache / "blob2")
    (model / "hyperparams.yaml").write_text("already a file", encoding="utf-8")

    assert materialize_symlinks(model) == 2
    for name, body in [("embedding_model.ckpt", b"weights-1" * 1000), ("mean_var_norm_emb.ckpt", b"norm")]:
        f = model / name
        assert not f.is_symlink() and f.read_bytes() == body
    assert (model / "hyperparams.yaml").read_text(encoding="utf-8") == "already a file"
    assert not list(model.glob("*.copying*"))         # no temp files left


def test_the_model_survives_the_cache_being_deleted(tmp_path):
    """The whole point: wipe the cache, the workspace still has the model."""
    cache, model = _cache_and_model_dir(tmp_path)
    (cache / "blob").write_bytes(b"weights")
    (model / "embedding_model.ckpt").symlink_to(cache / "blob")
    materialize_symlinks(model)
    (cache / "blob").unlink()
    assert (model / "embedding_model.ckpt").read_bytes() == b"weights"


def test_a_second_call_has_nothing_to_do(tmp_path):
    cache, model = _cache_and_model_dir(tmp_path)
    (cache / "blob").write_bytes(b"weights")
    (model / "a.ckpt").symlink_to(cache / "blob")
    assert materialize_symlinks(model) == 1
    assert materialize_symlinks(model) == 0


def test_a_broken_link_is_left_alone_and_reported(tmp_path, caplog):
    cache, model = _cache_and_model_dir(tmp_path)
    (model / "gone.ckpt").symlink_to(cache / "missing")
    with caplog.at_level(logging.WARNING, logger="moim_scribe.speakers.embed"):
        assert materialize_symlinks(model) == 0
    assert (model / "gone.ckpt").is_symlink()
    assert any("gone.ckpt" in r.message for r in caplog.records)


def test_a_missing_model_dir_is_not_an_error(tmp_path):
    assert materialize_symlinks(tmp_path / "nope") == 0


def test_get_model_copies_what_the_loader_linked(tmp_path, monkeypatch):
    """The function above is only worth anything if loading actually calls it."""
    import types
    from pathlib import Path

    from moim_scribe.speakers import embed

    cache, model = _cache_and_model_dir(tmp_path)
    (cache / "blob").write_bytes(b"weights")

    class FakeEncoder:
        @classmethod
        def from_hparams(cls, source, savedir, run_opts):
            (Path(savedir) / "embedding_model.ckpt").symlink_to(cache / "blob")
            return object()

    speaker_mod = types.ModuleType("speechbrain.inference.speaker")
    speaker_mod.EncoderClassifier = FakeEncoder
    for name, mod in [
        ("torch", types.ModuleType("torch")),
        ("speechbrain", types.ModuleType("speechbrain")),
        ("speechbrain.inference", types.ModuleType("speechbrain.inference")),
        ("speechbrain.inference.speaker", speaker_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(embed, "_model", None)
    monkeypatch.setattr(embed, "_model_dir", None)

    embed.get_model(model)
    f = model / "embedding_model.ckpt"
    assert not f.is_symlink() and f.read_bytes() == b"weights"
