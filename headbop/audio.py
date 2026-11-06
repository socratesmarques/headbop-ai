"""Local, bounded music analysis using Librosa and per-beat RMS energy."""

from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

SAMPLE_RATE = 22050
HOP_LENGTH = 256
MAX_DURATION = 30.0


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
    }
