"""Entity-name normalisation and comparison.

Keeps the original text, records every normalisation applied, and never
silently treats a substantive difference as a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SUFFIX_RULES = [
    (r"\bPVT\b", "PRIVATE"),
    (r"\bPRIV\b", "PRIVATE"),
    (r"\bLTD\b", "LIMITED"),
    (r"\bL\s?L\s?P\b", "LLP"),
    (r"\bCO\b(?=\s+PRIVATE|\s+LIMITED)", "COMPANY"),
]
_LEGAL_SUFFIXES = ["OPC PRIVATE LIMITED", "PRIVATE LIMITED", "LIMITED", "LLP"]
_PREFIXES = [r"^M\s*/\s*S\.?\s+", r"^MESSRS\.?\s+", r"^THE\s+"]


@dataclass
class NormalisedName:
    original: str
    normalised: str
    core: str  # without legal suffix
    suffix: str | None
    steps: list[str] = field(default_factory=list)


def normalise_name(name: str | None) -> NormalisedName:
    original = name or ""
    steps: list[str] = []
    s = original.upper().strip()
    if s != original.strip():
        steps.append("case-folded")
    s2 = s.replace("&", " AND ")
    if s2 != s:
        steps.append("'&' → 'AND'")
    s = s2
    for pat in _PREFIXES:
        s2 = re.sub(pat, "", s)
        if s2 != s:
            steps.append("removed honorific prefix")
        s = s2
    s2 = re.sub(r"[.,()\-']", " ", s)
    if s2 != s:
        steps.append("removed punctuation")
    s = re.sub(r"\s+", " ", s2).strip()
    for pat, repl in _SUFFIX_RULES:
        s2 = re.sub(pat, repl, s)
        if s2 != s:
            steps.append(f"expanded abbreviation → {repl}")
        s = s2
    s = re.sub(r"\s+", " ", s).strip()
    suffix = None
    core = s
    for suf in _LEGAL_SUFFIXES:
        if s.endswith(" " + suf) or s == suf:
            suffix = suf
            core = s[: -len(suf)].strip()
            break
    return NormalisedName(original, s, core, suffix, steps)


@dataclass
class NameComparison:
    relation: str  # exact | equivalent | suffix_differs | suffix_conflict | similar | different | missing
    a: NormalisedName
    b: NormalisedName
    similarity: float
    explanation: str


def _tokens(s: str) -> set[str]:
    return {t for t in s.split() if t not in {"AND", "OF"}}


def compare_names(a: str | None, b: str | None) -> NameComparison:
    na, nb = normalise_name(a), normalise_name(b)
    if not na.normalised or not nb.normalised:
        return NameComparison("missing", na, nb, 0.0, "One of the names is missing")
    if na.original.strip() == nb.original.strip():
        return NameComparison("exact", na, nb, 1.0, "Identical")
    if na.normalised == nb.normalised:
        steps = sorted(set(na.steps + nb.steps))
        return NameComparison("equivalent", na, nb, 1.0, "Same after normalisation (" + ", ".join(steps) + ")")
    if na.core and na.core == nb.core:
        if na.suffix and nb.suffix and na.suffix != nb.suffix:
            # Both state a legal form and they differ (e.g. Private Limited vs LLP): a different entity, not shorthand.
            return NameComparison("suffix_conflict", na, nb, 0.5,
                                  f"Same core name but different legal forms ({na.suffix} vs {nb.suffix})")
        return NameComparison("suffix_differs", na, nb, 0.95,
                              f"Same name; legal suffix omitted on one side ({na.suffix or 'none'} vs {nb.suffix or 'none'})")
    ta, tb = _tokens(na.core or na.normalised), _tokens(nb.core or nb.normalised)
    sim = len(ta & tb) / max(1, len(ta | tb))
    if sim >= 0.6:
        return NameComparison("similar", na, nb, round(sim, 2), "Names share most words but are not the same")
    return NameComparison("different", na, nb, round(sim, 2), "Names are different")
