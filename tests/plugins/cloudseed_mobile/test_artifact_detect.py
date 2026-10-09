"""Desktop's artifact rule, server port: shared fixtures plus fence extraction."""
import hashlib
import json
from pathlib import Path

import pytest

from plugins.cloudseed_mobile import artifact_detect as a
from plugins.cloudseed_mobile import deliverables as d

FIXTURE = Path(__file__).resolve().parents[2]/'fixtures'/'artifact_detect_cases.json'
# The app vendors these exact bytes (HermesMobileTests/Fixtures) and asserts the
# same hash; Desktop vitest reads this file directly. Regenerate expectations from
# the TypeScript rule, never by hand, and update all three together.
FIXTURE_SHA256 = '6bb1dac64746802067fcb8726aa892803f505de1cd4dec1945cf09335b9c8bfd'
CASES = json.loads(FIXTURE.read_text(encoding='utf-8'))


def test_fixture_bytes_are_the_shared_set():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert CASES['schema'] == 1 and len(CASES['detect']) >= 30 and len(CASES['slug']) >= 8


@pytest.mark.parametrize('case', CASES['detect'], ids=lambda c: c['name'])
def test_detect_matches_desktop(case):
    detection = a.detect_artifact(case['language'], case['code'])
    if detection:
        detection = {**detection, 'slug': a.artifact_slug(detection['kind'], detection['language'], detection['title'])}
    assert detection == case['expected']


@pytest.mark.parametrize('case', CASES['slug'], ids=lambda c: c['name'])
def test_slug_matches_desktop(case):
    assert a.artifact_slug(case['kind'], case['language'], case['title']) == case['expected']


def test_js_string_semantics():
    # JS lengths count UTF-16 units and slices may split a surrogate pair.
    assert a.utf16_len('a\U0001F600') == 3
    assert a.utf16_slice('a\U0001F600b', 2) == 'a\ud83d'
    # ECMAScript trim keeps U+001C/U+0085 (Python whitespace) and drops U+FEFF.
    assert a.js_trim('\ufeff\u2028 x\x1c\x85') == 'x\x1c\x85'
    # JS `\w`/`/i` are ASCII: a long-s or Kelvin sign is not a declaration name start.
    assert a.sanitize_language_tag('ſwift') == '' and a.sanitize_language_tag(' Swift tail') == 'swift'


HTML = '<!doctype html><html><head><title>Pricing page</title></head><body>' + '<p>row</p>' * 20 + '</body></html>'
CODE = '\n'.join(f'export function helper{i}(value: number) {{ return value * {i} }}' for i in range(60))


def test_fences_follow_desktop_markdown_normalization():
    text = '\n'.join([
        'Intro', '```html', HTML, '```',
        '```{.python}', 'not a fence opener', '```',  # invalid opener stays text; its closer opens
        'swallowed', '```',
        '~~~ts title="x"', CODE, '~~~',
        '````markdown', '```', 'const a = 1;', '```', '````',
        '```bash', 'https://example.com/a', '', 'https://example.com/b', '```',  # URL-only: unwrapped
        '```math', 'E = mc^2', '```',
        '```', '', '```',
    ])
    found = list(a.fences(text))
    assert found[0] == ('html', HTML)
    assert ('', 'swallowed') in found
    assert ('ts', CODE) in found
    assert ('markdown', '```\nconst a = 1;\n```') in found
    assert all(language not in {'bash', 'math'} for language, _ in found)


def test_unclosed_and_indented_fences():
    assert list(a.fences('Here:\n```python\n' + CODE)) == [('python', CODE)]
    indented = '1. Step\n   ```ts\n   const a = 1\n     nested\n   ```'
    assert list(a.fences(indented)) == [('ts', 'const a = 1\n  nested')]
    assert list(a.fences('```ts\r\nconst a = 1\r\n```\r')) == [('ts', 'const a = 1\r')]
    # An info-string tail with prose and few code signals is unwrapped like Desktop.
    assert list(a.fences('```ts note\nThis is prose.\nAnd more prose here.\n```')) == []


def project(content, timestamp=None):
    return d.project('default', {'id': 's', 'title': 'Fixture', 'cwd': '/granted'},
                     [{'id': 1, 'role': 'assistant', 'content': content, 'timestamp': timestamp}],
                     [{'id': 'w', 'root_path': '/granted', 'name': 'Fixture'}])


def test_projection_uses_desktop_rule():
    rows = project('```html\n' + HTML + '\n```\n\n```typescript\n' + CODE + '\n```', 10.0)
    html = next(r for r in rows if r['artifact_kind'] == 'html')
    assert html['display_name'] == 'Pricing page.html' and html['display_type'] == 'text/html'
    assert html['artifact_title'] == 'Pricing page' and html['artifact_slug'] == 'html:html:pricing-page'
    assert html['artifact_first_seen'] == [10.0, 1, 0, '1'] and html['inline_content'] == HTML
    code = next(r for r in rows if r['artifact_kind'] == 'code')
    assert code['display_name'] == 'helper0.ts' and code['artifact_slug'] == 'code:typescript:helper0'
    assert html['artifact_key'] != code['artifact_key']


def test_rule_is_stricter_than_the_old_server_fence_rule():
    # The v5 rule promoted any ```html body of 160+ chars; Desktop wants a document or
    # a 1,200-char fragment, and never promotes excluded languages however long.
    fragment = '<div class="card">' + 'x' * 400 + '</div>'
    assert project('```html\n' + fragment + '\n```') == []
    for language in ('text', 'diff', 'listing', 'console', 'log'):
        assert project(f'```{language}\n' + CODE + '\n```') == []
    assert project('```\n' + CODE + '\n```') == []


def test_code_title_never_borrows_a_markup_extension():
    named = '// index.html\n' + CODE
    assert a.display_name(a.detect_artifact('js', named)) == 'index.html.js'
    assert a.display_name({'kind': 'code', 'language': 'zig', 'title': 'main'}) == 'main.zig'
    assert a.display_name({'kind': 'code', 'language': 'python', 'title': 'server.py'}) == 'server.py'
    assert a.display_name({'kind': 'svg', 'language': 'svg', 'title': 'Org  Chart'}) == 'Org Chart.svg'
