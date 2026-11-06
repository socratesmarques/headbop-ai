"""Deterministic sampled transforms shared by project export and video rendering."""

from bisect import bisect_right
import math

FPS = 24
MOTIONS = frozenset({"smooth", "hard", "velocity"})


def _finite(value: float) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def generate_keyframes(
    duration: float,
    beats: list[float],
    intensity: list[float],
    bpm: float,
    strength: float,
    motion: str,
) -> list[dict]:
    """Generate 24 fps transforms and an exact end frame.

    BPM overrides the regular beat interval, retaining the first detected beat
    as the anchor. Per-beat energy is interpolated in source time. ``smooth``
    uses rounded cosine pulses; ``hard`` has a sharp attack and linear release;
    ``velocity`` uses a damped oscillation with a small recoil. x/y are fractions
    of canvas dimensions and positive rotation is clockwise in the renderer.
    """
    if not _finite(duration) or not 0 < duration <= 30.00005:
        raise ValueError("A duração deve ser positiva e de no máximo 30 segundos.")
    if not _finite(bpm) or not 40 <= bpm <= 240:
        raise ValueError("O BPM deve estar entre 40 e 240.")
    if not _finite(strength) or not 0 <= strength <= 2:
        raise ValueError("A intensidade deve estar entre 0 e 2.")
    if motion not in MOTIONS:
        raise ValueError("Modo de movimento inválido.")
    if len(beats) != len(intensity):
        raise ValueError("Cada batida precisa de uma intensidade correspondente.")
    if any(not _finite(t) or not 0 <= t <= duration for t in beats):
        raise ValueError("Tempo de batida inválido.")
    if any(beats[i] >= beats[i + 1] for i in range(len(beats) - 1)):
        raise ValueError("As batidas devem estar em ordem crescente e sem repetições.")
    if any(not _finite(value) or not 0 <= value <= 1 for value in intensity):
        raise ValueError("Intensidade de batida inválida.")

    anchor = beats[0] if beats else 0.0
    period = 60.0 / bpm
    sample_times = [index / FPS for index in range(math.ceil(duration * FPS))]
    sample_times.append(duration)
    frames = []
    for time in sample_times:
        if not beats:
            energy = 1.0
        else:
            right = bisect_right(beats, time)
            if right == 0:
                energy = intensity[0]
            elif right == len(beats):
                energy = intensity[-1]
            else:
                left = right - 1
                mix = (time - beats[left]) / (beats[right] - beats[left])
                energy = intensity[left] * (1 - mix) + intensity[right] * mix
        cycles = (time - anchor) / period
        cycle_index = math.floor(cycles)
        phase = cycles - cycle_index
        direction = 1 if cycle_index % 2 == 0 else -1

        if motion == "smooth":
            pulse = (1 + math.cos(2 * math.pi * cycles)) / 2
            # Anticipate only the first half-beat; avoid motion in a long intro.
            if cycles < -0.5:
                pulse = 0.0
            rotation = math.cos(math.pi * cycles) * pulse
        elif motion == "hard":
            pulse = max(0.0, 1 - phase / 0.32) if cycles >= 0 else 0.0
            rotation = direction * pulse
        else:
            # Fast attack, changing return speed and damped overshoot.
            pulse = (1 + 7 * phase) * math.exp(-7 * phase) * math.cos(9 * phase)
            if cycles < 0:
                pulse = 0.0
            rotation = direction * pulse
        amplitude = strength * energy
        frames.append(
            {
                "time": round(time, 8),
                "x": round(0.012 * amplitude * rotation, 8),
                "y": round(-0.045 * amplitude * pulse, 8),
                "rotation": round(5 * amplitude * rotation, 8),
                "scale": round(1 + 0.025 * amplitude * max(0.0, pulse), 8),
            }
        )
    return frames
