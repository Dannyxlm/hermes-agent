"""Provider image-dimension error parsing, separated from turn recovery."""
from __future__ import annotations

import math
import re
from typing import Optional

def _image_error_max_dimension(error: Exception) -> Optional[int]:
    """Extract a provider-reported image dimension ceiling, if present."""
    parts = []
    for value in (error, getattr(error, "message", None), getattr(error, "body", None)):
        if value:
            try:
                parts.append(str(value))
            except Exception:
                pass
    text = " ".join(parts).lower()
    # OpenAI Codex Responses reports a tile-patch budget (ceil(w/32)×ceil(h/32))
    # instead of a pixel ceiling. A square image is the worst case for the budget,
    # so a per-side cap of isqrt(limit)*32 px keeps isqrt(limit)² ≤ limit — for the
    # 30000-patch ceiling that is 5536 px. Without this the caller falls back to
    # 8000 px and a 6000 px image that already exceeds the budget is skipped (#106337).
    if "patches after processing" in text:
        match = re.search(r"exceeding the limit of\s*(\d{2,7})", text)
        if not match:
            return None
        max_dimension = math.isqrt(int(match.group(1))) * 32
        return max_dimension if 512 <= max_dimension <= 8000 else None
    if "image" not in text or "dimension" not in text or "max allowed size" not in text:
        return None
    match = re.search(r"max allowed size(?:\s+for [^:]+)?:\s*(\d{3,5})\s*pixels?", text)
    if not match:
        return None
    try:
        max_dimension = int(match.group(1))
    except ValueError:
        return None
    return max_dimension if 512 <= max_dimension <= 8000 else None
