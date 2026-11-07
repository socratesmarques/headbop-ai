import math

import pytest

from headbop.animation import cubic_bezier_ease, generate_motion


def analysis(**changes):
    source = {
        "bpm": 120, "beats": [0.5, 1, 1.5, 2, 2.5], "intensity": [1] * 5,
        "onsets": [0.5, 1, 1.5, 2, 2.5], "energy": [{"time": 0, "value": 1}, {"time": 3, "value": 1}],
        "silences": [], "events": [], "rhythm_confidence": 0.9,
    }
    source.update(changes)
    return source


def motion(source=None, **settings):
    return generate_motion(3, source if source is not None else analysis(), {"bpm": 120, **settings})


def identity(frame):
    return all(frame[key] == 0 for key in ("x", "y", "rotation")) and all(
        frame[key] == 1 for key in ("scale", "scale_x", "scale_y")
    )


def test_bezier_inverts_horizontal_curve_instead_of_using_time_as_parameter():
    # Parametric point at u=.5: x=.3125, y=.5 for handles (.25,0),(.25,1).
    assert cubic_bezier_ease(0.3125, 0.25, 0, 0.25, 1) == pytest.approx(0.5, abs=1e-6)
    assert cubic_bezier_ease(0) == 0
    assert cubic_bezier_ease(1) == 1
    assert cubic_bezier_ease(0.5) > 0.7


@pytest.mark.parametrize("fps", [24, 30, 60])
def test_sampling_has_regular_times_and_exact_final_frame(fps):
    result = generate_motion(1.213, {"beats": [0.5], "intensity": [1]}, {"fps": fps})
    assert len(result) == math.ceil(1.213 * fps) + 1
    assert result[-1]["time"] == 1.213
    assert [frame["time"] for frame in result[:-1]] == pytest.approx([i / fps for i in range(len(result) - 1)])
    assert all(a["time"] < b["time"] for a, b in zip(result, result[1:]))


@pytest.mark.parametrize("style", ["auto", "smooth", "hard", "velocity"])
def test_zero_intensity_is_identity_in_all_layers(style):
    for frame in motion(motion=style, intensity=0):
        assert identity(frame)
        assert identity(frame["layers"]["body"])
        assert identity(frame["layers"]["head"])


def test_empty_analysis_does_not_invent_motion():
    assert all(identity(frame) for frame in motion({}))


@pytest.mark.parametrize("style", ["auto", "smooth", "hard", "velocity"])
def test_anticipation_impact_and_recovery_land_on_irregular_source_beats(style):
    source = analysis(beats=[0.5, 1.1, 1.65, 2.3], intensity=[1] * 4, onsets=[])
    result = motion(source, fps=60, motion=style)
    for beat in source["beats"]:
        impact = round(beat * 60)
        assert result[impact]["y"] > 0.025
        assert result[impact]["y"] > result[impact - 1]["y"]
        assert result[impact]["y"] > result[impact + 1]["y"]
        assert any(frame["y"] < 0 for frame in result[max(0, impact - 10):impact])
        assert any(frame["y"] < 0 for frame in result[impact + 1:impact + 23])
    assert identity(result[0])
    assert identity(result[-1])


@pytest.mark.parametrize("style", ["smooth", "hard", "velocity"])
def test_continuity_across_gesture_and_beat_boundaries(style):
    result = motion(motion=style, fps=60)
    # A position reset would jump close to the full 0.032 beat amplitude.
    assert max(abs(a["y"] - b["y"]) for a, b in zip(result, result[1:])) < 0.021
    assert max(abs(a["rotation"] - b["rotation"]) for a, b in zip(result, result[1:])) < 3.5
    for key in ("x", "y", "rotation", "scale", "scale_x", "scale_y"):
        assert all(math.isfinite(frame[key]) for frame in result)


def test_silence_suppresses_extrapolated_grid_and_recovers_at_return():
    result = motion(analysis(silences=[[0.7, 2.3]]), fps=60)
    for frame in result:
        if 0.83 <= frame["time"] <= 2.17:
            assert identity(frame)
            assert identity(frame["layers"]["body"])
            assert identity(frame["layers"]["head"])
    assert result[150]["y"] > 0.025
    assert max(abs(a["y"] - b["y"]) for a, b in zip(result, result[1:])) < 0.02


def test_speed_changes_gesture_width_while_preserving_impact_times():
    source = analysis(beats=[1.5], intensity=[1], onsets=[])
    slow = motion(source, speed=0.5, fps=60)
    fast = motion(source, speed=2, fps=60)
    assert max(slow, key=lambda frame: frame["y"])["time"] == 1.5
    assert max(fast, key=lambda frame: frame["y"])["time"] == 1.5
    assert sum(not identity(frame) for frame in slow) > sum(not identity(frame) for frame in fast)


def test_bpm_rounding_preserves_beats_but_override_changes_grid():
    source = analysis(bpm=119.7, beats=[0.5, 1.1, 1.65, 2.3], intensity=[1] * 4, onsets=[])
    rounded = motion(source, bpm=120, fps=60)
    overridden = motion(source, bpm=60, fps=60)
    assert rounded[66]["y"] > rounded[65]["y"]
    assert rounded[66]["y"] > rounded[67]["y"]
    assert overridden[90]["y"] > 0.025
    assert overridden[66]["y"] < 0.01


def test_continuous_energy_and_beat_energy_control_amplitude():
    loud = motion()
    quiet = motion(analysis(energy=[{"time": 0, "value": 0.1}, {"time": 3, "value": 0.1}]))
    absent = motion(analysis(intensity=[0] * 5))
    assert max(frame["y"] for frame in quiet) < 0.6 * max(frame["y"] for frame in loud)
    assert all(identity(frame) for frame in absent)


def test_spectral_events_accent_corresponding_layers_without_instrument_labels():
    low = motion(analysis(events=[{"time": 1, "kind": "low", "strength": 1}]))[24]
    high = motion(analysis(events=[{"time": 1, "kind": "high", "strength": 1}]))[24]
    assert low["layers"]["body"]["y"] > high["layers"]["body"]["y"]
    assert high["layers"]["head"]["y"] > low["layers"]["head"]["y"]


def test_head_is_relative_body_follows_through_and_squash_preserves_area():
    result = motion(fps=60, squash=0.8)
    frame = result[60]
    assert frame["layers"]["head"]["rotation"] != frame["rotation"]
    assert frame["layers"]["body"]["rotation"] != frame["layers"]["head"]["rotation"]
    assert frame["layers"]["body"]["y"] < result[61]["layers"]["body"]["y"]
    for transform in (frame, *frame["layers"].values()):
        assert transform["scale_x"] > 1
        assert transform["scale_y"] < 1
        assert transform["scale_x"] * transform["scale_y"] == pytest.approx(1, abs=2e-8)
    assert all(frame["scale_x"] == frame["scale_y"] == 1 for frame in motion(squash=0))


def test_easing_and_auto_profiles_are_deterministic_and_distinct():
    assert motion() == motion()
    assert motion(easing="linear") != motion(easing="ease-in-out") != motion(easing="natural")
    assert motion(analysis(rhythm_confidence=0.1)) != motion(analysis(rhythm_confidence=0.9))


@pytest.mark.parametrize("settings", [
    {"fps": 25}, {"speed": 0.1}, {"speed": float("nan")}, {"squash": -0.1},
    {"intensity": True}, {"easing": "unknown"}, {"motion": "reference"}, {"bpm": 300},
])
def test_invalid_v2_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        motion(**settings)


@pytest.mark.parametrize("change", [
    {"energy": [{"time": 0, "value": float("nan")}]},
    {"silences": [[2, 1]]}, {"onsets": [1, 0.5]}, {"rhythm_confidence": 3},
    {"events": [{"time": 1, "kind": "kick", "strength": 1}]},
    {"beats": [0.5, 1], "intensity": [1]},
])
def test_invalid_features_are_rejected(change):
    with pytest.raises(ValueError):
        motion(analysis(**change))
