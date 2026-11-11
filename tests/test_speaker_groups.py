"""Group tags on fingerprints — a display, never a gate.

Two directions have to hold at once, and checking only one makes the whole file
worthless:

  1. it reports the shape that actually goes wrong — someone from another
     recurring group winning a match in this recording
  2. it stays quiet for the cases that look the same but are fine — the person
     who attends everything, speakers with no tags yet, recordings with no pack

🔴 And it must not change identification. The tempting design is to raise the
similarity threshold when groups differ; measured on real data that would have
dropped correct matches, because cross-group attendance is common (people join a
second group, guests visit, the organizer is everywhere) and the score ranges
overlap. Tags also go stale silently. A stale tag that prints costs a line; a
stale tag that blocks costs a correct answer with no trace.
"""
from __future__ import annotations

import json

from moim_scribe.speakers.groups import (
    check_stats_file,
    format_note,
    group_map,
    off_group_matches,
)

INDEX = {
    "Ari": {"groups": ["book-club"], "total_sec": 400},
    "Bo": {"groups": ["book-club"], "total_sec": 300},
    "Cam": {"groups": ["standup"], "total_sec": 300},
    "Dae": {"groups": ["book-club", "standup"], "total_sec": 300},  # in both
    # 🔴 Eun carries a home group *and* attends_all. Without the group, dropping
    # the attends_all check would still pass every test here — the "untagged
    # speaker" branch would quietly cover for it, and a mutation proved it did.
    "Eun": {"groups": ["standup"], "attends_all": True, "total_sec": 900},
    "Fern": {"total_sec": 120},                                     # not tagged yet
    # Plain outsiders, no second group and no attends_all. They exist so that a
    # test about *some other* filter cannot be satisfied by the group check
    # rejecting them anyway.
    "Gil": {"groups": ["standup"], "total_sec": 200},
    "Hana": {"groups": ["standup"], "total_sec": 200},
}


class TestReportsTheRealShape:
    def test_outsider_is_reported(self):
        off = off_group_matches("book-club", ["Ari", "Cam"], INDEX)
        assert [o["name"] for o in off] == ["Cam"]
        assert "standup" in format_note(off)
        assert "book-club" in format_note(off)

    def test_several_outsiders_are_all_named(self):
        off = off_group_matches("standup", ["Ari", "Bo", "Cam"], INDEX)
        assert {o["name"] for o in off} == {"Ari", "Bo"}
        assert "2 speaker(s)" in format_note(off)


class TestStaysQuietWhenItShould:
    def test_member_of_this_group(self):
        assert off_group_matches("book-club", ["Ari", "Bo"], INDEX) == []

    def test_member_of_two_groups(self):
        assert off_group_matches("book-club", ["Dae"], INDEX) == []
        assert off_group_matches("standup", ["Dae"], INDEX) == []

    def test_person_who_attends_everything(self):
        assert off_group_matches("book-club", ["Eun"], INDEX) == []
        assert off_group_matches("standup", ["Eun"], INDEX) == []

    def test_untagged_speaker_is_not_suspected(self):
        assert off_group_matches("book-club", ["Fern"], INDEX) == []

    def test_no_pack_matched_skips_the_check(self):
        """An unknown group is not a suspicion. Reporting here would put a warning
        on every uncategorized recording until the warning means nothing."""
        assert off_group_matches(None, ["Cam"], INDEX) == []
        assert off_group_matches("", ["Cam"], INDEX) == []

    def test_empty_index_says_nothing(self):
        assert off_group_matches("book-club", ["Cam"], {}) == []

    def test_format_note_empty_for_nothing(self):
        assert format_note([]) == ""


class TestGroupMap:
    def test_splits_tags_from_attends_all(self):
        groups, always = group_map(INDEX)
        assert groups["Dae"] == ["book-club", "standup"]
        assert groups["Eun"] == ["standup"]   # a home group does not cancel attends_all
        assert always == {"Eun"}

    def test_survives_a_malformed_entry(self):
        groups, always = group_map({"X": "not-a-dict", "Y": {"groups": ["g"]}})
        assert groups == {"Y": ["g"]}
        assert always == set()


class TestStatsFile:
    def _write(self, tmp_path, speakers):
        p = tmp_path / "speakers.json"
        p.write_text(json.dumps({"speakers": speakers}), encoding="utf-8")
        return p

    def test_reads_confident_matches_only(self, tmp_path):
        """Unidentified clusters have no name to check, and a label with no score
        was set by hand — neither is a fingerprint match, so neither is this
        check's business.

        🔴 The two ignored entries are *plain* outsiders — not someone who is in
        both groups, not someone marked attends_all. Either of those would be
        rejected by the group check regardless, so the filter could be deleted
        with every test still green. A mutation showed exactly that on the first
        version of this test.
        """
        p = self._write(tmp_path, [
            {"label": "Cam", "identified": True, "match_score": 0.71},
            {"label": "Gil", "identified": False, "match_score": None},
            {"label": "Hana", "identified": True, "match_score": None},
        ])
        note = check_stats_file(p, "book-club", INDEX)
        assert "Cam" in note
        assert "Gil" not in note
        assert "Hana" not in note

    def test_missing_file_is_silent(self, tmp_path):
        assert check_stats_file(tmp_path / "nope.json", "book-club", INDEX) == ""

    def test_broken_json_is_silent(self, tmp_path):
        p = tmp_path / "speakers.json"
        p.write_text("{not json", encoding="utf-8")
        assert check_stats_file(p, "book-club", INDEX) == ""


class TestEnrollTagging:
    """`enroll --group` writes the tag; enrolling is still the part that matters."""

    def _run(self, tmp_path, monkeypatch, *, group=None, attends_all=False,
             names=("Ari",), preexisting=True):
        import argparse

        from moim_scribe import cli
        from moim_scribe.speakers.db import SpeakerDB

        # Every command except init refuses a workspace without config.yaml.
        (tmp_path / "config.yaml").touch()
        cfg = cli._cfg(str(tmp_path))
        db = SpeakerDB(cfg.speakers_dir)
        if preexisting:
            db.save_index({n: {"total_sec": 100, "n_samples": 1} for n in names})

        monkeypatch.setattr("moim_scribe.speakers.enroll.enroll",
                            lambda *a, **k: {"enrolled": list(names), "refused": []})
        args = argparse.Namespace(
            workspace=str(tmp_path), audio="a.m4a", jsonl="a.jsonl",
            mapping=[f"SPEAKER_0{i}={n}" for i, n in enumerate(names)],
            group=group, attends_all=attends_all,
            allow_duplicate=False, force=False)
        assert cli.cmd_enroll(args) == 0
        return db.load_index()

    def test_group_is_written(self, tmp_path, monkeypatch):
        index = self._run(tmp_path, monkeypatch, group="book-club")
        assert index["Ari"]["groups"] == ["book-club"]

    def test_same_group_twice_does_not_duplicate(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch, group="book-club")
        index = self._run(tmp_path, monkeypatch, group="book-club", preexisting=False)
        assert index["Ari"]["groups"] == ["book-club"]

    def test_second_group_is_added_not_replaced(self, tmp_path, monkeypatch):
        """People do join a second group — that is the case the tag exists for."""
        self._run(tmp_path, monkeypatch, group="book-club")
        index = self._run(tmp_path, monkeypatch, group="standup", preexisting=False)
        assert index["Ari"]["groups"] == ["book-club", "standup"]

    def test_attends_all_flag(self, tmp_path, monkeypatch):
        index = self._run(tmp_path, monkeypatch, attends_all=True)
        assert index["Ari"]["attends_all"] is True

    def test_no_flags_leaves_the_index_alone(self, tmp_path, monkeypatch):
        index = self._run(tmp_path, monkeypatch)
        assert "groups" not in index["Ari"]
        assert "attends_all" not in index["Ari"]

    def test_enrollment_that_produced_no_entry_is_skipped(self, tmp_path, monkeypatch):
        """enroll() is stubbed here, so a name it never wrote must not be invented
        by the tagging step — tags describe enrolled speakers, nothing else."""
        index = self._run(tmp_path, monkeypatch, group="book-club", preexisting=False)
        assert index == {}

    def test_refused_name_is_not_tagged_and_exit_is_nonzero(self, tmp_path, monkeypatch):
        """A guard refused the pair: tagging an existing entry anyway would
        record a group for an enrollment that never happened."""
        import argparse

        from moim_scribe import cli
        from moim_scribe.speakers.db import SpeakerDB

        # Every command except init refuses a workspace without config.yaml.
        (tmp_path / "config.yaml").touch()
        cfg = cli._cfg(str(tmp_path))
        db = SpeakerDB(cfg.speakers_dir)
        db.save_index({"Ari": {"total_sec": 100, "n_samples": 1}})
        monkeypatch.setattr("moim_scribe.speakers.enroll.enroll", lambda *a, **k: {
            "enrolled": [], "refused": [{"label": "SPEAKER_00", "name": "Ari"}]})
        args = argparse.Namespace(
            workspace=str(tmp_path), audio="a.m4a", jsonl="a.jsonl",
            mapping=["SPEAKER_00=Ari"], group="book-club", attends_all=False,
            allow_duplicate=False, force=False)
        assert cli.cmd_enroll(args) == 1
        assert "groups" not in db.load_index()["Ari"]
