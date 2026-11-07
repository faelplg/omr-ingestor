"""Shared navigation recognition for raw-export review and text cleanup."""

import re

_NAVIGATION_SOUND_ATTRIBUTES = frozenset({"dacapo", "dalsegno", "tocoda", "fine", "coda", "segno"})


_NAVIGATION = re.compile(
    r"(?<!\w)(?:d\s*\.?\s*[cs]\s*\.?(?!\w)|(?:al\s+)?fine\b|coda\b|fim\b)",
    re.IGNORECASE,
)
