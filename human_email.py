"""
Human-looking catch-all local-parts (not pure random garbage).

Patterns (weighted):
  first.last
  firstlast
  first.last.NN
  first_last
  f.last
  first.l
  firstlastNN
  first.YYYY
  nickname + digits

Uses name.txt (Given|Family) when available.
"""

from __future__ import annotations

import random
import re
import string
from pathlib import Path
from typing import List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent
_NAME_FILE = _ROOT / "name.txt"

# fallback English-ish pools if name.txt thin
_FALLBACK_FIRST = [
    "alex", "jordan", "taylor", "morgan", "casey", "riley", "quinn", "avery",
    "parker", "sage", "noah", "liam", "emma", "olivia", "mia", "lucas",
    "mason", "sophia", "james", "oliver", "amelia", "charlotte", "henry",
    "budi", "siti", "andi", "rina", "dewi", "agus", "rani", "dimas",
]
_FALLBACK_LAST = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller",
    "davis", "wilson", "moore", "lee", "martin", "clark", "hall", "young",
    "santoso", "wijaya", "pratama", "saputra", "kusuma", "nugroho", "hidayat",
]


def _slug(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s[:16]


def load_name_pairs(path: Optional[Path] = None) -> List[Tuple[str, str]]:
    p = path or _NAME_FILE
    pairs: List[Tuple[str, str]] = []
    if not p.is_file():
        return pairs
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return pairs
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            a, _, b = line.partition("|")
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            a, b = parts[0], parts[-1]
        fa, la = _slug(a), _slug(b)
        if fa and la:
            pairs.append((fa, la))
    return pairs


def pick_names(pairs: Optional[List[Tuple[str, str]]] = None) -> Tuple[str, str]:
    if pairs is None:
        pairs = load_name_pairs()
    if pairs:
        return random.choice(pairs)
    return random.choice(_FALLBACK_FIRST), random.choice(_FALLBACK_LAST)


def _pick_names(pairs: List[Tuple[str, str]]) -> Tuple[str, str]:
    return pick_names(pairs)


def human_local_part(
    *,
    given: str = "",
    family: str = "",
    pairs: Optional[List[Tuple[str, str]]] = None,
) -> str:
    """Build one human-ish local-part (no domain)."""
    if given and family:
        first, last = _slug(given), _slug(family)
    else:
        first, last = _pick_names(pairs if pairs is not None else load_name_pairs())
    if not first:
        first = random.choice(_FALLBACK_FIRST)
    if not last:
        last = random.choice(_FALLBACK_LAST)

    fi, li = first[0], last[0]
    yy = random.randint(1988, 2005)
    nn = random.randint(1, 99)
    nnn = random.randint(10, 999)

    # Always end with digits for uniqueness (user request)
    digits = str(random.randint(10, 9999))
    patterns = [
        (20, lambda: f"{first}.{last}{digits}"),
        (16, lambda: f"{first}{last}{digits}"),
        (12, lambda: f"{first}_{last}{digits}"),
        (10, lambda: f"{fi}.{last}{digits}"),
        (10, lambda: f"{first}.{li}{digits}"),
        (8, lambda: f"{first}.{last}.{digits}"),
        (8, lambda: f"{first}-{last}{digits}"),
        (6, lambda: f"{first}{yy}{nn}"),
        (6, lambda: f"{first}{nnn}"),
    ]
    weights = [w for w, _ in patterns]
    builders = [b for _, b in patterns]
    local = random.choices(builders, weights=weights, k=1)[0]()
    local = re.sub(r"[^a-z0-9._+-]", "", local.lower())
    local = re.sub(r"[._+-]{2,}", ".", local).strip("._+-")
    # force trailing digits if missing
    if not re.search(r"\d+$", local):
        local = f"{local}{random.randint(10, 9999)}"
    if len(local) < 5:
        local = f"{first}{random.randint(100, 9999)}"
    if len(local) > 32:
        # keep trailing digits
        m = re.search(r"(\d+)$", local)
        dig = m.group(1) if m else str(random.randint(10, 99))
        local = local[: 32 - len(dig)].rstrip("._+-") + dig
    if local and not local[0].isalnum():
        local = "u" + local
    return local or f"user{random.randint(1000, 9999)}"


def human_catchall_email(
    domain: str,
    *,
    given: str = "",
    family: str = "",
) -> str:
    domain = (domain or "").lstrip("@").strip()
    if not domain:
        raise ValueError("empty email domain")
    local = human_local_part(given=given, family=family)
    return f"{local}@{domain}"
