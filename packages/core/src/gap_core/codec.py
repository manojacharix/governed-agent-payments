"""A lossless JSON codec for core types, so a decision's exact inputs can be stored and replayed (06 §1).

encode() turns dataclasses, tuples, sets, maps, datetimes and None into a canonicalisable tree of
tagged objects; decode() rebuilds identical values (dataclass validation runs again on the way back).
The tree contains no floats and no nulls, so canonical() and hash_value() apply to it directly.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from . import authority, evaluate, facts, money, proposal
from .canonical import canonical

_TYPES = {}
for _mod in (authority, evaluate, facts, money, proposal):
    for _name, _obj in vars(_mod).items():
        if isinstance(_obj, type) and dataclasses.is_dataclass(_obj) and _obj.__module__ == _mod.__name__:
            _TYPES[_name] = _obj


class CodecError(ValueError):
    """A value cannot be encoded or decoded."""


def encode(v):
    if v is None:
        return {"$t": "none"}
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, str):
        return v
    if isinstance(v, float):
        raise CodecError("floats are not allowed")
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise CodecError("naive datetime")
        return {"$t": "dt", "v": v.isoformat()}
    if isinstance(v, facts.FactSet):
        return {"$t": "FactSet", "v": [encode(f) for f in sorted(v, key=lambda f: (f.name, f.source))]}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        name = type(v).__name__
        if _TYPES.get(name) is not type(v):
            raise CodecError(f"unregistered type {name}")
        return {"$t": name, **{f.name: encode(getattr(v, f.name)) for f in dataclasses.fields(v)}}
    if isinstance(v, tuple):
        return {"$t": "tuple", "v": [encode(x) for x in v]}
    if isinstance(v, list):
        return [encode(x) for x in v]
    if isinstance(v, (set, frozenset)):
        items = [encode(x) for x in v]
        return {"$t": "set", "v": sorted(items, key=canonical)}
    if isinstance(v, dict):
        pairs = [[encode(k), encode(x)] for k, x in v.items()]
        return {"$t": "map", "v": sorted(pairs, key=lambda p: canonical(p[0]))}
    raise CodecError(f"cannot encode {type(v).__name__}")


def decode(t):
    if isinstance(t, (bool, int, str)):
        return t
    if isinstance(t, list):
        return [decode(x) for x in t]
    if not isinstance(t, dict) or "$t" not in t:
        raise CodecError(f"malformed node {t!r}")
    tag = t["$t"]
    if tag == "none":
        return None
    if tag == "dt":
        return datetime.fromisoformat(t["v"])
    if tag == "tuple":
        return tuple(decode(x) for x in t["v"])
    if tag == "set":
        return frozenset(decode(x) for x in t["v"])
    if tag == "map":
        return {_hashable(decode(k)): decode(x) for k, x in t["v"]}
    if tag == "FactSet":
        return facts.FactSet([decode(x) for x in t["v"]])
    cls = _TYPES.get(tag)
    if cls is None:
        raise CodecError(f"unknown type {tag}")
    kwargs = {k: decode(x) for k, x in t.items() if k != "$t"}
    names = {f.name for f in dataclasses.fields(cls)}
    if set(kwargs) != names:
        raise CodecError(f"{tag}: fields {sorted(set(kwargs) ^ names)} do not match")
    return cls(**kwargs)


def _hashable(k):
    return tuple(k) if isinstance(k, list) else k
