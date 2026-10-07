"""Nearest-existing-skill lookup for autonomous skill creation.

The background review fork and the curator pass create skills with no user present. Each fork sees
the skill index from the parent's cached system prompt, which predates anything created later in
the same session, and the duplicate check on create matches exact names only. Together those let
one long conversation mint several near-identical skills (``personal-document-vault``,
``personal-records-vault``, ``personal-identity-documents``, ``personal-identity-paperwork``).

``nearest_skills`` ranks the installed skills against a proposed SKILL.md with TF-IDF cosine over
name + description + the head of the body. Short trigger descriptions alone carry too little signal;
measured on 29 real curator consolidations, adding ~3k chars of body raised recall@3 from 18 to 22.
The result feeds a one-time speed bump in ``skill_manage(create)``, not a hard block, so a ranking
miss costs one extra tool call rather than a lost skill.
"""

import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_BODY_CHARS = 3000
_EXCERPT_CHARS = 240
# Below this cosine the nearest skill shares little more than boilerplate vocabulary.
_MIN_SCORE = 0.10
_STOPWORDS = frozenset(
    "a an and any are as at be by for from in into is it its of on or our the this that to use "
    "used using via when with your you skill skills".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    for suffix, repl in (("ies", "y"), ("ing", ""), ("es", ""), ("ed", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + repl
    return word


def _tokens(text: str) -> List[str]:
    return [_stem(w) for w in _TOKEN_RE.findall(text.lower())
            if len(w) > 1 and not w.isdigit() and w not in _STOPWORDS]


def _skill_document(name: str, description: str, body: str) -> List[str]:
    # The name is the strongest class signal and the shortest field, so it counts twice.
    spaced = name.replace("-", " ").replace("_", " ")
    return _tokens(f"{spaced} {spaced} {description} {body[:_BODY_CHARS]}")


def _excerpt(description: str, body: str) -> str:
    match = re.search(r"^#+\s*When to Use\s*$\n+(.+?)(?=^#|\Z)", body, re.M | re.S | re.I)
    text = " ".join((match.group(1) if match else body).split())
    text = text or description
    return text[:_EXCERPT_CHARS] + ("…" if len(text) > _EXCERPT_CHARS else "")


def _installed_skills(exclude: Iterable[str]) -> List[Dict[str, str]]:
    """Every installed SKILL.md (local dir + skills.external_dirs), first copy of each name wins."""
    from agent.skill_utils import get_all_skills_dirs, parse_frontmatter
    from tools.skill_manager_tool import _iter_skill_dirs

    skipped, seen, rows = set(exclude), set(), []
    for root in get_all_skills_dirs():
        if not root.exists():
            continue
        for skill_dir in _iter_skill_dirs(root):
            try:
                text = (Path(skill_dir) / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            frontmatter, body = parse_frontmatter(text[: _BODY_CHARS + 4000])
            name = str(frontmatter.get("name") or skill_dir.name).strip()
            if not name or name in skipped or name in seen:
                continue
            seen.add(name)
            rows.append({"name": name, "description": str(frontmatter.get("description") or ""), "body": body})
    return rows


def nearest_skills(name: str, content: str, *, limit: int = 3,
                   exclude: Iterable[str] = ()) -> List[Dict[str, object]]:
    """Up to ``limit`` installed skills most similar to the proposed SKILL.md ``content``, best first:
    ``{"name", "description", "score", "excerpt"}``. Empty when nothing clears ``_MIN_SCORE``."""
    from agent.skill_utils import parse_frontmatter

    frontmatter, body = parse_frontmatter(content)
    query = _skill_document(name, str(frontmatter.get("description") or ""), body)
    installed = _installed_skills([name, *exclude])
    if not query or not installed:
        return []
    docs = [_skill_document(s["name"], s["description"], s["body"]) for s in installed]
    df = Counter(term for doc in docs for term in set(doc))
    total = len(docs)

    def _vector(terms: List[str]) -> Dict[str, float]:
        # Keep shared terms informative even in a one-skill library.
        weights = {t: (1 + math.log(c)) * (1 + math.log((total + 1) / (df.get(t, 0) + 1)))
                   for t, c in Counter(terms).items()}
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {t: w / norm for t, w in weights.items()}

    qv = _vector(query)
    scored = []
    for skill, doc in zip(installed, docs):
        dv = _vector(doc)
        score = sum(w * dv.get(t, 0.0) for t, w in qv.items())
        if score >= _MIN_SCORE:
            scored.append((score, skill))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"name": s["name"], "description": s["description"], "score": round(score, 3),
             "excerpt": _excerpt(s["description"], s["body"])} for score, s in scored[:limit]]


def format_overlap_refusal(name: str, matches: List[Dict[str, object]]) -> str:
    lines = "\n".join(f"  • {m['name']} — {m['description'] or m['excerpt']}" for m in matches)
    return (
        f"Not created yet: before adding '{name}' autonomously, compare it with the closest existing "
        f"skills:\n{lines}\n"
        f"If one of these covers the same class of task, skill_view it and patch it (or add a "
        f"references/ file under it) instead. If '{name}' is genuinely a different class, repeat the "
        f"same create once and it will go through.")


def overlap_candidates_or_none(name: str, content: str) -> Optional[List[Dict[str, object]]]:
    """``nearest_skills`` that never raises: a ranking failure must not block a create."""
    try:
        return nearest_skills(name, content) or None
    except Exception:
        logger.warning("skill overlap lookup failed for %s; allowing create", name, exc_info=True)
        return None
