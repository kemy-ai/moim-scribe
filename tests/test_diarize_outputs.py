"""write_outputs: the three-file output contract, pinned at every level.

This is the schema pipeline.process_recording consumes and the one a prompt
filter has to operate on, so it is driven from a plain fixture — no whisper, no
pyannote, no torch. Before this file the contract had no tests at all.
"""
import json
from pathlib import Path

from moim_scribe.diarize import write_outputs

# Every key write_outputs may emit, by level. Putting a field on merge_info does
# NOT surface it in the output — the writer uses an allow-list — so adding one
# means editing both the writer and this set. That friction is the point:
# "anchors" and "suspects" are built by comparing against the whole enrolment
# DB, so they can name people who were not in the room, and every new field
# needs a deliberate call on whether it may reach a summarizer prompt.
KNOWN_TOP_KEYS = {"n_speakers", "speakers", "merge_reassign"}
KNOWN_ENTRY_KEYS = {"label", "talk_sec", "share", "identified", "raw_speaker", "match_score"}
# presence_unconfirmed: the presence gate's withhold reason (anchor names on
# purpose — post-hoc diagnosis; speaker_stats drops it before any prompt).
KNOWN_MERGE_REASSIGN_KEYS = {
    "triggered", "method", "anchors", "suspects", "stats", "presence_unconfirmed",
}

CLEAN_MERGE_INFO = {"triggered": False, "suspects": {}, "anchors": [], "stats": {}}
TRIGGERED_MERGE_INFO = {
    "triggered": True,
    "suspects": {"SPEAKER_00": {"talk": {"Ada": 9.0}, "winners": ["Ada"], "avg_sim": {"Ada": 0.4}}},
    "anchors": ["Ada", "Bo"],
    "stats": {"n_total": 3, "n_confident": 2},
}


def _merged():
    return [
        {"start": 0.0, "end": 10.0, "speaker": "Ada", "text": "first"},
        {"start": 10.0, "end": 12.3, "speaker": "SPEAKER_01", "text": "second"},
        {"start": 12.3, "end": 15.0, "speaker": "Ada", "text": "third"},
    ]


def _write(tmp_path, merged=None, id_mapping=None, id_sims=None, merge_info=None):
    return write_outputs(
        str(tmp_path),
        "rec_",
        _merged() if merged is None else merged,
        {} if id_mapping is None else id_mapping,
        {} if id_sims is None else id_sims,
        dict(CLEAN_MERGE_INFO) if merge_info is None else merge_info,
    )


def test_three_files_land_with_expected_names(tmp_path):
    jsonl, txt, spk, _ = _write(tmp_path)
    assert Path(jsonl).name == "rec_transcript_diarized.jsonl"
    assert Path(txt).name == "rec_transcript_diarized.txt"
    assert Path(spk).name == "rec_speakers.json"
    assert all(Path(p).is_file() for p in (jsonl, txt, spk))


def test_jsonl_round_trips_every_segment(tmp_path):
    jsonl, *_ = _write(tmp_path)
    lines = Path(jsonl).read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln) for ln in lines] == _merged()


def test_txt_line_format_is_a_contract_with_scan(tmp_path):
    """The txt layout is parsed back by speakers.scan — it is not cosmetics."""
    from moim_scribe.speakers.scan import LINE_RE

    _, txt, _, _ = _write(tmp_path)
    lines = Path(txt).read_text(encoding="utf-8").splitlines()
    assert lines[1] == "[  10.0s] SPEAKER_01: second"
    for line in lines:
        assert LINE_RE.match(line), f"scan.load_rows would drop this line: {line!r}"


def test_returned_dict_carries_no_filesystem_paths(tmp_path):
    """The caller attaches *_path keys; this schema must stay path-free.

    The dict travels into summarizer prompts and a home directory identifies
    the user, so paths belong to run_diarization's return value, not here.
    """
    *_, data = _write(tmp_path)
    assert [k for k in data if k.endswith("_path")] == []
    assert str(tmp_path) not in json.dumps(data)


def test_speakers_sorted_by_talk_and_shares_sum_to_one(tmp_path):
    *_, data = _write(tmp_path)
    assert data["n_speakers"] == 2
    assert [s["label"] for s in data["speakers"]] == ["Ada", "SPEAKER_01"]
    assert data["speakers"][0]["talk_sec"] == 12.7
    assert abs(sum(s["share"] for s in data["speakers"]) - 1.0) < 0.02


def test_identified_follows_id_mapping(tmp_path):
    *_, data = _write(tmp_path, id_mapping={"SPEAKER_00": "Ada"})
    by_label = {s["label"]: s for s in data["speakers"]}
    assert by_label["Ada"]["identified"] is True
    assert by_label["SPEAKER_01"]["identified"] is False


def test_identified_covers_anchor_only_names(tmp_path):
    """A merge reassignment can name someone cluster-mean matching never saw.

    Pins the union in write_outputs: identified = id_mapping.values() | anchors.
    """
    merged = [{"start": 0.0, "end": 5.0, "speaker": "Bo", "text": "x"}]
    *_, data = _write(tmp_path, merged=merged, id_mapping={}, merge_info=TRIGGERED_MERGE_INFO)
    entry = data["speakers"][0]
    assert entry["label"] == "Bo"
    assert entry["identified"] is True
    assert "raw_speaker" not in entry


def test_raw_speaker_and_match_score_only_for_mapped_labels(tmp_path):
    *_, data = _write(
        tmp_path,
        id_mapping={"SPEAKER_00": "Ada"},
        id_sims={"SPEAKER_00": [("Ada", 0.9123), ("Bo", 0.4)]},
    )
    by_label = {s["label"]: s for s in data["speakers"]}
    assert by_label["Ada"]["raw_speaker"] == "SPEAKER_00"
    assert by_label["Ada"]["match_score"] == 0.912
    assert "raw_speaker" not in by_label["SPEAKER_01"]
    assert "match_score" not in by_label["SPEAKER_01"]


def test_merge_reassign_present_only_when_triggered(tmp_path):
    *_, clean = _write(tmp_path)
    assert "merge_reassign" not in clean

    *_, fired = _write(tmp_path, merge_info=TRIGGERED_MERGE_INFO)
    assert fired["merge_reassign"]["anchors"] == ["Ada", "Bo"]
    assert "margin>=" in fired["merge_reassign"]["method"]


def test_merge_reassign_written_when_presence_gate_withheld(tmp_path):
    """A withheld pass must leave its reason on disk. triggered stays False,
    but the block exists and names the unconfirmed anchors — otherwise
    "why did nobody get renamed?" has no answer after the fact."""
    withheld = {
        "triggered": False,
        "suspects": {"SPEAKER_00": {"talk": {"Ada": 9.0}, "winners": ["Ada"], "avg_sim": {"Ada": 0.4}}},
        "anchors": ["Ada", "Bo"],
        "stats": {"n_total": 3, "anchor_presence": {"Ada": 3, "Bo": 0}},
        "presence_unconfirmed": ["Bo"],
    }
    *_, data = _write(tmp_path, merge_info=withheld)
    assert data["merge_reassign"]["triggered"] is False
    assert data["merge_reassign"]["presence_unconfirmed"] == ["Bo"]


def test_top_level_keys_within_allow_list(tmp_path):
    *_, data = _write(tmp_path, merge_info=TRIGGERED_MERGE_INFO)
    extra = set(data) - KNOWN_TOP_KEYS
    assert not extra, f"unclassified top-level key(s): {extra} — is it prompt-safe?"


def test_speaker_entry_keys_within_allow_list(tmp_path):
    *_, data = _write(
        tmp_path,
        id_mapping={"SPEAKER_00": "Ada"},
        id_sims={"SPEAKER_00": [("Ada", 0.9)]},
        merge_info=TRIGGERED_MERGE_INFO,
    )
    for entry in data["speakers"]:
        extra = set(entry) - KNOWN_ENTRY_KEYS
        assert not extra, f"unclassified speakers[] key(s): {extra} — is it prompt-safe?"


def test_merge_reassign_keys_within_allow_list(tmp_path):
    """The third level. Locking only the top two lets nested fields through.

    A field added inside merge_reassign carries the same risk as a top-level
    one — these values come from a whole-DB comparison — but a two-level canary
    would not see it.
    """
    *_, data = _write(tmp_path, merge_info=TRIGGERED_MERGE_INFO)
    extra = set(data["merge_reassign"]) - KNOWN_MERGE_REASSIGN_KEYS
    assert not extra, f"unclassified merge_reassign key(s): {extra} — is it prompt-safe?"


def test_input_merged_not_mutated(tmp_path):
    merged = _merged()
    _write(tmp_path, merged=merged)
    assert merged == _merged()
