"""The command line: process isolation, exit codes, workspace and config refusals,
init that never resets config.yaml, and what doctor reports.

The engine is stubbed as in the pipeline tests. Like the real one, the stub
writes its work products next to the audio it is given, which is the property
`process` relies on to keep its work out of inbox/_processing.
"""
import json
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from moim_scribe import cli, pipeline, preflight
from moim_scribe.config import load_config


class _Summ:
    def summarize(self, prompt):
        return "# T\n\nbody\n"


def _init(ws, *extra):
    return cli.main(["init", str(ws), "--language", "en", *extra])


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    assert _init(w) == 0
    return w


def _stub_engine(monkeypatch, seen=None, fail=False):
    def fake_run(audio, **kw):
        folder = Path(audio).parent
        if seen is not None:
            seen.append(folder)
        # The real engine leaves its full-length wav behind when it fails.
        (folder / f"_diarize_full_{os.getpid()}.wav").write_bytes(b"W" * 1000)
        if fail:
            raise RuntimeError("engine boom")
        stem = Path(audio).stem
        txt = folder / f"{stem}_transcript_diarized.txt"
        jl = folder / f"{stem}_transcript_diarized.jsonl"
        sp = folder / f"{stem}_speakers.json"
        txt.write_text("[  0.0s] SPEAKER_00: hello\n", encoding="utf-8")
        jl.write_text('{"start":0,"end":1,"speaker":"SPEAKER_00","text":"hello"}\n',
                      encoding="utf-8")
        sp.write_text(json.dumps({"n_speakers": 1, "speakers": []}), encoding="utf-8")
        return {"txt_path": str(txt), "jsonl_path": str(jl), "stats_path": str(sp)}

    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", fake_run)
    monkeypatch.setattr(pipeline, "get_summarizer", lambda *a, **k: _Summ())
    monkeypatch.setattr(pipeline, "_recording_time_block", lambda *a, **k: "")


def _snapshot(folder: Path) -> dict:
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


# --- A1: process works on a private copy ------------------------------------------

def test_process_never_touches_what_a_watcher_has_claimed(ws, tmp_path, monkeypatch):
    """Red team a8: a watcher holds the only copy of two recordings in
    _processing; `process` on an unrelated file of the same name must leave
    both byte-for-byte (the old code overwrote one and globbed both away)."""
    _stub_engine(monkeypatch)
    proc = ws / "inbox" / "_processing"
    (proc / "team sync 2026-11-11.m4a").write_bytes(b"ORIGINAL-A" * 500)
    (proc / "team.m4a").write_bytes(b"ORIGINAL-B" * 500)
    (proc / "team [x].m4a").write_bytes(b"ORIGINAL-C" * 500)  # a stem that is a glob
    before = _snapshot(proc)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "team.m4a").write_bytes(b"NEW" * 100)

    assert cli.main(["process", str(elsewhere / "team.m4a"), "--workspace", str(ws)]) == 0

    assert _snapshot(proc) == before
    assert (elsewhere / "team.m4a").read_bytes() == b"NEW" * 100
    assert list((ws / "results").glob("*_summary.md"))


def test_process_refuses_a_file_in_inbox(ws, monkeypatch, capsys):
    """Red team a8b: `process inbox/x.m4a` raced the scheduled scan for the same
    file and the claim of the same name in _processing. Recordings in inbox/
    belong to the watcher."""
    _stub_engine(monkeypatch)
    rec = ws / "inbox" / "board meeting.m4a"
    rec.write_bytes(b"IN-INBOX" * 100)
    sub = ws / "inbox" / "team"
    sub.mkdir()
    (sub / "x.m4a").write_bytes(b"SUB" * 100)
    proc = ws / "inbox" / "_processing"
    (proc / "board meeting.m4a").write_bytes(b"CLAIMED" * 100)
    before = _snapshot(proc)

    assert cli.main(["process", str(rec), "--workspace", str(ws)]) == 1
    assert cli.main(["process", str(sub / "x.m4a"), "--workspace", str(ws)]) == 1

    assert "scan-inbox" in capsys.readouterr().err
    assert _snapshot(proc) == before
    assert rec.read_bytes() == b"IN-INBOX" * 100
    assert not list((ws / "results").iterdir())


def test_process_engine_writes_into_its_own_temp_dir_and_removes_it(ws, tmp_path, monkeypatch):
    seen = []
    _stub_engine(monkeypatch, seen=seen)
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)

    assert cli.main(["process", str(src), "--workspace", str(ws)]) == 0

    assert len(seen) == 1
    assert seen[0].parent == ws.resolve() and seen[0].name.startswith(".process-")
    assert not seen[0].exists()
    assert not list((ws / "inbox" / "_processing").iterdir())


def test_a_failed_process_leaves_no_temp_dir_or_wav(ws, tmp_path, monkeypatch):
    seen = []
    _stub_engine(monkeypatch, seen=seen, fail=True)
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)

    # An ordinary failure ends in exit 1 and one line, not a traceback.
    assert cli.main(["process", str(src), "--workspace", str(ws)]) == 1

    assert not seen[0].exists()
    assert not list(ws.glob(".process-*"))
    assert not list(ws.rglob("_diarize_full_*.wav"))
    assert src.read_bytes() == b"x" * 100


def test_process_stops_on_a_setup_problem_before_copying(ws, tmp_path, monkeypatch, capsys):
    _stub_engine(monkeypatch)
    monkeypatch.setattr(preflight, "preflight", lambda cfg: ["Hugging Face token: save it"])
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)

    assert cli.main(["process", str(src), "--workspace", str(ws)]) == 1

    assert "setup problem: Hugging Face token: save it" in capsys.readouterr().err
    assert not list(ws.glob(".process-*"))


def test_process_turns_systemexit_into_a_setup_problem(ws, tmp_path, monkeypatch, capsys):
    _stub_engine(monkeypatch)

    def boom(*a, **k):
        raise SystemExit("no token")

    monkeypatch.setattr(pipeline, "get_hf_token", boom)
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)

    assert cli.main(["process", str(src), "--workspace", str(ws)]) == 1

    assert "setup problem: no token" in capsys.readouterr().err
    assert not list(ws.glob(".process-*"))


def test_process_type_flag_compares_folder_names_normalized(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    _init(ws)
    (ws / "config.yaml").write_text("types:\n  Transcript: null\n", encoding="utf-8")
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)
    got = []
    monkeypatch.setattr("moim_scribe.pipeline.process_recording",
                        lambda cfg_, claimed, **kw: got.append(kw) or claimed)

    assert cli.main(["process", str(src), "--type", "transcript", "--workspace", str(ws)]) == 0
    assert got == [{"summarize": False}]


# --- A5: exit codes ------------------------------------------------------------

@pytest.mark.parametrize("counts, code", [
    ({"processed": 1, "failed": 0, "skipped": 0}, 0),
    ({"processed": 0, "failed": 0, "skipped": 0}, 0),  # also "another run is active"
    ({"processed": 1, "failed": 1, "skipped": 0}, 1),
    ({"processed": 0, "failed": 0, "skipped": 0, "setup_problems": ["x: y"]}, 1),
])
def test_scan_inbox_exit_code(ws, monkeypatch, capsys, counts, code):
    monkeypatch.setattr("moim_scribe.pipeline.scan_inbox", lambda cfg: counts)
    assert cli.main(["scan-inbox", str(ws)]) == code
    out = capsys.readouterr().out
    assert ("setup problem" in out) == ("setup_problems" in counts)


def test_scan_inbox_exits_1_when_a_recording_failed(ws, monkeypatch):
    """Red team a8c, through the real scan_inbox."""
    _stub_engine(monkeypatch, fail=True)
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    (ws / "inbox" / "bad one.m4a").write_bytes(b"x" * 100)
    assert cli.main(["scan-inbox", str(ws)]) == 1
    assert (ws / "inbox" / "_error" / "bad one.m4a").exists()


def test_scan_inbox_exits_1_on_a_real_setup_problem(ws, monkeypatch, capsys, real_preflight):
    monkeypatch.setattr("shutil.which", lambda name: None)
    (ws / "inbox" / "rec.m4a").write_bytes(b"x" * 100)
    assert cli.main(["scan-inbox", str(ws)]) == 1
    assert "setup problem" in capsys.readouterr().out
    assert (ws / "inbox" / "rec.m4a").exists()


def test_enroll_with_a_label_not_in_the_jsonl_exits_1(ws, tmp_path, monkeypatch, caplog):
    from moim_scribe.speakers import enroll as en

    monkeypatch.setattr(en, "load_wav_mono16k", lambda p: np.zeros(16000))
    monkeypatch.setattr(en, "extract_embedding_from_segments",
                        lambda w, sr, ranges, model_dir: (np.ones(8) / np.sqrt(8), 5.0))
    jl = tmp_path / "rec_transcript_diarized.jsonl"
    jl.write_text("\n".join(json.dumps({"start": i * 6.0, "end": i * 6.0 + 5, "speaker": spk})
                            for i, spk in enumerate(["SPEAKER_00", "SPEAKER_01"] * 3)),
                  encoding="utf-8")
    rc = cli.main(["enroll", str(tmp_path / "rec.m4a"), str(jl), "SPEAKER_00=Ada",
                   "SPEAKER_07=Bo", "--workspace", str(ws)])
    assert rc == 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "label not found" in msg and "SPEAKER_00, SPEAKER_01" in msg
    index = json.loads((ws / "speakers" / "index.json").read_text(encoding="utf-8"))
    assert set(index) == {"Ada"}


def test_enroll_refusal_kind_for_a_missing_label(tmp_path, monkeypatch):
    from moim_scribe.speakers import enroll as en
    from moim_scribe.speakers.db import SpeakerDB

    monkeypatch.setattr(en, "load_wav_mono16k", lambda p: np.zeros(16000))
    jl = tmp_path / "x.jsonl"
    jl.write_text(json.dumps({"start": 0, "end": 5, "speaker": "SPEAKER_00"}), encoding="utf-8")
    res = en.enroll(tmp_path / "a.m4a", jl, {"SPEAKER_03": "Bo"}, SpeakerDB(tmp_path / "s"),
                    tmp_path, embed_fn=lambda w, sr, r: (np.ones(4), 5.0))
    assert res["enrolled"] == []
    assert [(r["label"], r["kind"]) for r in res["refused"]] == [("SPEAKER_03", "label not found")]


# --- B2: no config.yaml, no workspace --------------------------------------------

@pytest.mark.parametrize("argv", [
    ["scan-inbox", "{ws}"],
    ["watch", "{ws}"],
    ["doctor", "{ws}"],
    ["speakers", "{ws}"],
    ["process", "{src}", "--workspace", "{ws}"],
    ["enroll", "{src}", "{src}", "SPEAKER_00=A", "--workspace", "{ws}"],
    ["verify", "{src}", "{src}", "--workspace", "{ws}"],
])
def test_every_command_but_init_refuses_a_folder_without_config(tmp_path, capsys, argv):
    typo = tmp_path / "MyMeetngs"
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x")
    argv = [a.format(ws=typo, src=src) for a in argv]
    assert cli.main(argv) == 1
    assert f'run "moim-scribe init {typo}" first' in capsys.readouterr().err
    assert not typo.exists()


def test_an_existing_folder_without_config_gains_nothing(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    assert cli.main(["scan-inbox", str(folder)]) == 1
    assert list(folder.iterdir()) == []


# --- ConfigError ----------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "summarizer: nonsense\n",
    "- a\n- b\n",
    "default_speakers: -1\n",
])
def test_an_unusable_config_is_one_line_and_exit_1(ws, capsys, body):
    (ws / "config.yaml").write_text(body, encoding="utf-8")
    assert cli.main(["scan-inbox", str(ws)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("config error: config.yaml:")
    assert "Traceback" not in err


def test_init_reports_an_unusable_existing_config(ws, capsys):
    (ws / "config.yaml").write_text("- a\n", encoding="utf-8")
    (ws / ".gitignore").unlink()
    assert _init(ws) == 1
    assert "config error:" in capsys.readouterr().err
    assert (ws / "config.yaml").read_text(encoding="utf-8") == "- a\n"
    assert (ws / ".gitignore").exists()  # repaired anyway


def test_init_load_error_of_the_config_it_wrote_is_caught(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONFIG_TEMPLATE", "summarizer: nonsense\nlanguage: {language}\n"
                                                "# {stt_backend}\n")
    assert _init(tmp_path / "w") == 1
    assert "config error:" in capsys.readouterr().err


# --- B3/B4: init ----------------------------------------------------------------

def test_init_twice_keeps_config_and_restores_missing_lens(ws, capsys):
    cfg_path = ws / "config.yaml"
    cfg_path.write_text("language: en\nsummarizer: none\n", encoding="utf-8")
    (ws / "packs" / "lenses" / "summary.en.md").unlink()
    capsys.readouterr()

    assert _init(ws) == 0

    assert cfg_path.read_text(encoding="utf-8") == "language: en\nsummarizer: none\n"
    assert (ws / "packs" / "lenses" / "summary.en.md").is_file()
    assert "left untouched" in capsys.readouterr().out
    assert not (ws / "config.yaml.bak").exists()


def test_init_force_is_a_no_op_alias(ws, capsys):
    (ws / "config.yaml").write_text("summarizer: none\n", encoding="utf-8")
    capsys.readouterr()
    assert _init(ws, "--force") == 0
    assert (ws / "config.yaml").read_text(encoding="utf-8") == "summarizer: none\n"
    assert "--reset-config" in capsys.readouterr().out


def test_init_reset_config_keeps_a_backup(ws, capsys):
    (ws / "config.yaml").write_text("summarizer: none\n", encoding="utf-8")
    capsys.readouterr()
    assert _init(ws, "--reset-config") == 0
    assert (ws / "config.yaml.bak").read_text(encoding="utf-8") == "summarizer: none\n"
    assert load_config(ws).summarizer == "claude_cli"
    assert "config.yaml.bak" in capsys.readouterr().out


def test_init_says_what_it_wrote(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_stt_backend", lambda: "faster")
    assert _init(tmp_path / "w") == 0
    out = capsys.readouterr().out
    assert "language en, stt_backend faster" in out
    assert "--language en" in out and "edit config.yaml" in out
    assert load_config(tmp_path / "w").stt_backend == "faster"


def test_gitignore_lists_packs_and_glossary(ws):
    lines = (ws / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "packs/" in lines and "glossary.txt" in lines


def test_gitignore_of_an_old_workspace_gains_packs_and_glossary(ws):
    gi = ws / ".gitignore"
    gi.write_text("# mine\nspeakers/\n.hf_token\n.soniox_key\n", encoding="utf-8")
    assert _init(ws) == 0
    lines = gi.read_text(encoding="utf-8").splitlines()
    assert "packs/" in lines and "glossary.txt" in lines and "# mine" in lines
    assert lines.count(".hf_token") == 1


# --- logging --------------------------------------------------------------------

def test_httpx_request_lines_are_quiet_unless_verbose():
    import logging

    cli._setup_logging(False)
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.WARNING
    cli._setup_logging(True)
    assert logging.getLogger("httpx").level == logging.NOTSET


# --- B6/C2: doctor ----------------------------------------------------------------

def _doctor(ws, capsys, *extra, body=None):
    if body is not None:
        (ws / "config.yaml").write_text(body, encoding="utf-8")
    rc = cli.main(["doctor", str(ws), *extra])
    return rc, capsys.readouterr().out


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("HF_TOKEN", "SONIOX_API_KEY", "OLLAMA_HOST", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_doctor_first_line_warns_about_the_slow_import(ws, capsys, clean_env):
    _, out = _doctor(ws, capsys)
    assert "up to a minute" in out.splitlines()[0]


@pytest.mark.parametrize("body, audio, transcript", [
    ("stt_backend: faster\nsummarizer: claude_cli\n",
     "audio: stays on this machine",
     "transcript: made on this machine, then sent to Anthropic for the summary (claude_cli)"),
    ("stt_backend: soniox\nsummarizer: anthropic_api\n",
     "audio: UPLOADED to Soniox", "then sent to Anthropic for the summary (anthropic_api)"),
    ("stt_backend: faster\nsummarizer: none\n",
     "audio: stays on this machine", "transcript: stays on this machine (summarizer: none)"),
    ("stt_backend: faster\nsummarizer: ollama\n",
     "audio: stays on this machine", "transcript: stays on this machine (ollama)"),
])
def test_doctor_states_the_data_flow(ws, capsys, clean_env, body, audio, transcript):
    _, out = _doctor(ws, capsys, body=body + "language: en\n")
    assert audio in out and transcript in out
    assert "language: en" in out


def test_doctor_names_a_remote_ollama(ws, capsys, clean_env, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11434")
    _, out = _doctor(ws, capsys, body="summarizer: ollama\n")
    assert "then sent to the Ollama server at gpu-box:11434" in out


def test_doctor_requirements_are_the_preflight_list(ws, capsys, clean_env, real_preflight):
    from moim_scribe.config import load_config as lc

    _, out = _doctor(ws, capsys)
    for c in preflight.checks(lc(ws)):
        assert c.label in out


def test_doctor_fails_without_a_token(ws, capsys, clean_env, real_preflight):
    rc, out = _doctor(ws, capsys, body="stt_backend: faster\nsummarizer: none\n")
    assert rc == 1
    assert "[FAIL] Hugging Face token" in out


def test_doctor_fails_an_empty_token_file(ws, capsys, clean_env, real_preflight, monkeypatch):
    monkeypatch.setattr(preflight, "checks", lambda cfg: [])  # only the content check
    (ws / ".hf_token").write_bytes(b"\xef\xbb\xbf\r\n  \r\n")
    rc, out = _doctor(ws, capsys, body="stt_backend: faster\nsummarizer: none\n")
    assert rc == 1
    assert ".hf_token has content: the file is empty" in out


def test_doctor_warns_about_a_token_without_hf_prefix(ws, capsys, clean_env):
    (ws / ".hf_token").write_bytes("abc123\r\n".encode("utf-16"))
    _, out = _doctor(ws, capsys, body="stt_backend: faster\nsummarizer: none\n")
    assert "[OK  ]" in out and ".hf_token has content" in out
    assert "[WARN] the Hugging Face token does not start with hf_" in out


def test_doctor_reports_any_import_exception_without_a_traceback(ws, capsys, clean_env,
                                                                 monkeypatch):
    monkeypatch.setattr(preflight, "module_findable", lambda name: True)
    real_import = cli.importlib.import_module

    def broken(name):
        if name == "speechbrain":
            raise OSError("libtorch.dylib: image not found")
        if name in ("faster_whisper", "pyannote.audio"):
            return types.ModuleType(name)
        return real_import(name)

    monkeypatch.setattr(cli.importlib, "import_module", broken)
    rc, out = _doctor(ws, capsys, body="stt_backend: faster\nsummarizer: none\n")
    assert rc == 1
    assert "[FAIL] speechbrain importable: import failed with OSError" in out
    assert "[OK  ] faster_whisper importable" in out


def test_doctor_is_offline_by_default(ws, capsys, clean_env, monkeypatch):
    calls = []
    hub = types.ModuleType("huggingface_hub")
    hub.auth_check = lambda repo, token=None: calls.append(repo)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    _doctor(ws, capsys)
    assert calls == []


def test_doctor_online_names_the_repo_to_agree_to(ws, capsys, clean_env, monkeypatch):
    from moim_scribe.secrets import GATED_HF_REPOS

    class GatedRepoError(Exception):
        pass

    def auth_check(repo, token=None):
        assert token == "hf_x"
        if repo == GATED_HF_REPOS[1]:
            raise GatedRepoError("403")
        if repo == GATED_HF_REPOS[2]:
            raise ConnectionError("offline")

    hub = types.ModuleType("huggingface_hub")
    hub.auth_check = auth_check
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setattr(preflight, "checks", lambda cfg: [])
    monkeypatch.setattr(cli, "_import_error", lambda name: None)
    monkeypatch.setattr(preflight, "module_findable", lambda name: True)

    rc, out = _doctor(ws, capsys, "--online", body="summarizer: none\n")

    assert f"{GATED_HF_REPOS[0]}: this token has access" in out
    assert f"https://huggingface.co/{GATED_HF_REPOS[1]}" in out
    assert '"Agree and access repository"' in out
    assert f"could not check {GATED_HF_REPOS[2]}: ConnectionError" in out
    assert rc == 1  # the refused repo; "could not check" alone would not fail


def test_doctor_online_without_the_hub_package_is_not_a_failure(ws, capsys, clean_env,
                                                                 monkeypatch):
    from moim_scribe.secrets import GATED_HF_REPOS

    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # import raises ModuleNotFoundError
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setattr(preflight, "checks", lambda cfg: [])
    monkeypatch.setattr(cli, "_import_error", lambda name: None)
    monkeypatch.setattr(preflight, "module_findable", lambda name: True)

    rc, out = _doctor(ws, capsys, "--online", body="summarizer: none\n")

    for repo in GATED_HF_REPOS:
        assert f"could not check {repo}: ModuleNotFoundError" in out
    assert rc == 0


def test_process_reports_an_ordinary_failure_in_one_line(tmp_path, monkeypatch, capsys):
    """A summarizer or engine failure in `process` used to end in a traceback.
    It must end in one line, exit 1, the original untouched and no temp dir."""
    ws = tmp_path / "ws"
    assert cli.main(["init", str(ws), "--language", "en"]) == 0
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)

    def boom(cfg, claimed, **kw):
        raise RuntimeError("summarization failed (transcript kept at results/x.txt): rate limited")

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", boom)
    capsys.readouterr()
    assert cli.main(["process", str(src), "--workspace", str(ws)]) == 1
    err = capsys.readouterr().err
    assert "failed: RuntimeError: summarization failed" in err
    assert "Traceback" not in err
    assert src.read_bytes() == b"x" * 100
    assert not list(ws.glob(".process-*"))
