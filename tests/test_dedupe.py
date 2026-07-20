"""dedupe_repeated_segments: deterministic whisper repetition-loop collapse."""
from moim_scribe.diarize import dedupe_repeated_segments


def test_empty_input():
    out, removed = dedupe_repeated_segments([])
    assert out == [] and removed == 0


def test_no_repeats_pass_through():
    segs = [(0.0, 1.0, "a"), (1.0, 2.0, "b"), (2.0, 3.0, "c")]
    out, removed = dedupe_repeated_segments(segs)
    assert out == segs and removed == 0


def test_two_repeats_are_kept():
    segs = [(0.0, 1.0, "same"), (1.0, 2.0, "same"), (2.0, 3.0, "other")]
    out, removed = dedupe_repeated_segments(segs)
    assert out == segs and removed == 0


def test_irregular_run_collapses_to_keep():
    # 5 identical lines at irregular intervals -> keep the first 2
    segs = [(0.0, 1.0, "loop"), (1.7, 2.0, "loop"), (4.0, 5.0, "loop"),
            (9.0, 10.0, "loop"), (15.0, 16.0, "loop"), (20.0, 21.0, "end")]
    out, removed = dedupe_repeated_segments(segs)
    assert removed == 3
    assert [s[2] for s in out] == ["loop", "loop", "end"]


def test_uniform_run_collapses_harder():
    # exactly 1.0s intervals — the machine-regular signature -> keep only 1
    segs = [(float(i), float(i) + 0.8, "tick") for i in range(6)] + [(10.0, 11.0, "end")]
    out, removed = dedupe_repeated_segments(segs)
    assert removed == 5
    assert [s[2] for s in out] == ["tick", "end"]


def test_input_not_mutated():
    segs = [(float(i), float(i) + 0.5, "x") for i in range(4)]
    snapshot = list(segs)
    dedupe_repeated_segments(segs)
    assert segs == snapshot


def test_empty_text_runs_not_collapsed():
    segs = [(0.0, 1.0, ""), (1.0, 2.0, ""), (2.0, 3.0, ""), (3.0, 4.0, "")]
    out, removed = dedupe_repeated_segments(segs)
    assert removed == 0 and len(out) == 4
