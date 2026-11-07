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


def cubic_bezier_ease(value: float, x1: float = 0.22, y1: float = 0.0,
                      x2: float = 0.28, y2: float = 1.0) -> float:
    """Evaluate CSS-style cubic Bezier easing by inverting its x coordinate.

    Evaluating y directly at ``value`` is incorrect when the horizontal handles
    differ. Bisection also works for flat endpoint tangents without division.
    """
    value = min(1.0, max(0.0, value))
    if value in (0.0, 1.0):
        return value
    lower, upper = 0.0, 1.0
    for _ in range(24):
        parameter = (lower + upper) / 2
        other = 1 - parameter
        x = 3 * other * other * parameter * x1 + 3 * other * parameter * parameter * x2 + parameter ** 3
        if x < value:
            lower = parameter
        else:
            upper = parameter
    parameter = (lower + upper) / 2
    other = 1 - parameter
    return 3 * other * other * parameter * y1 + 3 * other * parameter * parameter * y2 + parameter ** 3


def _ease(value: float, easing: str) -> float:
    if easing == "linear":
        return max(0.0, min(1.0, value))
    if easing == "ease-in-out":
        return cubic_bezier_ease(value, 0.42, 0.0, 0.58, 1.0)
    return cubic_bezier_ease(value)


# Phase zero is the audible impact. Every segment joins at the same position;
# nonlinear easing has zero endpoint velocity, including the neutral boundary.
_GESTURES = {
    "smooth": ((-0.34, 0.0), (-0.20, -0.15), (0.0, 1.0), (0.36, -0.12), (0.74, 0.0)),
    "hard": ((-0.25, 0.0), (-0.14, -0.22), (0.0, 1.0), (0.18, -0.24), (0.36, 0.07), (0.60, 0.0)),
    "velocity": ((-0.32, 0.0), (-0.18, -0.28), (0.0, 1.0), (0.23, -0.40), (0.47, 0.15), (0.82, 0.0)),
}


def _gesture(phase: float, motion: str, easing: str) -> float:
    points = _GESTURES[motion]
    if phase <= points[0][0] or phase >= points[-1][0]:
        return 0.0
    for (left, first), (right, last) in zip(points, points[1:]):
        if phase <= right:
            progress = _ease((phase - left) / (right - left), easing)
            return first + (last - first) * progress
    return 0.0


def _interpolate(times: list[float], values: list[float], time: float, default: float = 1.0) -> float:
    if not times:
        return default
    right = bisect_right(times, time)
    if right == 0:
        return values[0]
    if right == len(times):
        return values[-1]
    left = right - 1
    weight = (time - times[left]) / (times[right] - times[left])
    return values[left] * (1 - weight) + values[right] * weight


def _silence_gain(time: float, silences: list, transition: float = 0.12) -> float:
    """Ease to rest inside a quiet interval, anticipating its audible return."""
    for start, end in silences:
        if start <= time <= end:
            width = min(transition, (end - start) / 2)
            if width <= 0:
                continue
            distance = min(time - start, end - time)
            progress = max(0.0, min(1.0, distance / width))
            return 1 - progress * progress * (3 - 2 * progress)
    return 1.0


def _transform(x: float, y: float, rotation: float, pulse: float, squash: float,
               scale_amount: float = 0.008) -> dict:
    # Directional multipliers are separate from uniform scale. Their product
    # stays one so squash/stretch does not also change the apparent volume.
    stretch = max(0.65, min(1.35, 1 + squash * pulse))
    return {
        "x": round(x, 8), "y": round(y, 8), "rotation": round(rotation, 8),
        "scale": round(1 + scale_amount * max(0.0, pulse), 8),
        "scale_x": round(stretch, 8), "scale_y": round(1 / stretch, 8),
    }


def generate_motion(duration: float, analysis: dict, settings, rig=None) -> list[dict]:
    """Generate v2 motion at 24/30/60 fps, keeping impact peaks on audio beats.

    ``settings`` accepts a mapping or a Pydantic settings model. ``speed`` shapes
    gesture duration around each beat; only a BPM override changes the beat grid.
    Sparse onsets and spectral low/high events add restrained body/head accents.
    They do not identify instruments. Quiet passages ease to a stationary pose.

    Top-level transforms animate a whole image or a single subject. Layer body
    transforms are global; head transforms are relative (body @ head). x/y use
    full canvas dimensions, and scale_x/scale_y multiply uniform scale. The rig
    supplies pivots to the renderer; its presence does not change musical timing.
    Rotation follows OpenCV's positive-angle convention in the v2 renderer.
    The legacy function above is deliberately independent and unchanged.
    """
    if hasattr(settings, "model_dump"):
        settings = settings.model_dump()
    if not isinstance(settings, dict) or not isinstance(analysis, dict):
        raise ValueError("Configuração e análise de áudio inválidas.")
    bpm = settings.get("bpm", analysis.get("bpm", 120.0))
    strength = settings.get("intensity", 1.0)
    motion = settings.get("motion", "auto")
    fps = settings.get("fps", FPS)
    speed = settings.get("speed", 1.0)
    easing = settings.get("easing", "natural")
    squash = settings.get("squash", 0.25)
    if not _finite(duration) or not 0 < duration <= 30.00005:
        raise ValueError("A duração deve ser positiva e de no máximo 30 segundos.")
    for value, low, high, name in (
        (bpm, 40, 240, "BPM"), (strength, 0, 2, "intensidade"),
        (speed, 0.5, 2, "velocidade"), (squash, 0, 1, "deformação"),
    ):
        if not _finite(value) or not low <= value <= high:
            raise ValueError(f"Valor de {name} inválido.")
    if motion not in {*MOTIONS, "auto"} or easing not in {"natural", "linear", "ease-in-out"}:
        raise ValueError("Modo de movimento ou easing inválido.")
    if isinstance(fps, bool) or fps not in (24, 30, 60):
        raise ValueError("O FPS deve ser 24, 30 ou 60.")

    beats = analysis.get("beats", [])
    intensities = analysis.get("intensity", [1.0] * len(beats))
    onsets = analysis.get("onsets", [])
    energy = analysis.get("energy", [])
    silences = analysis.get("silences", [])
    events = analysis.get("events", [])
    confidence = analysis.get("rhythm_confidence", 0.5)
    if len(beats) != len(intensities):
        raise ValueError("Cada batida precisa de uma intensidade correspondente.")
    for times in (beats, onsets):
        if any(not _finite(t) or not 0 <= t <= duration for t in times):
            raise ValueError("Tempo de evento inválido.")
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("Os eventos devem estar em ordem crescente e sem repetições.")
    if any(not _finite(value) or not 0 <= value <= 1 for value in [*intensities, confidence]):
        raise ValueError("Intensidade ou confiança inválida.")
    if any(not isinstance(point, dict) or not _finite(point.get("time"))
           or not 0 <= point["time"] <= duration or not _finite(point.get("value"))
           or not 0 <= point["value"] <= 1 for point in energy):
        raise ValueError("Curva de energia inválida.")
    energy_times = [point["time"] for point in energy]
    energy_values = [point["value"] for point in energy]
    if any(a >= b for a, b in zip(energy_times, energy_times[1:])):
        raise ValueError("A energia deve estar em ordem crescente.")
    for interval in silences:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2 or any(not _finite(t) for t in interval):
            raise ValueError("Intervalo de silêncio inválido.")
        if not 0 <= interval[0] < interval[1] <= duration:
            raise ValueError("Intervalo de silêncio inválido.")
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in ("low", "high"):
            raise ValueError("Evento espectral inválido.")
        if not _finite(event.get("time")) or not 0 <= event["time"] <= duration:
            raise ValueError("Tempo de evento espectral inválido.")
        if not _finite(event.get("strength")) or not 0 <= event["strength"] <= 1:
            raise ValueError("Intensidade de evento espectral inválida.")

    period = 60.0 / bpm
    measured = analysis.get("bpm", bpm)
    overridden = _finite(measured) and abs(bpm - measured) > 0.5
    anchors = list(beats or onsets)
    if overridden and anchors:
        anchor = anchors[0]
        anchors = [anchor + index * period for index in range(math.ceil((duration - anchor) / period))]
    # Empty analyses intentionally produce a stationary pose. Do not invent
    # a full-strength dance for a source with no measurable rhythmic evidence.
    accents = []
    if not overridden:
        accents.extend((event["time"], event["kind"], event["strength"]) for event in events)
        for onset in onsets:
            if all(abs(onset - beat) > min(0.09, period * 0.18) for beat in anchors):
                accents.append((onset, "high", 0.35))
    accents.sort()
    accent_times = [event[0] for event in accents]

    # Release smoothing lets a short transient finish its recovery even after
    # instantaneous RMS drops. The explicit silence gate still reaches rest.
    relaxed = []
    previous = 0.0
    previous_time = 0.0
    for time, value in zip(energy_times, energy_values):
        elapsed = max(0.0, time - previous_time)
        previous = max(value, previous * math.exp(-elapsed / 0.16))
        relaxed.append(previous)
        previous_time = time
    amplitudes = []
    for anchor in anchors:
        beat_energy = _interpolate(beats, intensities, anchor)
        measured_energy = _interpolate(energy_times, relaxed, anchor)
        amplitudes.append(beat_energy * (0.72 + 0.28 * measured_energy))

    sample_times = [index / fps for index in range(math.ceil(duration * fps))]
    sample_times.append(duration)
    frames = []
    radius = period * 1.0 / speed
    for time in sample_times:
        pulse = rotation = body_pulse = body_rotation = 0.0
        left = bisect_right(anchors, time - radius)
        right = bisect_right(anchors, time + radius)
        for index in range(left, right):
            phase = (time - anchors[index]) * speed / period
            selected = motion
            if selected == "auto":
                selected = "smooth" if confidence < 0.4 or amplitudes[index] < 0.38 else "velocity"
            value = _gesture(phase, selected, easing) * amplitudes[index]
            # A slower, slightly delayed torso supplies follow-through. Head
            # remains at phase zero so the visible nod lands on the actual beat.
            body_value = _gesture((phase - 0.035) / 1.08, "smooth", easing) * amplitudes[index]
            direction = 1 if index % 2 == 0 else -1
            pulse += value
            rotation += direction * value
            body_pulse += body_value
            body_rotation += direction * body_value
        for index in range(bisect_right(accent_times, time - radius), bisect_right(accent_times, time + radius)):
            event_time, kind, amount = accents[index]
            phase = (time - event_time) * speed / period
            value = _gesture(phase, "hard", easing) * amount
            if kind == "low":
                body_pulse += 0.22 * value
            else:
                pulse += 0.14 * value
                rotation += 0.12 * value * (1 if index % 2 == 0 else -1)
        gain = strength * _silence_gain(time, silences) * (0.6 + 0.4 * _interpolate(energy_times, relaxed, time))
        pulse *= gain
        rotation *= gain
        body_pulse *= gain
        body_rotation *= gain
        whole = _transform(0.009 * rotation, 0.032 * pulse, 5.5 * rotation, pulse, 0.14 * squash)
        body = _transform(0.003 * body_rotation, 0.010 * body_pulse, 1.1 * body_rotation,
                          body_pulse, 0.06 * squash, 0.003)
        head = _transform(0.006 * rotation, 0.022 * pulse, 4.4 * rotation,
                          pulse, 0.11 * squash, 0.005)
        frames.append({"time": round(time, 8), **whole, "layers": {"body": body, "head": head}})
    return frames
