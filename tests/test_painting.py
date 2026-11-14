"""Selection follows visible edges and exact erase marks without changing RGB."""

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from headbop.painting import PaintSelection, paint_selection


def stroke(x, y, radius=.04, mode='add'):
    return {'points': [[x, y]], 'radius': radius, 'mode': mode}


@pytest.fixture
def picture(tmp_path):
    image = Image.new('RGBA', (200, 160), (230, 235, 240, 255))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((60, 40, 140, 120), fill=(200, 40, 30, 255))
    source = tmp_path / 'source.png'
    image.save(source)
    return source, tmp_path / 'foreground.png'


def apply(picture, strokes, *, smart=True, base='empty', current=None):
    source, output = picture
    metadata = paint_selection(source, output, PaintSelection(strokes=strokes, smart=smart,
                                                            base=base), current)
    return np.asarray(Image.open(output)), metadata


def test_smart_brush_expands_to_object_edge_but_not_background(picture):
    rgba, info = apply(picture, [stroke(.5, .5)])
    assert info['method'] == 'smart-brush' and info['kind'] == 'unknown'
    assert info['head_rect'] is None  # Drawing a region does not infer head anatomy.
    assert np.all(rgba[40:121, 60:141, 3] == 255)
    assert np.count_nonzero(rgba[:, :, 3]) == 81 * 81
    assert np.array_equal(rgba[:, :, :3], np.asarray(Image.open(picture[0]))[:, :, :3])


def test_manual_brush_is_precise_and_does_not_expand(picture):
    rgba, info = apply(picture, [stroke(.5, .5)], smart=False)
    assert info['method'] == 'manual-brush'
    assert 100 < np.count_nonzero(rgba[:, :, 3]) < 160
    assert rgba[80, 100, 3] == 255 and rgba[80, 120, 3] == 0


def test_eraser_refines_smart_expansion_and_later_add_can_restore(picture):
    rgba, _ = apply(picture, [stroke(.5, .5), stroke(.6, .5, mode='erase')])
    assert rgba[80, 100, 3] == 255 and rgba[80, 120, 3] == 0
    rgba, _ = apply(picture, [stroke(.5, .5), stroke(.6, .5, mode='erase'), stroke(.6, .5)])
    assert rgba[80, 120, 3] == 255


def test_smart_selection_keeps_multiple_marked_objects_not_unmarked_copy(tmp_path):
    image = Image.new('RGBA', (300, 180), 'white')
    draw = ImageDraw.Draw(image)
    for x in (30, 130, 230):
        draw.rectangle((x, 60, x + 40, 110), fill=(230, 60, 40, 255))
    source, output = tmp_path / 'image.png', tmp_path / 'mask.png'
    image.save(source)
    rgba, _ = apply((source, output), [stroke(50 / 300, .5), stroke(250 / 300, .5)])
    assert rgba[90, 50, 3] == 255 and rgba[90, 250, 3] == 255
    assert rgba[90, 150, 3] == 0


def test_source_alpha_is_authoritative_even_for_manual_paint(tmp_path):
    image = Image.new('RGBA', (100, 100), (10, 80, 190, 0))
    ImageDraw.Draw(image).rectangle((30, 30, 70, 70), fill=(10, 80, 190, 127))
    source, output = tmp_path / 'source.png', tmp_path / 'mask.png'
    image.save(source)
    rgba, _ = apply((source, output), [stroke(.5, .5, .2)], smart=False)
    assert rgba[50, 50, 3] == 127
    assert rgba[10, 10, 3] == 0
    assert np.all(rgba[:, :, 3] <= np.asarray(image)[:, :, 3])


def test_refining_current_mask_preserves_unpainted_selection(picture, tmp_path):
    first, _ = apply(picture, [stroke(.5, .5)])
    current = tmp_path / 'existing.png'
    Image.fromarray(first).save(current)
    refined, _ = apply(picture, [stroke(.6, .5, mode='erase')], base='current', current=current)
    assert refined[80, 120, 3] == 0
    assert refined[80, 70, 3] == 255
    assert refined[80, 100, 3] == 255


def test_empty_selection_does_not_replace_existing_file(picture):
    picture[1].write_bytes(b'previous-asset')
    with pytest.raises(ValueError, match='Pinte uma área'):
        apply(picture, [stroke(.5, .5), stroke(.5, .5, .1, mode='erase')], smart=False)
    assert picture[1].read_bytes() == b'previous-asset'


@pytest.mark.parametrize('payload', [
    {'strokes': []}, {'strokes': [stroke(-.1, .5)]},
    {'strokes': [stroke(.5, float('nan'))]}, {'strokes': [stroke(.5, .5, .201)]},
    {'strokes': [stroke(.5, .5, .001)]}, {'strokes': [stroke(.5, .5, mode='unknown')]},
    {'strokes': [stroke(.5, .5)] * 101},
    {'strokes': [{'points': [[.5, .5]] * 1001, 'radius': .02, 'mode': 'add'}]},
    {'strokes': [{'points': [[.5, .5]] * 1000, 'radius': .02, 'mode': 'add'}] * 6},
    {'strokes': [stroke(.5, .5)], 'base': 'path/to/private'},
])
def test_untrusted_brush_payloads_are_bounded(payload):
    with pytest.raises(ValidationError):
        PaintSelection.model_validate(payload)


def test_smart_processing_is_bounded_but_keeps_full_resolution_marks(tmp_path, monkeypatch):
    source, output = tmp_path / 'source.png', tmp_path / 'mask.png'
    image = Image.new('RGBA', (1600, 1200), 'white')
    ImageDraw.Draw(image).rectangle((500, 300, 1100, 900), fill='red')
    image.save(source)
    shapes = []
    def segmentation(rgb, marks, alpha, radius):
        shapes.append(rgb.shape)
        return (marks == 1).astype(np.uint8) * 255
    monkeypatch.setattr('headbop.painting._smart_mask', segmentation)
    rgba, _ = apply((source, output), [stroke(.5, .5, .002)])
    assert shapes == [(480, 640, 3)]
    assert rgba.shape == (1200, 1600, 4)
    assert rgba[600, 800, 3] == 255
