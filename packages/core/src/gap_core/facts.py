"""Facts and predicates (02 §4, 07 §6, 19 §3.3, §5).

Facts are frozen before evaluation. Unverified facts may only add caution: they can block, force a
tap or force clarify, but they can never clear a check (invariant 9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .hashing import hash_value
from .money import Money
from .timefmt import format_ts

ATTESTATION_CLASSES = ("source_signed", "capturer_signed", "payer_presented", "injected")


@dataclass(frozen=True)
class TermSet:
    terms: frozenset[str]
    vocabulary: str
    mapping_version: str


@dataclass(frozen=True)
class Fact:
    name: str
    value: object  # str | int | bool | datetime | Money | TermSet | tuple of one type
    source: str
    verified: bool
    attestation: str
    source_version: str = ""

    def __post_init__(self) -> None:
        if self.attestation not in ATTESTATION_CLASSES:
            raise ValueError(f"unknown attestation class {self.attestation!r}")
        if isinstance(self.value, float):
            raise TypeError("facts never hold floats (19 §5.3)")


def _jsonable(v):
    if isinstance(v, Money):
        return v.to_json()
    if isinstance(v, datetime):
        return format_ts(v)
    if isinstance(v, TermSet):
        return {"terms": sorted(v.terms), "vocabulary": v.vocabulary, "mapping_version": v.mapping_version}
    if isinstance(v, (tuple, list, frozenset, set)):
        return [_jsonable(x) for x in (sorted(v) if isinstance(v, (set, frozenset)) else v)]
    return v


class FactSet:
    def __init__(self, facts: list[Fact] | tuple[Fact, ...] = ()) -> None:
        self._by_name: dict[str, Fact] = {}
        for f in facts:
            if f.name in self._by_name:
                raise ValueError(f"duplicate fact {f.name!r}")
            self._by_name[f.name] = f

    def get(self, name: str, *, verified_only: bool = False) -> Fact | None:
        f = self._by_name.get(name)
        if f is None or (verified_only and not f.verified):
            return None
        return f

    def with_fact(self, fact: Fact) -> FactSet:
        return FactSet([f for n, f in self._by_name.items() if n != fact.name] + [fact])

    def hash(self) -> str:
        return hash_value(
            [
                {"name": f.name, "value": _jsonable(f.value), "source": f.source, "verified": f.verified, "attestation": f.attestation}
                for f in sorted(self._by_name.values(), key=lambda f: (f.name, f.source))
            ]
        )

    def __iter__(self):
        return iter(self._by_name.values())


# --- predicates ----------------------------------------------------------------

_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "exists")


def validate_predicate(p) -> None:
    if not isinstance(p, dict) or len(p) == 0:
        raise ValueError("predicate must be a non-empty object")
    if set(p) == {"all"} or set(p) == {"any"}:
        items = next(iter(p.values()))
        if not isinstance(items, (list, tuple)) or not items:
            raise ValueError("all/any need a non-empty list")
        for x in items:
            validate_predicate(x)
        return
    if set(p) == {"not"}:
        validate_predicate(p["not"])
        return
    if "fact" in p and "op" in p and set(p) <= {"fact", "op", "value"}:
        if p["op"] not in _OPS:
            raise ValueError(f"unknown op {p['op']!r}")
        if (p["op"] == "exists") == ("value" in p):
            raise ValueError("exists takes no value; every other op needs one")
        if isinstance(p.get("value"), float):
            raise TypeError("no floats in predicates")
        return
    raise ValueError(f"malformed predicate {p!r}")


def _compare(op: str, a, b) -> bool:
    try:
        if op == "eq":
            return a == b
        if op == "ne":
            return a != b
        if op == "in":
            return a in b
        if op == "lt":
            return a < b
        if op == "le":
            return a <= b
        if op == "gt":
            return a > b
        if op == "ge":
            return a >= b
    except (TypeError, ValueError):  # type or currency mismatch: the comparison is false
        return False
    return False


def evaluate_predicate(p: dict, facts: FactSet, *, verified_only: bool) -> bool:
    """Comparisons on a missing fact are false. With verified_only, unverified facts count as missing."""
    if "all" in p:
        return all(evaluate_predicate(x, facts, verified_only=verified_only) for x in p["all"])
    if "any" in p:
        return any(evaluate_predicate(x, facts, verified_only=verified_only) for x in p["any"])
    if "not" in p:
        return not evaluate_predicate(p["not"], facts, verified_only=verified_only)
    f = facts.get(p["fact"], verified_only=verified_only)
    if p["op"] == "exists":
        return f is not None
    if f is None:
        return False
    return _compare(p["op"], f.value, p["value"])


def predicate_facts(p: dict) -> set[str]:
    if "all" in p or "any" in p:
        return set().union(*(predicate_facts(x) for x in next(iter(p.values()))))
    if "not" in p:
        return predicate_facts(p["not"])
    return {p["fact"]}
