"""What doctor says about pyannote on the Soniox path.

The hybrid step made an old certainty conditional. Before it, "stt: soniox"
meant pyannote was simply not part of the picture and doctor said nothing about
it. Now there is a third state: a user with a Hugging Face token but no
pyannote installed, whose hybrid step fails on every single recording and
reports it nowhere but the log. That is the state these tests pin down —
doctor has to call it a failure, while a plain Soniox setup with no token stays
quiet and passing.

Getting the condition backwards is invisible in normal use, which is exactly
why it is worth a test: both mistakes produce a doctor that runs and prints
something plausible.
"""
import argparse

import pytest

from moim_scribe import cli
from moim_scribe.config import ensure_workspace_dirs, load_config


@pytest.fixture
def ws(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    return tmp_path


def _run(ws, capsys, **cfg_overrides):
    """Run doctor over a workspace whose config.yaml carries the overrides."""
    if cfg_overrides:
        lines = [f"{k}: {'true' if v is True else 'false' if v is False else v}"
                 for k, v in cfg_overrides.items()]
        (ws / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cli.cmd_doctor(argparse.Namespace(workspace=str(ws)))
    return capsys.readouterr().out


def _pyannote_line(out):
    return next((ln for ln in out.splitlines() if "pyannote.audio importable" in ln), None)


# The check reports on the real import, which is absent in the CI test env and
# may be present locally. Either way the question here is whether the line is
# printed at all, so the assertions look for presence, not OK/FAIL.


def test_soniox_without_a_token_does_not_check_pyannote(ws, capsys):
    """The ordinary Soniox setup. Nothing about pyannote is the user's problem,
    and reporting it would train them to ignore doctor's output."""
    out = _run(ws, capsys, stt_backend="soniox")

    assert _pyannote_line(out) is None
    assert "hybrid speaker boundaries idle" in out


def test_soniox_with_a_token_checks_pyannote(ws, capsys):
    """A token means the hybrid step will try to run. If pyannote is missing it
    fails on every recording, and the log is the only place that says so."""
    (ws / ".hf_token").write_text("tok", encoding="utf-8")

    out = _run(ws, capsys, stt_backend="soniox")

    assert _pyannote_line(out) is not None
    assert "hybrid speaker boundaries idle" not in out


def test_hybrid_off_ignores_the_token(ws, capsys):
    """Turned off, the token is irrelevant again — no check, and no note about
    an idle step that is not merely idle but disabled."""
    (ws / ".hf_token").write_text("tok", encoding="utf-8")

    out = _run(ws, capsys, stt_backend="soniox", hybrid_diarization=False)

    assert _pyannote_line(out) is None
    assert "hybrid speaker boundaries idle" not in out


def test_the_env_token_counts_too(ws, capsys, monkeypatch):
    """HF_TOKEN in the environment is a supported way to hold the token, and
    find_hf_token prefers it over the file."""
    monkeypatch.setenv("HF_TOKEN", "tok")

    out = _run(ws, capsys, stt_backend="soniox")

    assert _pyannote_line(out) is not None


def test_the_local_path_still_requires_both(ws, capsys):
    """The mirror test: the new condition must not have made pyannote or the
    token optional where they are mandatory."""
    out = _run(ws, capsys, stt_backend="mlx")

    assert _pyannote_line(out) is not None
    assert "Hugging Face token" in out
