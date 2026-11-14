"""Portable, strictly validated Headbop project XML versions 1 and 2."""

from __future__ import annotations

import base64
import binascii
import math
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from .settings import Settings

MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 56 * 1024 * 1024
MAX_FRAMES = 1801
MAX_V1_FRAMES = 722
FORMAT_ERROR = "Formato não reconhecido. Importe um projeto XML Headbop v1 ou v2."
V1_SETTINGS = {"mode", "motion", "bpm", "intensity"}
V2_SETTINGS = V1_SETTINGS | {
    "engine", "fps", "resolution", "aspect", "speed", "easing", "squash"
}
V1_TRANSFORM = {"x", "y", "rotation", "scale"}
V2_TRANSFORM = V1_TRANSFORM | {"scale_x", "scale_y"}
RECT_ATTRIBUTES = ("x", "y", "width", "height")
SEGMENTATION_METHODS = {
    "alpha", "uniform-background", "face-grabcut", "manual-grabcut", "unavailable", "none",
    "smart-brush", "manual-brush",
}
SEGMENTATION_ATTRIBUTES = {"method", "kind", "confidence"}


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


def _settings(value: dict, version: int) -> dict:
    if not isinstance(value, dict) or set(value) != (
        V1_SETTINGS if version == 1 else V2_SETTINGS
    ):
        raise ValueError("Configurações incompletas ou desconhecidas no projeto.")
    if version == 1 and value["motion"] not in {"smooth", "hard", "velocity"}:
        raise ValueError("Movimento inválido.")
    parsed = dict(value)
    parsed["bpm"] = _number(value["bpm"], 40, 240, "BPM")
    parsed["intensity"] = _number(value["intensity"], 0, 2, "Intensidade")
    if version == 1:
        parsed["engine"] = "legacy"
    else:
        parsed["speed"] = _number(value["speed"], 0.5, 2, "Velocidade")
        parsed["squash"] = _number(value["squash"], 0, 1, "Deformação")
        for field, allowed in (("fps", {24, 30, 60}), ("resolution", {640, 720, 1080})):
            number = _number(value[field], min(allowed), max(allowed), field)
            if number not in allowed:
                raise ValueError(f"{field} não suportado.")
            parsed[field] = int(number)
    validated = Settings.model_validate(parsed).model_dump()
    # A v1 document retains its original four settings; the caller chooses legacy.
    return {field: validated[field] for field in value}


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
    valid = data.startswith(b"\x89PNG\r\n\x1a\n") if kind in {"image", "foreground"} else (
        len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    )
    if not valid:
        raise ValueError("Projeto deve conter imagem PNG e áudio WAV normalizados.")
    return data


def _segmentation_info(value: dict) -> dict:
    if set(value) not in (SEGMENTATION_ATTRIBUTES, SEGMENTATION_ATTRIBUTES | {"head_rect"}):
        raise ValueError("Metadados de recorte incompletos ou desconhecidos.")
    if value["method"] not in SEGMENTATION_METHODS:
        raise ValueError("Método de recorte desconhecido.")
    if value["kind"] not in {"head", "character", "unknown"}:
        raise ValueError("Tipo de recorte desconhecido.")
    result = {
        "method": value["method"], "kind": value["kind"],
        "confidence": _number(value["confidence"], 0, 1, "Confiança do recorte"),
    }
    if value.get("head_rect") is not None:
        result["head_rect"] = _rect(value["head_rect"])
    return result


def _transform(value: dict, *, extended: bool) -> dict:
    expected = V2_TRANSFORM if extended else V1_TRANSFORM
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Transformação incompleta ou desconhecida.")
    result = {
        "x": _number(value["x"], -2, 2, "Posição X"),
        "y": _number(value["y"], -2, 2, "Posição Y"),
        "rotation": _number(value["rotation"], -360, 360, "Rotação"),
        "scale": _number(value["scale"], 0.1, 4, "Escala"),
    }
    if extended:
        for axis in ("scale_x", "scale_y"):
            result[axis] = _number(value[axis], 0.1, 4, "Escala")
    return result


def _frames(frames: list[dict], duration: float, version: int, engine: str) -> list[dict]:
    limit = MAX_V1_FRAMES if version == 1 else MAX_FRAMES
    if not isinstance(frames, list) or not 1 <= len(frames) <= limit:
        raise ValueError("Quantidade de keyframes inválida.")
    result = []
    previous = -1.0
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("Keyframe incompleto ou desconhecido.")
        # V2 may carry original sampled frames when its engine is legacy.
        extended = version == 2 and (engine != "legacy" or "layers" in frame)
        expected = {"time"} | (V2_TRANSFORM | {"layers"} if extended else V1_TRANSFORM)
        if set(frame) != expected:
            raise ValueError("Keyframe incompleto ou desconhecido.")
        parsed = {"time": _number(frame["time"], 0, duration + 0.000001, "Tempo")}
        parsed.update(_transform(
            {key: value for key, value in frame.items() if key not in {"time", "layers"}},
            extended=extended,
        ))
        if extended:
            layers = frame["layers"]
            if not isinstance(layers, dict) or set(layers) != {"body", "head"}:
                raise ValueError("Camadas de keyframe incompletas ou desconhecidas.")
            parsed["layers"] = {
                name: _transform(layers[name], extended=True) for name in ("body", "head")
            }
        if parsed["time"] <= previous:
            raise ValueError("Keyframes devem estar em ordem temporal estrita.")
        previous = parsed["time"]
        result.append(parsed)
    return result


def export_project(
    project: dict, image_bytes: bytes, audio_bytes: bytes, *, foreground_bytes: bytes | None = None
) -> bytes:
    """Export only public animation data and embedded media, never local paths."""
    version = 1 if set(project["settings"]) == V1_SETTINGS else 2
    settings = _settings(project["settings"], version)
    duration = _number(project["duration"], 0.5, 30, "Duração")
    frames = _frames(project["keyframes"], duration, version, settings.get("engine", "legacy"))
    root = ET.Element("headbop-project", {"version": str(version)})
    ET.SubElement(root, "settings", {k: str(v) for k, v in settings.items()})
    segmentation = project.get("segmentation", {})
    rect = segmentation.get("rect")
    # An alpha bounding box describes existing transparency, not a GrabCut seed.
    # Passing that box back through GrabCut would destroy an already precise mask.
    if rect is not None and segmentation.get("method") != "alpha":
        ET.SubElement(root, "segmentation", dict(zip(
            RECT_ATTRIBUTES, map(str, _rect(rect)), strict=True
        )))
    if version == 2 and "method" in segmentation:
        info = _segmentation_info({
            key: value for key, value in segmentation.items()
            if key in SEGMENTATION_ATTRIBUTES | {"head_rect"}
        })
        node = ET.SubElement(root, "segmentation-info", {
            key: str(value) for key, value in info.items() if key != "head_rect"
        })
        if "head_rect" in info:
            ET.SubElement(node, "head-rect", dict(zip(
                RECT_ATTRIBUTES, map(str, info["head_rect"]), strict=True
            )))
    head_rect = project.get("rig", {}).get("head_rect")
    if version == 2 and head_rect is not None:
        rig = ET.SubElement(root, "rig")
        ET.SubElement(rig, "head-rect", dict(zip(
            RECT_ATTRIBUTES, map(str, _rect(head_rect)), strict=True
        )))
    assets = ET.SubElement(root, "assets")
    for kind, mime, data in (
        ("image", "image/png", image_bytes), ("audio", "audio/wav", audio_bytes)
    ):
        item = ET.SubElement(assets, kind, {"encoding": "base64", "mime": mime})
        item.text = base64.b64encode(_asset(data, kind)).decode("ascii")
    if version == 2 and foreground_bytes is not None:
        item = ET.SubElement(assets, "foreground", {"encoding": "base64", "mime": "image/png"})
        item.text = base64.b64encode(_asset(foreground_bytes, "foreground")).decode("ascii")
    timeline = ET.SubElement(root, "timeline", {
        "fps": str(settings.get("fps", 24)), "duration": str(duration)
    })
    for frame in frames:
        node = ET.SubElement(timeline, "keyframe", {
            key: str(value) for key, value in frame.items() if key != "layers"
        })
        for name, transform in frame.get("layers", {}).items():
            ET.SubElement(node, "layer", {
                "name": name, **{key: str(value) for key, value in transform.items()}
            })
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

    Frames are returned for faithful restoration after the caller verifies that the
    media duration matches this document. No paths or executable data are accepted.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_XML_BYTES:
        raise ValueError("Projeto XML vazio ou maior que 56 MiB.")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise ValueError("XML inválido ou inseguro; DTDs e entidades são proibidas.") from exc
    if root.tag != "headbop-project" or root.attrib not in ({"version": "1"}, {"version": "2"}):
        raise ValueError(FORMAT_ERROR)
    version = int(root.attrib["version"])
    _structure(root, {"version"}, leaf=False)
    allowed_sections = {"settings", "segmentation", "assets", "timeline"}
    if version == 2:
        allowed_sections.update({"rig", "segmentation-info"})
    sections = {}
    for child in root:
        if child.tag not in allowed_sections or child.tag in sections:
            raise ValueError("Seção XML desconhecida ou duplicada.")
        sections[child.tag] = child
    if not {"settings", "assets", "timeline"} <= sections.keys():
        raise ValueError("Projeto XML incompleto.")
    settings = sections["settings"]
    _structure(settings, V1_SETTINGS if version == 1 else V2_SETTINGS)
    result = {"schema_version": version, "settings": _settings(settings.attrib, version)}
    if "segmentation" in sections:
        segment = sections["segmentation"]
        _structure(segment, set(RECT_ATTRIBUTES))
        result["rect"] = _rect([segment.attrib[key] for key in RECT_ATTRIBUTES])
    if "segmentation-info" in sections:
        segment_info = sections["segmentation-info"]
        _structure(segment_info, SEGMENTATION_ATTRIBUTES, leaf=False)
        metadata = dict(segment_info.attrib)
        if len(segment_info):
            if len(segment_info) != 1 or segment_info[0].tag != "head-rect":
                raise ValueError("Metadados de recorte incompletos ou desconhecidos.")
            _structure(segment_info[0], set(RECT_ATTRIBUTES))
            metadata["head_rect"] = [segment_info[0].attrib[key] for key in RECT_ATTRIBUTES]
        result["segmentation_info"] = _segmentation_info(metadata)
    if "rig" in sections:
        rig = sections["rig"]
        _structure(rig, set(), leaf=False)
        if len(rig) != 1 or rig[0].tag != "head-rect":
            raise ValueError("Metadados de rig incompletos ou desconhecidos.")
        _structure(rig[0], set(RECT_ATTRIBUTES))
        result["head_rect"] = _rect([rig[0].attrib[key] for key in RECT_ATTRIBUTES])
    assets = sections["assets"]
    _structure(assets, set(), leaf=False)
    asset_names = {item.tag for item in assets}
    allowed_assets = {"image", "audio", "foreground"} if version == 2 else {"image", "audio"}
    if (not {"image", "audio"} <= asset_names or not asset_names <= allowed_assets
            or len(assets) != len(asset_names)):
        raise ValueError("Projeto deve conter exatamente uma imagem e um áudio.")
    for item in assets:
        _structure(item, {"encoding", "mime"}, text=True)
        expected_mime = "audio/wav" if item.tag == "audio" else "image/png"
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
    if timeline.attrib["fps"] != str(result["settings"].get("fps", 24)):
        raise ValueError("Taxa de quadros da timeline difere das configurações.")
    duration = _number(timeline.attrib["duration"], 0.5, 30, "Duração")
    if len(timeline) > (MAX_V1_FRAMES if version == 1 else MAX_FRAMES):
        raise ValueError("Quantidade de keyframes inválida.")
    frames = []
    for frame in timeline:
        if frame.tag != "keyframe":
            raise ValueError("Elemento de timeline desconhecido.")
        extended = version == 2 and (
            result["settings"]["engine"] != "legacy" or bool(len(frame))
        )
        attributes = {"time"} | (V2_TRANSFORM if extended else V1_TRANSFORM)
        _structure(frame, attributes, leaf=not extended)
        parsed_frame = dict(frame.attrib)
        if extended:
            layers = {}
            for layer in frame:
                if layer.tag != "layer":
                    raise ValueError("Elemento de keyframe desconhecido.")
                _structure(layer, V2_TRANSFORM | {"name"})
                name = layer.attrib["name"]
                if name not in {"body", "head"} or name in layers:
                    raise ValueError("Camada de keyframe desconhecida ou duplicada.")
                layers[name] = {key: value for key, value in layer.attrib.items() if key != "name"}
            parsed_frame["layers"] = layers
        frames.append(parsed_frame)
    result["duration"] = duration
    result["keyframes"] = _frames(
        frames, duration, version, result["settings"].get("engine", "legacy")
    )
    return result
