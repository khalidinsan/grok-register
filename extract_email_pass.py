#!/usr/bin/env python3
"""Scan logs/ for CREATED email/pass lines and write accounts/email_pass.txt."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Example:
# 16:33:32 [W1 ...] CREATED        email=xtnvcykw4@syzerf.my.id  name=Prabowo Subianto  pass=Nc0539157!a7#xgyVa4AC
CREATED_RE = re.compile(
    r"CREATED\s+email=(?P<email>\S+)\s+name=.+?\s+pass=(?P<pass>\S+)"
)


def extract_from_text(text: str) -> list[tuple[str, str]]:
    """Return ordered (email, password) pairs from log text."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = CREATED_RE.search(line)
        if m:
            out.append((m.group("email"), m.group("pass")))
    return out


def extract_from_logs(logs_dir: Path) -> list[tuple[str, str]]:
    """Scan all *.log under logs_dir; keep first occurrence per email."""
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    files = sorted(logs_dir.glob("*.log"))
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for email, password in extract_from_text(text):
            if email in seen:
                continue
            seen.add(email)
            pairs.append((email, password))
    return pairs


def write_email_pass(path: Path, pairs: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{email}:{password}" for email, password in pairs]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def self_check() -> None:
    sample = (
        "16:33:32 [W1 1/1 · #1/2 · remW 1 · ✓0 ✗0] CREATED        "
        "email=xtnvcykw4@syzerf.my.id  name=Prabowo Subianto  pass=Nc0539157!a7#xgyVa4AC"
    )
    got = extract_from_text(sample)
    expect = [("xtnvcykw4@syzerf.my.id", "Nc0539157!a7#xgyVa4AC")]
    if got != expect:
        raise SystemExit(f"self-check failed: got={got!r} expect={expect!r}")
    print("[ok] self-check passed")


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--logs-dir",
        type=Path,
        default=root / "logs",
        help="Directory containing *.log files",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=root / "accounts" / "email_pass.txt",
        help="Output path (email:password per line)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report counts only; do not write",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run built-in sample validation and exit",
    )
    args = p.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    if not args.logs_dir.is_dir():
        print(f"[err] logs dir not found: {args.logs_dir}", file=sys.stderr)
        return 1

    self_check()
    pairs = extract_from_logs(args.logs_dir)
    print(f"[ok] logs_dir={args.logs_dir}")
    print(f"[ok] unique accounts={len(pairs)}")
    if pairs:
        e0, p0 = pairs[0]
        print(f"[ok] first={e0}:{p0[:4]}…")
        e1, p1 = pairs[-1]
        print(f"[ok] last={e1}:{p1[:4]}…")

    if args.dry_run:
        print("[ok] dry-run: not writing")
        return 0

    write_email_pass(args.out, pairs)
    print(f"[ok] wrote {len(pairs)} lines → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
