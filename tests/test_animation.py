import math
import hashlib
import json

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


@pytest.mark.parametrize("motion,digest", [
    ("smooth", "9af16d73e9fb03f06ce30a00b873dff95509a17a687a8dab94620904623a7869"),
    ("hard", "83d88f00eb18c9aa0d35c322bb688007193ef4245f6c7f8e675b3ad8b5aa1ecd"),
    ("velocity", "ec9373f20c19e16c703f5b47b36372d6a78a67b166216e70c49b4d256f834149"),
])
def test_legacy_all_frame_values_match_pre_v2_baseline(motion, digest):
    # Captured from the original engine: fractional duration/anchor, BPM
    # override and unequal energies exercise every transform and interpolation.
    result = generate_keyframes(2.125, [0.21, 0.71, 1.21, 1.71], [0.8, 0.1, 1, 0.2], 137, 1.3, motion)
    encoded = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
    assert hashlib.sha256(encoded).hexdigest() == digest
