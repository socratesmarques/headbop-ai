"""Portable Headbop project XML. This is deliberately not Alight Motion XML."""

from __future__ import annotations

import base64
import binascii
import math
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 56 * 1024 * 1024
MAX_FRAMES = 722
FORMAT_ERROR = (
    "Formato não reconhecido. Importe um projeto XML Headbop v1. "
    "A compatibilidade com XML do Alight Motion não foi validada."
)


def _number(value: object, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} inválido.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} inválido.") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{label} fora do intervalo permitido.")
    return result


def _settings(value: dict) -> dict:
    if set(value) != {"mode", "motion", "bpm", "intensity"}:
        raise ValueError("Configurações incompletas ou desconhecidas no projeto.")
    if value["mode"] not in {"whole", "subject"}:
        raise ValueError("Modo de animação inválido.")
    if value["motion"] not in {"smooth", "hard", "velocity"}:
        raise ValueError("Movimento inválido.")
    return {
        "mode": value["mode"],
        "motion": value["motion"],
        "bpm": _number(value["bpm"], 40, 240, "BPM"),
        "intensity": _number(value["intensity"], 0, 2, "Intensidade"),
    }


def _rect(value: object) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Retângulo de recorte inválido.")
    x, y, width, height = [_number(v, 0, 1, "Recorte") for v in value]
    if width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
        raise ValueError("Retângulo de recorte fora da imagem.")
    return [x, y, width, height]


def _asset(data: bytes, kind: str) -> bytes:
    if not isinstance(data, bytes) or not data or len(data) > MAX_ASSET_BYTES:
        raise ValueError("Cada mídia incorporada deve ter no máximo 20 MiB.")
    valid = data.startswith(b"\x89PNG\r\n\x1a\n") if kind == "image" else (
        len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    )
    if not valid:
        raise ValueError("Projeto deve conter imagem PNG e áudio WAV normalizados.")
    return data


def _frames(frames: list[dict], duration: float) -> list[dict]:
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError("Quantidade de keyframes inválida.")
    result = []
    previous = -1.0
    for frame in frames:
        if set(frame) != {"time", "x", "y", "rotation", "scale"}:
            raise ValueError("Keyframe incompleto ou desconhecido.")
        parsed = {
            "time": _number(frame["time"], 0, duration + 0.000001, "Tempo"),
            "x": _number(frame["x"], -2, 2, "Posição X"),
            "y": _number(frame["y"], -2, 2, "Posição Y"),
            "rotation": _number(frame["rotation"], -360, 360, "Rotação"),
            "scale": _number(frame["scale"], 0.01, 10, "Escala"),
        }
        if parsed["time"] <= previous:
            raise ValueError("Keyframes devem estar em ordem temporal estrita.")
        previous = parsed["time"]
        result.append(parsed)
    return result


def export_project(project: dict, image_bytes: bytes, audio_bytes: bytes) -> bytes:
    """Export only public animation data and embedded media, never local paths."""
    settings = _settings(project["settings"])
    duration = _number(project["duration"], 0.5, 30, "Duração")
    frames = _frames(project["keyframes"], duration)
    root = ET.Element("headbop-project", {"version": "1"})
    ET.SubElement(root, "settings", {k: str(v) for k, v in settings.items()})
    segmentation = project.get("segmentation", {})
    rect = segmentation.get("rect")
    # An alpha bounding box describes existing transparency, not a GrabCut seed.
    # Passing that box back through GrabCut would destroy an already precise mask.
    if rect is not None and segmentation.get("method") != "alpha":
        ET.SubElement(root, "segmentation", dict(zip(
            ("x", "y", "width", "height"), map(str, _rect(rect)), strict=True
        )))
    assets = ET.SubElement(root, "assets")
    for kind, mime, data in (
        ("image", "image/png", image_bytes), ("audio", "audio/wav", audio_bytes)
    ):
        item = ET.SubElement(assets, kind, {"encoding": "base64", "mime": mime})
        item.text = base64.b64encode(_asset(data, kind)).decode("ascii")
    timeline = ET.SubElement(root, "timeline", {"fps": "24", "duration": str(duration)})
    for frame in frames:
        ET.SubElement(timeline, "keyframe", {k: str(v) for k, v in frame.items()})
    encoded = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(encoded) > MAX_XML_BYTES:
        raise ValueError("Projeto XML excede o limite de 56 MiB.")
    return encoded


def _structure(
    node: ET.Element, attributes: set[str], *, leaf: bool = True, text: bool = False
) -> None:
    if set(node.attrib) != attributes or (leaf and len(node)):
        raise ValueError("Estrutura XML Headbop inválida.")
    if node.tail and node.tail.strip():
        raise ValueError("Texto inesperado no XML Headbop.")
    if not text and node.text and node.text.strip():
        raise ValueError("Texto inesperado no XML Headbop.")


def import_project(data: bytes) -> dict:
    """Validate the entire document; caller must decode/reanalyse media before use.

    Sampled frames are checked but intentionally not trusted as execution input.
    The application regenerates them from settings and music with its current engine.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_XML_BYTES:
        raise ValueError("Projeto XML vazio ou maior que 56 MiB.")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise ValueError("XML inválido ou inseguro; DTDs e entidades são proibidas.") from exc
    if root.tag != "headbop-project" or root.attrib != {"version": "1"}:
        raise ValueError(FORMAT_ERROR)
    if root.text and root.text.strip():
        raise ValueError("Texto inesperado no XML Headbop.")
    sections = {}
    for child in root:
        if child.tag not in {"settings", "segmentation", "assets", "timeline"} or child.tag in sections:
            raise ValueError("Seção XML desconhecida ou duplicada.")
        sections[child.tag] = child
    if not {"settings", "assets", "timeline"} <= sections.keys():
        raise ValueError("Projeto XML incompleto.")
    settings = sections["settings"]
    _structure(settings, {"mode", "motion", "bpm", "intensity"})
    result = {"settings": _settings(settings.attrib)}
    if "segmentation" in sections:
        segment = sections["segmentation"]
        _structure(segment, {"x", "y", "width", "height"})
        result["rect"] = _rect([segment.attrib[k] for k in ("x", "y", "width", "height")])
    assets = sections["assets"]
    _structure(assets, set(), leaf=False)
    if len(assets) != 2 or {item.tag for item in assets} != {"image", "audio"}:
        raise ValueError("Projeto deve conter exatamente uma imagem e um áudio.")
    for item in assets:
        _structure(item, {"encoding", "mime"}, text=True)
        expected_mime = "image/png" if item.tag == "image" else "audio/wav"
        if item.attrib != {"encoding": "base64", "mime": expected_mime}:
            raise ValueError("Codificação ou tipo de mídia não permitido.")
        # Bound encoded size before decoding; whitespace is not emitted by exporter.
        encoded = item.text or ""
        if len(encoded) > ((MAX_ASSET_BYTES + 2) // 3) * 4:
            raise ValueError("Cada mídia incorporada deve ter no máximo 20 MiB.")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Mídia base64 inválida.") from exc
        result[item.tag] = _asset(decoded, item.tag)
    timeline = sections["timeline"]
    _structure(timeline, {"fps", "duration"}, leaf=False)
    if timeline.attrib["fps"] != "24":
        raise ValueError("Taxa de quadros não suportada; use 24 fps.")
    duration = _number(timeline.attrib["duration"], 0.5, 30, "Duração")
    if len(timeline) > MAX_FRAMES:
        raise ValueError("Quantidade de keyframes inválida.")
    frames = []
    for frame in timeline:
        if frame.tag != "keyframe":
            raise ValueError("Elemento de timeline desconhecido.")
        _structure(frame, {"time", "x", "y", "rotation", "scale"})
        frames.append(frame.attrib)
    _frames(frames, duration)
    return result
