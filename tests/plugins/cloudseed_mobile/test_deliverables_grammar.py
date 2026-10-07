"""Desktop candidate grammar and assistant delivery provenance."""
import pytest
from plugins.cloudseed_mobile import deliverables as d
from tests.plugins.cloudseed_mobile.test_deliverables import project


@pytest.mark.parametrize('text,paths', [
    ('The parser recognizes MEDIA: followed by a path.', []),
    ('MEDIA:download', []),
    ('MEDIA:/granted/report.md — enjoy', ['/granted/report.md']),
    ('MEDIA:/tmp/a.pdf.', ['/tmp/a.pdf']),
    ('MEDIA:`/granted/my report.unknown`', ['/granted/my report.unknown']),
    ('MEDIA:/granted/a.pdf and MEDIA:/granted/b.md', ['/granted/a.pdf', '/granted/b.md']),
    ('MEDIA:/granted/CloudSeed Strategy/report.md', ['/granted/CloudSeed Strategy/report.md']),
    ('MEDIA:outputs/a.pdf,', ['outputs/a.pdf']),
    ('MEDIA:"/granted/stop!.md"', ['/granted/stop!.md']),
])
def test_desktop_media_candidates(text, paths):
    assert [value for value, _ in d.references(text)] == paths


@pytest.mark.parametrize('text,action', [
    ('The syntax is MEDIA:outputs/example.pdf', 'referenced'),
    ('```text\nMEDIA:outputs/example.pdf\n```', 'referenced'),
    ('~~~text\nMEDIA:outputs/example.pdf\n~~~', 'referenced'),
    ('`MEDIA:outputs/example.pdf`', 'referenced'),
    ('`example\nMEDIA:outputs/example.pdf\n`', 'referenced'),
    ('``example\nMEDIA:outputs/example.pdf\n``', 'referenced'),
    ('MEDIA:`outputs/example.pdf`', 'delivered'),
    ('MEDIA:outputs/example.pdf', 'delivered'),
    ('MEDIA:outputs/example.pdf — enjoy', 'referenced'),
])
def test_assistant_admission(text, action):
    rows = project([{'id': 1, 'role': 'assistant', 'content': text}])
    assert [(row['relative_path'], row['outcome']) for row in rows] == [('outputs/example.pdf', action)]


def test_tool_media_is_delivery_even_inline():
    rows = project([{'id': 1, 'role': 'tool', 'content': 'Result MEDIA:outputs/example.pdf ready'}])
    assert [(r['relative_path'], r['outcome']) for r in rows] == [('outputs/example.pdf', 'delivered')]
