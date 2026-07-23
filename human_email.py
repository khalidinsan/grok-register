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


def _rand_digits(n: int = 0) -> str:
    """n=0 → length 2–4 random."""
    if n <= 0:
        n = random.randint(2, 4)
    return "".join(random.choice(string.digits) for _ in range(n))


def _rand_alnum(n: int = 0, *, start_letter: bool = False) -> str:
    """Random a-z0-9 suffix for uniqueness (not digits-only)."""
    if n <= 0:
        n = random.randint(3, 6)
    alphabet = string.ascii_lowercase + string.digits
    if start_letter or n >= 1:
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choice(alphabet) for _ in range(max(0, n - 1)))
        return first + rest
    return "".join(random.choice(alphabet) for _ in range(n))


def _unique_tail() -> str:
    """
    Trailing uniqueness blob: mix of digits and alnum strings.
    Examples: 42k9, x7m2, 8831a, k3p9q1
    """
    kind = random.choices(
        ("alnum", "digits_alnum", "alnum_digits", "digits", "long_alnum"),
        weights=(28, 24, 22, 12, 14),
        k=1,
    )[0]
    if kind == "alnum":
        return _rand_alnum(random.randint(3, 6), start_letter=True)
    if kind == "digits_alnum":
        return _rand_digits(random.randint(2, 3)) + _rand_alnum(
            random.randint(2, 4), start_letter=True
        )
    if kind == "alnum_digits":
        return _rand_alnum(random.randint(2, 4), start_letter=True) + _rand_digits(
            random.randint(2, 3)
        )
    if kind == "long_alnum":
        return _rand_alnum(random.randint(5, 8), start_letter=True)
    # digits-only still allowed sometimes (looks natural after year/name)
    return _rand_digits(random.randint(3, 5))


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
    # Unique tail: digits + alnum mix (not digits-only)
    tail = _unique_tail()

    patterns = [
        (20, lambda: f"{first}.{last}{tail}"),
        (16, lambda: f"{first}{last}{tail}"),
        (12, lambda: f"{first}_{last}{tail}"),
        (10, lambda: f"{fi}.{last}{tail}"),
        (10, lambda: f"{first}.{li}{tail}"),
        (8, lambda: f"{first}.{last}.{tail}"),
        (8, lambda: f"{first}-{last}{tail}"),
        (6, lambda: f"{first}{yy}{tail}"),
        (6, lambda: f"{first}{tail}"),
        (4, lambda: f"{first}.{tail}"),
    ]
    weights = [w for w, _ in patterns]
    builders = [b for _, b in patterns]
    local = random.choices(builders, weights=weights, k=1)[0]()
    local = re.sub(r"[^a-z0-9._+-]", "", local.lower())
    local = re.sub(r"[._+-]{2,}", ".", local).strip("._+-")
    # ensure some uniqueness suffix (digit or alnum) remains
    if not re.search(r"[a-z0-9]{2,}$", local) or len(local) < 6:
        local = f"{local}{_unique_tail()}"
    if len(local) < 5:
        local = f"{first}{_unique_tail()}"
    if len(local) > 32:
        # keep trailing uniqueness chunk
        m = re.search(r"([a-z0-9]{2,})$", local)
        dig = m.group(1) if m else _unique_tail()
        if len(dig) > 10:
            dig = dig[-8:]
        local = local[: 32 - len(dig)].rstrip("._+-") + dig
    if local and not local[0].isalnum():
        local = "u" + local
    return local or f"user{_unique_tail()}"


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
