import numpy as np
import pytest
import soundfile as sf

from headbop.audio import analyze_audio

SR = 22050


def click_track(path, bpm=120, duration=8, dynamics=False):
    samples = np.zeros(round(duration * SR), dtype=np.float32)
    count = 0
    for time in np.arange(0.5, duration - 0.05, 60 / bpm):
        start = round(time * SR)
        length = round(0.035 * SR)
        pulse = np.random.default_rng(count).normal(size=length) * np.exp(-np.arange(length) / (SR * 0.008))
        amplitude = 0.7 if not dynamics or count % 2 == 0 else 0.15
        samples[start : start + length] += (amplitude * pulse).astype(np.float32)
        count += 1
    sf.write(path, samples, SR, subtype="FLOAT")
    return samples


@pytest.mark.parametrize("bpm", [90, 120, 150])
def test_detect_regular_beat_and_tempo(tmp_path, bpm):
    path = tmp_path / "beats.wav"
    click_track(path, bpm=bpm, duration=10)
    result = analyze_audio(path)
    assert result["bpm"] == pytest.approx(bpm, rel=0.045)
    assert result["duration"] == 10
    assert len(result["beats"]) >= 8
    assert len(result["beats"]) == len(result["intensity"])
    assert all(0 <= value <= 1 for value in result["intensity"])
    assert np.median(np.diff(result["beats"])) == pytest.approx(60 / bpm, abs=0.025)
    # Ignore the tracker extrapolated boundary and check alignment to real clicks.
    interior = [t for t in result["beats"] if 0.7 < t < 9]
    assert np.median([abs((t - 0.5 + 30 / bpm) % (60 / bpm) - 30 / bpm) for t in interior]) < 0.06


def test_intensity_reflects_unequal_pulse_energy(tmp_path):
    path = tmp_path / "dynamic.wav"
    click_track(path, dynamics=True)
    result = analyze_audio(path)
    values = [energy for t, energy in zip(result["beats"], result["intensity"]) if 1 < t < 7]
    assert max(values) > 0.8
    assert min(values) < 0.4


@pytest.mark.parametrize("duration", [0.2, 31])
def test_reject_duration(tmp_path, duration):
    path = tmp_path / "length.wav"
    sf.write(path, np.ones(round(SR * duration)) * 0.1, SR)
    with pytest.raises(ValueError, match="0,5 e 30"):
        analyze_audio(path)


def test_reject_silence_and_invalid_file(tmp_path):
    path = tmp_path / "silence.wav"
    sf.write(path, np.zeros(SR), SR)
    with pytest.raises(ValueError, match="silencioso"):
        analyze_audio(path)
    path.write_bytes(b"not audio")
    with pytest.raises(ValueError, match="ler o áudio"):
        analyze_audio(path)


def test_reject_nonfinite_samples(tmp_path):
    path = tmp_path / "nan.wav"
    sf.write(path, np.full(SR, float("nan")), SR, subtype="FLOAT")
    with pytest.raises(ValueError, match="inválidas"):
        analyze_audio(path)


def test_continuous_energy_onsets_silence_and_confidence(tmp_path):
    path = tmp_path / "pause.wav"
    samples = click_track(path, duration=8)
    samples[3 * SR:5 * SR] = 0
    sf.write(path, samples, SR, subtype="FLOAT")
    result = analyze_audio(path)
    energy = result["energy"]
    assert energy[0]["time"] == 0
    assert energy[-1]["time"] == 8
    assert len(energy) > 8 * 60
    assert all(a["time"] < b["time"] for a, b in zip(energy, energy[1:]))
    assert all(0 <= point["value"] <= 1 for point in energy)
    assert max(point["value"] for point in energy) == pytest.approx(1)
    assert all(point["value"] == 0 for point in energy if 3.1 < point["time"] < 4.9)
    assert any(start < 3.1 and end > 4.9 for start, end in result["silences"])
    assert all(0 <= start < end <= 8 for start, end in result["silences"])
    assert 0.35 < result["rhythm_confidence"] <= 1
    for onset in (0.5, 1, 1.5, 2, 2.5, 5, 5.5, 6):
        assert min(abs(detected - onset) for detected in result["onsets"]) < 0.06
    assert all(not 3.1 < onset < 4.9 for onset in result["onsets"])


def test_spectral_events_distinguish_low_high_frequency_emphasis(tmp_path):
    samples = np.zeros(SR * 8, dtype=np.float32)
    length = round(0.10 * SR)
    time = np.arange(length) / SR
    for index, onset in enumerate(np.arange(0.5, 7.8, 0.5)):
        frequency = 100 if index % 2 == 0 else 4400
        pulse = np.sin(2 * np.pi * frequency * time) * np.exp(-time / 0.02)
        start = round(onset * SR)
        samples[start:start + length] = pulse
    path = tmp_path / "bands.wav"
    sf.write(path, samples, SR)
    result = analyze_audio(path)
    assert result["events"]
    assert {event["kind"] for event in result["events"]} == {"low", "high"}
    assert all(0 <= event["strength"] <= 1 for event in result["events"])
    for index, onset in enumerate(np.arange(1, 7.5, 0.5), start=1):
        expected = "low" if index % 2 == 0 else "high"
        nearby = [event for event in result["events"] if abs(event["time"] - onset) < 0.06]
        assert nearby and all(event["kind"] == expected for event in nearby)


def test_resampling_keeps_features_in_source_time(tmp_path):
    path = tmp_path / "resampled.wav"
    samples = click_track(path, duration=5)
    sf.write(path, np.repeat(samples, 2), SR * 2)
    result = analyze_audio(path)
    assert result["duration"] == 5
    assert result["energy"][-1]["time"] == 5
    assert all(0 <= time < 5 for time in result["onsets"])
    assert result["bpm"] == pytest.approx(120, rel=0.045)
