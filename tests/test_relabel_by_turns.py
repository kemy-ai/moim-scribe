"""The overlap mapping that lets one engine's text carry another's speakers.

Two properties carry the whole function, and both are easy to lose in a
"simplification" that still passes a naive test:

  - **maximum overlap, not start-time lookup.** Segments and turns are cut at
    different places, so a segment that straddles a boundary belongs to whoever
    holds more of it — not to whoever happened to be speaking at its first
    sample. Getting this wrong shifts speech one speaker backwards at every
    turn change, which reads as plausible dialogue and is entirely wrong.
  - **gaps keep their existing label.** A segment overlapping no turn at all is
    left alone rather than assigned to the nearest speaker. Unclassified is a
    truthful answer; a confident wrong one is not.
"""
from moim_scribe.diarize import relabel_by_turns


def seg(start, end, speaker="SPEAKER_00", text="x"):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


def test_a_straddling_segment_goes_to_whoever_holds_more_of_it():
    """The start-time bug in its purest form: the segment begins inside A's
    turn but spends 8 of its 10 seconds inside B's."""
    merged = [seg(9.0, 19.0)]
    turns = [(0.0, 10.0, "A"), (10.0, 30.0, "B")]

    out, stats = relabel_by_turns(merged, turns)

    assert out[0]["speaker"] == "B"
    assert stats["n_relabeled"] == 1


def test_a_segment_in_a_gap_between_turns_keeps_its_label():
    merged = [seg(12.0, 14.0, speaker="SPEAKER_07")]
    turns = [(0.0, 10.0, "A"), (20.0, 30.0, "B")]

    out, stats = relabel_by_turns(merged, turns)

    assert out[0]["speaker"] == "SPEAKER_07"
    assert stats == {"n_relabeled": 0, "n_kept": 1, "n_turn_speakers": 2}


def test_unsorted_turns_still_map_correctly():
    """The loop breaks early on the assumption that turns are time-ordered, and
    that ordering is an implementation detail of whatever produced them. The
    function sorts rather than trusting it; without the sort, the early break
    silently drops the later-but-listed-first turn."""
    merged = [seg(21.0, 29.0)]
    turns = [(20.0, 30.0, "B"), (0.0, 10.0, "A")]  # out of order on purpose

    out, _ = relabel_by_turns(merged, turns)

    assert out[0]["speaker"] == "B"


def test_no_turns_returns_the_input_unchanged():
    merged = [seg(0.0, 5.0, speaker="SPEAKER_03")]

    out, stats = relabel_by_turns(merged, [])

    assert out[0]["speaker"] == "SPEAKER_03"
    assert stats == {"n_relabeled": 0, "n_kept": 1, "n_turn_speakers": 0}


def test_the_input_list_is_not_mutated():
    """Callers keep a snapshot of the original labels for merge detection; a
    mutating relabel would rewrite that snapshot out from under them."""
    original = seg(1.0, 9.0, speaker="SPEAKER_00")
    merged = [original]

    out, _ = relabel_by_turns(merged, [(0.0, 10.0, "A")])

    assert original["speaker"] == "SPEAKER_00"
    assert out[0] is not original
    assert out[0]["speaker"] == "A"


def test_text_and_timings_survive_the_relabel():
    merged = [seg(1.0, 9.0, text="hello there")]

    out, _ = relabel_by_turns(merged, [(0.0, 10.0, "A")])

    assert out[0]["text"] == "hello there"
    assert (out[0]["start"], out[0]["end"]) == (1.0, 9.0)


def test_stats_count_speakers_not_turns():
    """One speaker taking several turns is still one speaker — the count is
    what the log reports as "N speakers from pyannote"."""
    merged = [seg(1.0, 2.0), seg(11.0, 12.0), seg(21.0, 22.0)]
    turns = [(0.0, 5.0, "A"), (10.0, 15.0, "B"), (20.0, 25.0, "A")]

    _, stats = relabel_by_turns(merged, turns)

    assert stats["n_turn_speakers"] == 2
    assert stats["n_relabeled"] == 3


def test_a_segment_spanning_several_turns_takes_the_largest():
    merged = [seg(0.0, 30.0)]
    turns = [(0.0, 5.0, "A"), (5.0, 25.0, "B"), (25.0, 30.0, "C")]

    out, _ = relabel_by_turns(merged, turns)

    assert out[0]["speaker"] == "B"


def test_touching_turns_do_not_count_as_overlap():
    """A turn ending exactly where the segment starts shares no time with it.
    Counted as overlap, zero-length contact would win ties against real
    overlap and reintroduce the backwards shift."""
    merged = [seg(10.0, 20.0)]
    turns = [(0.0, 10.0, "A"), (10.0, 20.0, "B")]

    out, _ = relabel_by_turns(merged, turns)

    assert out[0]["speaker"] == "B"
