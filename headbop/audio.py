"""Local, bounded music analysis with beat, energy and spectral heuristics."""

from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

SAMPLE_RATE = 22050
HOP_LENGTH = 256
MAX_DURATION = 30.0


def _rich_features(y: np.ndarray, envelope: np.ndarray, beats: np.ndarray, duration: float) -> dict:
    """Measure features without assigning instrument or musical-genre labels.

    The energy curve is RMS at approximately 86 Hz. Silence needs at least
    350 ms below -35 dB relative to peak RMS (and an absolute noise floor).
    Spectral low/high events describe frequency emphasis, not kick/snare hits.
    """
    frame_length = 1024
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=HOP_LENGTH)[0]
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=SAMPLE_RATE, hop_length=HOP_LENGTH)
    maximum = max(float(np.max(rms)), 1e-10)
    relative = np.clip(rms / maximum, 0, 1)
    energy = [
        {"time": round(float(time), 6), "value": round(float(value), 6)}
        for time, value in zip(frame_times, relative)
        if time < duration
    ]
    energy.append({"time": round(duration, 6), "value": round(float(relative[-1]), 6)})

    # Centered analysis windows overlap by half their length. Report the
    # conservative silent region rather than clipping an audible transient.
    silent = rms < max(1e-5, maximum * 0.018)
    silences = []
    start = None
    step = HOP_LENGTH / SAMPLE_RATE
    for index in range(len(silent) + 1):
        quiet = index < len(silent) and bool(silent[index])
        if quiet and start is None:
            start = max(0.0, float(frame_times[index]) - step / 2)
        elif not quiet and start is not None:
            end = min(duration, index * step - step / 2) if index < len(silent) else duration
            if end - start >= 0.35:
                silences.append([round(start, 6), round(end, 6)])
            start = None

    onset_frames = librosa.onset.onset_detect(
        onset_envelope=envelope, sr=SAMPLE_RATE, hop_length=HOP_LENGTH,
        units="frames", backtrack=False,
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=SAMPLE_RATE, hop_length=HOP_LENGTH)
    onset_times = onset_times[onset_times < duration]
    onset_frames = onset_frames[:len(onset_times)]
    spectrum = np.abs(librosa.stft(y, n_fft=frame_length, hop_length=HOP_LENGTH)) ** 2
    frequencies = librosa.fft_frequencies(sr=SAMPLE_RATE, n_fft=frame_length)
    bands = {
        "low": np.sum(spectrum[(frequencies >= 30) & (frequencies < 220)], axis=0),
        "high": np.sum(spectrum[(frequencies >= 1800) & (frequencies <= 9000)], axis=0),
    }
    # Onset strength has a small analysis delay; inspect neighboring spectral
    # windows so brief transients are not assigned a zero band strength.
    band_hits = {
        kind: np.asarray([
            np.max(values[max(0, int(frame) - 3):min(len(values), int(frame) + 2)])
            for frame in onset_frames
        ])
        for kind, values in bands.items()
    }
    events = []
    for index, time in enumerate(onset_times):
        total = sum(float(values[index]) for values in band_hits.values())
        for kind, values in band_hits.items():
            peak = float(np.max(values)) if len(values) else 0.0
            power = float(values[index])
            if peak > 1e-10 and total > 1e-10 and power / total >= 0.28:
                events.append({
                    "time": round(float(time), 6), "kind": kind,
                    "strength": round(min(1.0, float(np.sqrt(power / peak))), 6),
                })

    # Confidence expresses evidence supporting the measured grid. It is not a
    # probability of a correct tempo: half/double-tempo ambiguity remains.
    confidence = 0.0
    if len(beats) >= 2 and len(onset_times) >= 2:
        period = float(np.median(np.diff(beats)))
        tolerance = min(0.09, period * 0.18)
        distances = np.min(np.abs(beats[:, None] - onset_times[None, :]), axis=1)
        alignment = float(np.mean(np.clip(1 - distances / max(tolerance, 1e-6), 0, 1)))
        regularity = max(0.0, 1 - float(np.std(np.diff(beats))) / max(period, 1e-6))
        support = min(1.0, len(onset_times) / 6)
        confidence = support * (0.75 * alignment + 0.25 * regularity)
    return {
        "onsets": [round(float(time), 6) for time in onset_times],
        "energy": energy,
        "silences": silences,
        "events": events,
        "rhythm_confidence": round(float(np.clip(confidence, 0, 1)), 6),
    }


def analyze_audio(path: Path) -> dict:
    """Return measured tempo, beat times and relative energy for a decoded WAV.

    Intensity is RMS in a 120 ms window around each beat, relative to the
    loudest measured beat. Beat tracking is an estimate; it is not a claim of
    score-level rhythmic accuracy. No network requests or external model files
    are used. The API decodes user formats to a bounded mono WAV first.
    """
    try:
        info = sf.info(str(path))
        duration = info.frames / info.samplerate
        if not 0.5 <= duration <= MAX_DURATION + 1 / SAMPLE_RATE:
            raise ValueError("O áudio deve ter entre 0,5 e 30 segundos.")
        samples, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except (RuntimeError, OSError, ZeroDivisionError) as exc:
        raise ValueError("Não foi possível ler o áudio. Envie um arquivo de áudio válido.") from exc

    y = np.mean(samples, axis=1)
    if not np.isfinite(y).all():
        raise ValueError("O áudio contém amostras inválidas.")
    rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
    if rms < 1e-5:
        raise ValueError("O áudio está silencioso ou tem volume insuficiente para análise.")
    if sr != SAMPLE_RATE:
        y = librosa.resample(y, orig_sr=sr, target_sr=SAMPLE_RATE)

    envelope = librosa.onset.onset_strength(y=y, sr=SAMPLE_RATE, hop_length=HOP_LENGTH)
    if not np.isfinite(envelope).all() or float(np.max(envelope)) < 1e-7:
        raise ValueError("Não foi possível identificar batidas neste áudio.")
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=envelope,
        sr=SAMPLE_RATE,
        hop_length=HOP_LENGTH,
        start_bpm=120.0,
        tightness=100,
        trim=False,
    )
    measured_bpm = float(np.asarray(tempo).reshape(-1)[0])
    if not np.isfinite(measured_bpm) or measured_bpm <= 0 or len(beat_frames) < 2:
        raise ValueError("Não foi possível estimar o ritmo. Tente um trecho com batidas mais claras.")
    while measured_bpm < 40:
        measured_bpm *= 2
    while measured_bpm > 240:
        measured_bpm /= 2

    times = librosa.frames_to_time(beat_frames, sr=SAMPLE_RATE, hop_length=HOP_LENGTH)
    times = times[(times >= 0) & (times < duration)]
    # Beat tracking can extend the grid through an intro/outro. Keep inferred
    # grid times, but give silent windows zero intensity rather than invent energy.
    half_window = round(0.06 * SAMPLE_RATE)
    energies = []
    for time in times:
        center = round(float(time) * SAMPLE_RATE)
        window = y[max(0, center - half_window) : min(len(y), center + half_window)]
        energies.append(float(np.sqrt(np.mean(window.astype(np.float64) ** 2))))
    maximum = max(energies, default=0.0)
    if maximum < 1e-5:
        raise ValueError("Não foi possível medir a intensidade das batidas neste áudio.")
    return {
        "bpm": round(measured_bpm, 3),
        "beats": [round(float(value), 6) for value in times],
        "intensity": [round(min(value / maximum, 1.0), 6) for value in energies],
        "duration": round(duration, 6),
        **_rich_features(y, envelope, times, duration),
    }
