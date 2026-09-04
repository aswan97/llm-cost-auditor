#!/usr/bin/env python3
"""Enforce the CLAUDE.md rule: never write a price as a literal.

Scans the package for numeric literals bound to price-shaped names outside the
pricing module. Rates, multipliers, and thresholds must come from the price
table (SPEC.md §7.1) so they stay in sync with the catalog and with the rate in
force at each request's own timestamp.

Exits 0 when clean, 1 with a report when a literal is found, and 0 when there is
no source tree yet.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path("src")

# Paths allowed to contain price data or price-shaped constants.
ALLOWLIST_PARTS = {"pricing"}

# Name fragments that make a numeric literal a price, not an ordinary number.
PRICE_NAME_FRAGMENTS = (
    "price",
    "rate",
    "cost",
    "usd",
    "multiplier",
    "discount",
    "premium",
    "per_mtok",
    "per_token",
    "min_cacheable",
)

# Numbers that are never a price, whatever they are called.
BENIGN = {0, 0.0, 1, 1.0, -1}

SUPPRESSION = "noqa: price-literal"


def is_price_name(name: str) -> bool:
    lowered = name.lower()
    return any(fragment in lowered for fragment in PRICE_NAME_FRAGMENTS)


def literal_value(node: ast.AST) -> float | int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = literal_value(node.operand)
        return None if inner is None else -inner
    return None


def targets(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Attribute):
                names.append(target.attr)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names.append(node.target.id)
    return names


def check_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    problems: list[str] = []
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        found: list[tuple[str, float | int]] = []

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = literal_value(node.value) if node.value is not None else None
            if value is not None and value not in BENIGN:
                found += [(name, value) for name in targets(node) if is_price_name(name)]

        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                value = literal_value(kw.value)
                if kw.arg and value is not None and value not in BENIGN and is_price_name(kw.arg):
                    found.append((kw.arg, value))

        for name, value in found:
            line_no = getattr(node, "lineno", 0)
            line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            if SUPPRESSION in line:
                continue
            problems.append(
                f"{path}:{line_no}: price literal `{name} = {value}` — "
                f"read it from the pricing module instead"
            )

    return problems


def main() -> int:
    if not SRC.exists():
        print("no src/ tree yet — nothing to check")
        return 0

    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if ALLOWLIST_PARTS & set(path.parts):
            continue
        problems += check_file(path)

    if problems:
        print("Price literals found (see CLAUDE.md: never write a price as a literal):\n")
        for problem in problems:
            print(f"  {problem}")
        print(
            f"\n{len(problems)} violation(s). Use pricing.rate(...) / pricing.multiplier(...), "
            f"or append `# {SUPPRESSION}` with a justification if this genuinely is not a price."
        )
        return 1

    print("no price literals found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
