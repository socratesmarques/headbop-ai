"""Catalog coverage prevents untranslated flow messages from shipping again."""

import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'headbop/static/i18n.js').read_text()
CATALOGS = json.JSONDecoder().raw_decode(SOURCE.split('const translations = ', 1)[1])[0]
LANGUAGES = ('pt-BR', 'en', 'es', 'fr', 'de', 'it', 'zh-CN', 'hi')


@pytest.mark.parametrize('language', LANGUAGES)
def test_every_locale_has_all_messages_and_identical_parameters(language):
    reference = CATALOGS['pt-BR']
    actual = CATALOGS[language]
    assert actual.keys() == reference.keys()
    for key, source in reference.items():
        assert isinstance(actual[key], str) and actual[key].strip(), (language, key)
        assert set(re.findall(r'\{\w+\}', source)) == set(re.findall(r'\{\w+\}', actual[key])), (
            language, key
        )


class VisibleMessages(HTMLParser):
    """Collect interface text while excluding language names and product names."""

    def __init__(self):
        super().__init__()
        self.messages = []
        self.ignored = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        ignore = (tag in {'script', 'style'} or attrs.get('id') == 'language-select'
                  or 'data-auth-text' in attrs or 'data-i18n' in attrs)
        self.ignored.append((tag, ignore or (self.ignored[-1][1] if self.ignored else False)))
        if tag in {'meta', 'link', 'input', 'br', 'img'}:
            self.ignored.pop()
        for name in ('aria-label', 'title', 'placeholder', 'alt', 'data-auth-text',
                     'data-i18n', 'data-i18n-aria'):
            if attrs.get(name):
                self.messages.append(attrs[name])

    def handle_endtag(self, tag):
        for index in range(len(self.ignored) - 1, -1, -1):
            if self.ignored[index][0] == tag:
                del self.ignored[index:]
                break

    def handle_data(self, data):
        if self.ignored and self.ignored[-1][1]:
            return
        value = data.strip()
        if value and any(char.isalpha() for char in value):
            self.messages.append(value)


def test_visible_html_messages_are_catalogued():
    parser = VisibleMessages()
    parser.feed((ROOT / 'headbop/static/index.html').read_text())
    technical = {'headbop', 'AI', 'HEADBOP STUDIO', 'ϟ', 'BPM', 'Headbop 2.0',
                 '24 FPS', '30 FPS', '60 FPS', '720 · HD', '1080 · Full HD',
                 'MP4', 'HEADBOP AI', 'OPEN SOURCE', 'Headbop', 'GitHub', 'GitHub ↗'}
    known = set(CATALOGS['pt-BR']) | set(CATALOGS['pt-BR'].values()) | technical
    assert set(parser.messages) <= known


def test_dynamic_client_message_literals_are_catalogued():
    source = (ROOT / 'headbop/static/app.js').read_text()
    patterns = [r'text\("[^"]+", "([^"]+)"', r'notice\("([^"]+)"',
                r'previewStatus\("([^"]+)"', r'messageError\("([^"]+)"',
                r'setBusy\([^,]+, "([^"]+)"']
    used = {key for pattern in patterns for key in re.findall(pattern, source)}
    assert used <= CATALOGS['pt-BR'].keys()


def test_backend_warnings_are_translatable():
    warnings = set()
    for name in ('imaging', 'rigging', 'main', 'painting'):
        tree = ast.parse((ROOT / f'headbop/{name}.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == 'warning'
                            and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                        warnings.add(value.value)
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'warning' for target in node.targets
            ) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                warnings.add(node.value.value)
    assert warnings <= CATALOGS['pt-BR'].keys()


def test_known_error_contract_is_present_in_every_catalog():
    codes = {'auth_required', 'invalid_credentials', 'csrf_failed', 'origin_forbidden',
             'rate_limited', 'capacity_busy', 'project_not_found', 'quota_projects',
             'quota_storage', 'render_timeout', 'render_failed', 'invalid_request',
             'upload_too_large', 'content_length_required', 'auth_busy', 'session_expired',
             'request_timeout', 'network', 'unknown'}
    for catalog in CATALOGS.values():
        assert {f'error.{code}' for code in codes} <= catalog.keys()


def test_media_validation_instructions_are_translatable():
    messages = set()
    for name in ('audio', 'formats', 'main', 'settings', 'imaging', 'rigging', 'painting'):
        tree = ast.parse((ROOT / f'headbop/{name}.py').read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            call = node.exc
            if not isinstance(call.func, ast.Name):
                continue
            index = {'ValueError': 0, 'HTTPException': 1}.get(call.func.id)
            if index is None or len(call.args) <= index:
                continue
            value = call.args[index]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                messages.add(value.value)
        # The format discriminator is deliberately a named constant.
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'FORMAT_ERROR'
                for target in node.targets
            ):
                messages.add(ast.literal_eval(node.value))
    assert messages <= CATALOGS['pt-BR'].keys()


def test_parameterized_media_validation_has_complete_finite_messages():
    tree = ast.parse((ROOT / 'headbop/formats.py').read_text())
    labels = {'fps', 'resolution'}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == '_number' and len(node.args) == 4
                and isinstance(node.args[3], ast.Constant)):
            labels.add(node.args[3].value)
    messages = {f'{label} {suffix}.' for label in labels
                for suffix in ('inválido', 'fora do intervalo permitido')}
    messages |= {f'{label} não suportado.' for label in ('fps', 'resolution')}
    messages |= {f'Arquivo excede o limite de {limit} MiB.' for limit in (20, 63)}
    assert messages <= CATALOGS['pt-BR'].keys()
