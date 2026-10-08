"""Desktop's artifact rule, ported exactly so box, Desktop and phone agree.

Source of truth: ``apps/desktop/src/lib/artifact-detect.ts`` (detection, title,
slug) plus the prose and structured-text guards from ``markdown-code.ts``. The
shared cases in ``tests/fixtures/artifact_detect_cases.json`` are run by Desktop
vitest, this package's pytest and the app's XCTest.

JavaScript semantics are spelled out rather than assumed:
- string lengths and slices count UTF-16 code units;
- ``\\s`` and ``trim()`` use the ECMAScript whitespace set, ``\\w`` and ``\\b`` are
  ASCII, and ``/i`` folds ASCII letters only (hence ``re.ASCII`` everywhere);
- multiline ``^``/``$`` also break on ``\\r``, U+2028 and U+2029.

``fences()`` mirrors how Desktop's markdown pipeline turns a message into the
``(language, code)`` pairs its code-block renderer sees (``normalizeFenceBlocks``
in ``markdown-preprocess.ts``). It does not model list or blockquote containers.
"""
import math
import re
import unicodedata

# ECMAScript WhiteSpace + LineTerminator: what JS `\s` matches and `trim()` removes.
_WS = '\t\n\x0b\x0c\r \u00a0\u1680' + ''.join(map(chr, range(0x2000, 0x200b))) + '\u2028\u2029\u202f\u205f\u3000\ufeff'
_S = f'[{_WS}]'
_NS = f'[^{_WS}]'
_BOL = r'(?:^|(?<=[\n\r\u2028\u2029]))'
_EOL = r'(?=[\n\r\u2028\u2029]|\Z)'
_A = re.ASCII
_AI = re.ASCII | re.IGNORECASE


def _rx(pattern, flags=_A):
    return re.compile(pattern, flags)


def js_trim(value):
    return value.strip(_WS)


def utf16_len(value):
    return len(value.encode('utf-16-le', 'surrogatepass')) // 2


def utf16_slice(value, end):
    """``value.slice(0, end)``; may end in a lone high surrogate, exactly like JS."""
    if len(value) <= end // 2:
        return value
    return value.encode('utf-16-le', 'surrogatepass')[:2 * end].decode('utf-16-le', 'surrogatepass')


def _count(pattern, text):
    return sum(1 for _ in pattern.finditer(text))


# --- markdown-code.ts -------------------------------------------------------

_VALID_LANGUAGE = _rx(r'[a-z0-9][a-z0-9+#-]*\Z', _AI)
_SPLIT_WS = _rx(_S)
_SPLIT_WS_RUN = _rx(_S + '+')
NON_CODE_FENCE_LANGUAGES = frozenset({'', 'text', 'plain', 'plaintext', 'md', 'markdown'})
COMMON_CODE_LANGUAGES = frozenset({
    'bash', 'c', 'cpp', 'css', 'diff', 'go', 'html', 'java', 'javascript', 'js', 'json', 'jsx',
    'markdown', 'md', 'php', 'python', 'py', 'ruby', 'rust', 'rs', 'sh', 'sql', 'swift', 'tsx',
    'ts', 'typescript', 'xml', 'yaml', 'yml',
})


def sanitize_language_tag(tag):
    first = _SPLIT_WS.split(js_trim(tag), maxsplit=1)[0]
    return first.lower() if _VALID_LANGUAGE.match(first) and len(first) <= 16 else ''


_PROSE_LINE = _rx(r'[A-Za-z0-9"\'`*-]')
_CODE_SIGNALS = (
    _rx(rf'(?:{_BOL}|{_S})(?:const|let|var|function|class|import|export|return|if|for|while|switch)\b', _AI),
    _rx(r'=>|==|===|!=|!==|\{|\}|;|</?[a-z][^>]*>', _AI),
    _rx(rf'{_BOL}{_S}*(?:#include|SELECT|INSERT|UPDATE|DELETE|CREATE|DROP)\b', _AI),
)
_BOLD = _rx(r'\*\*[^*]+\*\*')
_INLINE_CODE = _rx(r'`[^`\n]+`')
_BULLET_LINE = _rx(rf'{_BOL}{_S}*[-*]{_S}+{_NS}+')
_URL_LINE = _rx(rf'{_BOL}{_S}*https?://{_NS}+{_S}*{_EOL}', _AI)
_SENTENCE_PUNCTUATION = _rx(rf'[.!?](?:{_S}|\Z)')
_CONFIG_SEPARATOR_LINE = _rx(rf'[A-Za-z0-9_][A-Za-z0-9_.-]*{_S}*[:=]{_S}*{_NS}')
_CONFIG_KEY = _rx(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')
_INDENTED_LINE = _rx(rf'{_S}+{_NS}')
_BULLET_INFO = _rx(rf'[-*+]{_S}')
_URL_INFO = _rx(r'https?://')


def _prose_line_count(body):
    return sum(1 for line in body.split('\n') if (trimmed := js_trim(line)) and _PROSE_LINE.match(trimmed))


def _code_signals(body):
    trimmed = js_trim(body)
    return {
        'bullet_lines': _count(_BULLET_LINE, trimmed),
        'code_signals': sum(_count(pattern, trimmed) for pattern in _CODE_SIGNALS),
        'has_markdown': _count(_BOLD, trimmed) + _count(_INLINE_CODE, trimmed) > 0,
        'prose_lines': _prose_line_count(trimmed),
        'trimmed': trimmed,
        'url_lines': _count(_URL_LINE, trimmed),
    }


def _is_config_directive_line(line):
    trimmed = js_trim(line)
    if _CONFIG_SEPARATOR_LINE.match(trimmed):
        return True
    tokens = _SPLIT_WS_RUN.split(trimmed)
    return 2 <= len(tokens) <= 3 and bool(_CONFIG_KEY.match(tokens[0]))


def is_likely_structured_text(body):
    lines = [line for line in body.split('\n') if js_trim(line)]
    if len(lines) < 2:
        return False
    if any(_INDENTED_LINE.match(line) for line in lines):
        return True
    if any(_SENTENCE_PUNCTUATION.search(js_trim(line)) for line in lines):
        return False
    config_lines = sum(1 for line in lines if _is_config_directive_line(line))
    return config_lines >= max(2, math.ceil(len(lines) * 0.6))


def is_likely_prose_fence(info, body):
    trimmed_info = js_trim(info)
    raw_info = trimmed_info.lower()
    language = sanitize_language_tag(info)
    info_token = _SPLIT_WS_RUN.split(trimmed_info, maxsplit=1)[0]
    has_info_tail = bool(trimmed_info) and trimmed_info != info_token
    if _BULLET_INFO.match(raw_info) or _URL_INFO.match(raw_info):
        return True
    signals = _code_signals(body)
    if not signals['trimmed']:
        return False
    if (has_info_tail and signals['code_signals'] <= 2
            and (signals['prose_lines'] >= 2 or signals['bullet_lines'] >= 1 or signals['url_lines'] >= 1)):
        return True
    if language not in NON_CODE_FENCE_LANGUAGES:
        return False
    if is_likely_structured_text(body):
        return False
    return ((signals['bullet_lines'] >= 2 and signals['has_markdown'] and signals['code_signals'] <= 2)
            or (signals['prose_lines'] >= 3 and signals['code_signals'] == 0))


def is_likely_prose_code_block(language, code):
    clean_language = sanitize_language_tag(language or '')
    signals = _code_signals(code or '')
    if not signals['trimmed'] or signals['code_signals'] >= 3:
        return False
    if signals['bullet_lines'] >= 1 and (signals['has_markdown'] or signals['prose_lines'] >= 2):
        return True
    if is_likely_structured_text(code or ''):
        return False
    if clean_language in NON_CODE_FENCE_LANGUAGES:
        return signals['prose_lines'] >= 3 and signals['code_signals'] == 0
    return clean_language not in COMMON_CODE_LANGUAGES and signals['prose_lines'] >= 2 and signals['code_signals'] <= 1


# --- artifact-detect.ts -----------------------------------------------------

_HTML_DOC = _rx(rf'<!doctype{_S}+html|<html[{_WS}>]|<head[{_WS}>]|<body[{_WS}>]', _AI)
_HTML_TAG = _rx(rf'<[a-z][a-z0-9-]*(?:{_S}[^>]*)?>', _AI)
_SVG_TAG = _rx(rf'<svg[{_WS}>]', _AI)
HTML_DOC_MIN_CHARS = 160
HTML_FRAGMENT_MIN_CHARS = 1200
SVG_MIN_CHARS = 2000
CODE_MIN_LINES = 48
CODE_MIN_CHARS = 3000
HTML_LANGUAGES = frozenset({'html', 'htm', 'xhtml'})
NON_ARTIFACT_LANGUAGES = frozenset({
    '', 'console', 'diff', 'listing', 'log', 'logs', 'markdown', 'md', 'mermaid', 'output',
    'patch', 'plain', 'plaintext', 'shell-session', 'stdout', 'text', 'txt',
})
_TAG = _rx(r'<[^>]*>')
_WS_RUN = _rx(_S + '+')
_TITLE_FROM_TAG = {tag: _rx(rf'<{tag}[^>]*>([\s\S]*?)</{tag}>', _AI) for tag in ('h1', 'title')}
_CODE_DECLARATION = _rx(
    rf'(?:^|\n){_S}*(?:export{_S}+)?(?:default{_S}+)?(?:async{_S}+)?'
    rf'(?:function|class|struct|interface|enum|trait|impl|def|fn){_S}+([A-Za-z_$][A-Za-z0-9_$]*)')
_FILENAME_COMMENT = _rx(rf'^{_S}*(?://|#|--|<!--|/\*){_S}*([A-Za-z0-9_./-]+\.[a-z0-9]{{1,8}})\b', _AI)


def _strip_tags(value):
    return js_trim(_WS_RUN.sub(' ', _TAG.sub(' ', value)))


def _title_from_tag(content, tag):
    match = _TITLE_FROM_TAG[tag].search(content)
    return utf16_slice(_strip_tags(match.group(1) or ''), 80) if match else ''


def _code_title(language, content):
    head = utf16_slice(content, 2000)
    file_name = _FILENAME_COMMENT.search(head)
    if file_name:
        return file_name.group(1)
    declaration = _CODE_DECLARATION.search(head)
    if declaration:
        return declaration.group(1)
    return language


def detect_artifact(language, code):
    """``detectArtifact``: ``{kind, language, title}`` or None. ``language=None`` is JS undefined."""
    trimmed = js_trim(code or '')
    if not trimmed:
        return None
    clean = sanitize_language_tag(language or '')
    length = utf16_len(trimmed)
    if clean in HTML_LANGUAGES:
        document = _HTML_DOC.search(trimmed) is not None
        if ((document and length >= HTML_DOC_MIN_CHARS)
                or (not document and length >= HTML_FRAGMENT_MIN_CHARS and _HTML_TAG.search(trimmed))):
            title = _title_from_tag(trimmed, 'title') or _title_from_tag(trimmed, 'h1') or 'HTML'
            return {'kind': 'html', 'language': clean, 'title': title}
        return None
    if clean == 'svg':
        if length >= SVG_MIN_CHARS and _SVG_TAG.search(trimmed):
            return {'kind': 'svg', 'language': clean, 'title': _title_from_tag(trimmed, 'title') or 'SVG'}
        return None
    if clean in NON_ARTIFACT_LANGUAGES:
        return None
    if length < CODE_MIN_CHARS and trimmed.count('\n') + 1 < CODE_MIN_LINES:
        return None
    if is_likely_prose_code_block(clean, trimmed):
        return None
    return {'kind': 'code', 'language': clean, 'title': _code_title(clean, trimmed)}


def artifact_slug(kind, language, title):
    """``artifactSlug``: the per-chat identity of one artifact the model iterates on."""
    folded = []
    for char in title.lower():
        if unicodedata.category(char)[0] in 'LN':
            folded.append(char)
        elif not folded or folded[-1] != '-':
            folded.append('-')
    slug = utf16_slice(''.join(folded).strip('-'), 48)
    return f'{kind}:{language}:{slug or "untitled"}'


_DOWNLOAD_EXTENSION = {
    'bash': '.sh', 'c': '.c', 'cpp': '.cpp', 'csharp': '.cs', 'css': '.css', 'go': '.go',
    'htm': '.html', 'html': '.html', 'java': '.java', 'javascript': '.js', 'js': '.js',
    'json': '.json', 'jsx': '.jsx', 'kotlin': '.kt', 'php': '.php', 'py': '.py', 'python': '.py',
    'rb': '.rb', 'rs': '.rs', 'ruby': '.rb', 'rust': '.rs', 'sh': '.sh', 'sql': '.sql',
    'svg': '.svg', 'swift': '.swift', 'toml': '.toml', 'ts': '.ts', 'tsx': '.tsx',
    'typescript': '.ts', 'xhtml': '.html', 'xml': '.xml', 'yaml': '.yaml', 'yml': '.yaml',
}
_HAS_EXTENSION = _rx(r'\.[a-z0-9]{1,8}\Z', _AI)
_MARKUP_EXTENSIONS = ('.html', '.htm', '.xhtml', '.svg')


def display_name(detection):
    """Readable Files name (``Pricing page.html``). Its extension is what the app reads
    to pick the HTML, SVG or code reader, so code never borrows a markup extension."""
    kind, language = detection['kind'], detection['language']
    base = ' '.join(detection['title'].split())[:80] or kind.upper()
    extension = {'html': '.html', 'svg': '.svg'}.get(kind) or _DOWNLOAD_EXTENSION.get(language) or '.' + language
    lower = base.lower()
    if kind == 'code' and _HAS_EXTENSION.search(base) and not lower.endswith(_MARKUP_EXTENSIONS):
        return base
    return base if lower.endswith(extension) else base + extension


# --- markdown-preprocess.ts normalizeFenceBlocks ----------------------------

_FENCE_LINE = _rx(r'([ \t]*)(`{3,}|~{3,})([^\n]*)\Z')
_LOCAL_PREVIEW_ONLY = _rx(r'https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::[0-9]+)?/?\Z', _AI)
_URL_ONLY_LINE = _rx(rf'{_S}*https?://{_NS}+{_S}*\Z', _AI)


def _closing_fence(lines, start, marker):
    for cursor in range(start + 1, len(lines)):
        match = _FENCE_LINE.match(lines[cursor])
        if match and not js_trim(match.group(3)) and match.group(2)[0] == marker[0] and len(match.group(2)) >= len(marker):
            return cursor
    return -1


def _dedent(lines, indent):
    width = len(indent)
    out = []
    for line in lines:
        cut = 0
        while cut < width and cut < len(line) and line[cut] in ' \t':
            cut += 1
        out.append(line[cut:])
    return '\n'.join(out)


def fences(text):
    """Yield ``(language, code)`` for every fence Desktop renders as a code block.

    Invalid openers stay text, empty/localhost/URL-only blocks are dropped, prose
    fences are unwrapped, ``math`` goes to KaTeX, and an unclosed fence runs to the
    end of the message (Desktop renders it the same way once streaming stops).
    """
    lines = text.split('\n')
    index = 0
    while index < len(lines):
        match = _FENCE_LINE.match(lines[index])
        if not match:
            index += 1
            continue
        indent, marker, info = match.group(1), match.group(2), js_trim(match.group(3))
        language = sanitize_language_tag(_SPLIT_WS_RUN.split(info, maxsplit=1)[0])
        if info and not language:
            index += 1
            continue
        close = _closing_fence(lines, index, marker)
        body_lines = lines[index + 1:close if close != -1 else len(lines)]
        body = '\n'.join(body_lines)
        trimmed = js_trim(body)
        if close == -1:
            if trimmed and not is_likely_prose_fence(info, body) and language != 'math':
                yield language, _dedent(body_lines, indent)
            if trimmed:
                return
            index += 1
            continue
        index = close + 1
        if (not trimmed or _LOCAL_PREVIEW_ONLY.match(trimmed)
                or all(_URL_ONLY_LINE.match(line) for line in body_lines if js_trim(line))
                or is_likely_prose_fence(info, body) or language == 'math'):
            continue
        yield language, _dedent(body_lines, indent)
