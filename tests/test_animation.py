import math

import pytest

from headbop.animation import generate_keyframes


def frames(**overrides):
    arguments = dict(duration=2, beats=[0, 0.5, 1, 1.5], intensity=[1, 0.5, 1, 0.5], bpm=120, strength=1, motion="smooth")
    arguments.update(overrides)
    return generate_keyframes(**arguments)


def test_exact_regular_timing_and_end():
    result = frames(duration=1.23, beats=[0, 0.5, 1], intensity=[1, 1, 1])
    assert len(result) == math.ceil(1.23 * 24) + 1
    assert result[0]["time"] == 0
    assert result[-1]["time"] == 1.23
    assert all(a["time"] < b["time"] for a, b in zip(result, result[1:]))
    assert result[7]["time"] == pytest.approx(7 / 24)


@pytest.mark.parametrize("motion", ["smooth", "hard", "velocity"])
def test_zero_strength_is_identity(motion):
    assert all(frame["x"] == frame["y"] == frame["rotation"] == 0 and frame["scale"] == 1 for frame in frames(strength=0, motion=motion))


def test_modes_differ_and_velocity_has_recoil():
    results = [frames(motion=motion) for motion in ["smooth", "hard", "velocity"]]
    assert results[0] != results[1] != results[2]
    assert any(frame["y"] > 0 for frame in results[2])
    assert all(frame["y"] <= 0 for frame in results[0])
    assert frames() == frames()


def test_bpm_changes_beat_spacing_and_strength_scales_motion():
    constant = dict(beats=[0], intensity=[1], motion="hard")
    slow, fast = frames(bpm=60, **constant), frames(bpm=120, **constant)
    assert slow[12]["y"] == 0
    assert fast[12]["y"] < 0
    assert frames(strength=2)[0]["y"] == 2 * frames()[0]["y"]


def test_source_energy_controls_movement():
    result = frames(motion="hard", intensity=[1, 0.5, 0, 1])
    assert result[0]["y"] == pytest.approx(-0.045)
    assert result[12]["y"] == pytest.approx(-0.0225)
    assert result[24]["y"] == 0


@pytest.mark.parametrize("override", [
    {"duration": float("nan")}, {"duration": 31}, {"bpm": 0}, {"strength": -1},
    {"strength": float("inf")}, {"motion": "unknown"}, {"beats": [0.5, 0]},
    {"intensity": [1]}, {"beats": [0, 0, 1, 1.5]}, {"intensity": [1, -1, 1, 1]},
])
def test_reject_invalid_inputs(override):
    with pytest.raises(ValueError):
        frames(**override)
