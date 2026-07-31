"""The prompt filter: which speakers.json fields may reach a summarizer.

The risk is specific. `anchors` and `suspects` are produced by comparing every
cluster against the whole enrolment database, so they can carry the names of
people who were not in the recording — and the prompt header directly above
this block tells the model the attendee list comes from this data.

The last test here is the one that matters most: it states the property in terms
of names rather than field names, so it keeps holding when the schema changes.
"""
import json
from pathlib import Path

from moim_scribe.diarize import write_outputs
from moim_scribe.speaker_stats import (
    MERGE_REASSIGN_PROMPT_KEYS,
    render_speaker_stats_block,
    strip_diagnostic_keys,
)

ABSENT = "Absent Person"
ALSO_ABSENT = "Another Absentee"

FIRED = {
    "triggered": True,
    "anchors": ["Ada", ABSENT],
    "suspects": {
        "SPEAKER_00": {
            "talk": {ALSO_ABSENT: 3.0},
            "winners": [ALSO_ABSENT],
            "avg_sim": {ALSO_ABSENT: 0.41},
        }
    },
    "stats": {"n_total": 1, "n_confident": 1},
}


def test_normal_fields_pass_through():
    data = {
        "n_speakers": 2,
        "speakers": [
            {"label": "Ada", "talk_sec": 12.7, "share": 0.85, "identified": True},
            {"label": "SPEAKER_01", "talk_sec": 2.3, "share": 0.15, "identified": False},
        ],
    }
    assert strip_diagnostic_keys(data) == data


def test_merge_reassign_keeps_only_the_boolean():
    out = strip_diagnostic_keys({"n_speakers": 1, "speakers": [], "merge_reassign": dict(FIRED)})
    assert out["merge_reassign"] == {"triggered": True}


def test_presence_unconfirmed_never_reaches_a_prompt():
    """The withhold reason names anchors compared against the whole DB — people
    possibly not in the room. The allow-list drops it structurally; this pins
    that the new field stays dropped if the list is ever widened."""
    withheld = {
        "triggered": False,
        "anchors": ["Ada", "Bo"],
        "presence_unconfirmed": ["Bo"],
        "stats": {"anchor_presence": {"Ada": 3, "Bo": 0}},
    }
    out = strip_diagnostic_keys({"n_speakers": 1, "speakers": [], "merge_reassign": withheld})
    assert out["merge_reassign"] == {"triggered": False}


def test_merge_reassign_survives_as_a_signal_not_as_a_deletion():
    """A lens needs to know a reassignment happened — that is why it is not dropped whole."""
    assert "triggered" in MERGE_REASSIGN_PROMPT_KEYS
    out = strip_diagnostic_keys({"speakers": [], "merge_reassign": dict(FIRED)})
    assert "merge_reassign" in out


def test_diagnostic_entry_key_is_removed():
    data = {
        "speakers": [
            {"label": "Ada", "identified": True, "match_score_full_db": {"name": ABSENT, "score": 0.7}}
        ]
    }
    out = strip_diagnostic_keys(data)
    assert out["speakers"][0] == {"label": "Ada", "identified": True}


def test_diag_suffix_is_filtered_at_both_levels():
    data = {
        "n_speakers": 1,
        "whatever_diag": {"name": ABSENT},
        "speakers": [{"label": "Ada", "probe_diag": ABSENT}],
    }
    out = strip_diagnostic_keys(data)
    assert "whatever_diag" not in out
    assert out["speakers"][0] == {"label": "Ada"}


def test_render_is_indented_json_with_korean_intact(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"n_speakers": 1, "speakers": [{"label": "김하늘"}]}), encoding="utf-8")
    out = render_speaker_stats_block(p)
    assert "김하늘" in out  # not \uXXXX escaped
    assert '\n  "n_speakers"' in out  # indent=2, matching write_outputs


def test_render_returns_empty_for_missing_file(tmp_path):
    assert render_speaker_stats_block(tmp_path / "nope.json") == ""


def test_render_returns_empty_for_corrupt_json_without_echoing_it(tmp_path, caplog):
    p = tmp_path / "s.json"
    p.write_text('{"speakers": [{"label": "' + ABSENT + '"', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert render_speaker_stats_block(p) == ""
    assert ABSENT not in caplog.text  # a filter must not leak via its own error path


def test_render_returns_empty_for_non_object_json(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert render_speaker_stats_block(p) == ""


def test_no_name_outside_the_speaker_labels_reaches_the_prompt(tmp_path):
    """Stated as a property of names, so it holds through schema changes.

    Uses the real write_outputs output rather than a hand-built dict, so this
    also fails if that writer starts emitting a new name-bearing field.
    """
    merged = [{"start": 0.0, "end": 10.0, "speaker": "Ada", "text": "hello"}]
    _, _, spk_path, _ = write_outputs(str(tmp_path), "r_", merged, {}, {}, dict(FIRED))

    rendered = render_speaker_stats_block(Path(spk_path))
    labels = {s["label"] for s in json.loads(rendered)["speakers"]}
    assert labels == {"Ada"}
    for absent in (ABSENT, ALSO_ABSENT):
        assert absent not in rendered, f"{absent} reached the prompt"

    # ...and the file on disk still has them, so diagnosis loses nothing.
    raw = Path(spk_path).read_text(encoding="utf-8")
    assert ABSENT in raw and ALSO_ABSENT in raw
