"""Units of work over working state (19 §12).

The flow and identity layers keep their state in Tables: dict-like views of one kind of object. A Store
decides where those objects live:

- MemoryStore: plain dicts in one process (tests, the simulator, single-process demos).
- PgStore: the state_items table in Postgres, so several host processes can share state and any of them
  can restart without losing it.

With PgStore every operation runs in a unit of work: one transaction under a per-payer advisory lock (so two
processes never work on the same payer at once), loading objects on first use, letting the code mutate them in
place, and on exit writing back exactly the objects that changed. Each write names the version it read, so a
lost update raises StateConflict instead of passing silently, even if a lock scope were wrong.
checkpoint() commits mid-unit before an irreversible step (calling a merchant). The locks are session-level, so
they survive that commit: no other process can slip in between the commit and the rest of the operation (it
could otherwise evaluate a cap before this operation's hold is posted). They are released when the unit ends.

Access outside a unit runs as a unit of its own. Objects (dataclasses) handed out stay live for as long as a
caller holds them: whenever the store re-reads their row it refreshes them in place, so a reference kept from an
earlier operation shows the current state, as it would in memory. Changes made to them outside a unit are not saved.
"""

from __future__ import annotations

import json
import weakref
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator

from psycopg.types.json import Jsonb


class StateConflict(RuntimeError):
    """Another process changed an object this unit read. The unit is rolled back; the request can be retried."""


class Kind:
    """How one kind of object is stored: key, lock scope, tag, status and expiry derived from the object."""

    def __init__(self, name: str, dump: Callable[[Any], Any], load: Callable[[Any], Any], *,
                 scope: Callable[[Any], str] = lambda o: "", tag: Callable[[Any], str] = lambda o: "",
                 status: Callable[[Any], str] = lambda o: "", expires: Callable[[Any], datetime | None] = lambda o: None) -> None:
        self.name, self.dump, self.load = name, dump, load
        self.scope, self.tag, self.status, self.expires = scope, tag, status, expires


# --- memory ---------------------------------------------------------------------------------------------

class MemoryTable(dict):
    """A dict with the extra query methods PgTable offers."""

    def __init__(self, kind: Kind) -> None:
        super().__init__()
        self.kind = kind

    def where(self, *, scope: str | None = None, tag: str | None = None, status: tuple | None = None) -> list:
        k = self.kind
        return [o for o in list(super().values()) if (scope is None or k.scope(o) == scope) and (tag is None or k.tag(o) == tag)
                and (status is None or k.status(o) in status)]

    def scope_of(self, key: str) -> str | None:
        o = super().get(key)
        return None if o is None else self.kind.scope(o)


class MemoryStore:
    durable = False

    def __init__(self) -> None:
        self._tables: dict[str, MemoryTable] = {}

    def table(self, kind: Kind) -> MemoryTable:
        return self._tables.setdefault(kind.name, MemoryTable(kind))

    @contextmanager
    def unit(self, scope: str | None = None) -> Iterator[None]:
        yield

    def lock(self, scope: str) -> None:
        pass

    def checkpoint(self) -> None:
        pass


# --- Postgres ---------------------------------------------------------------------------------------------

_MISSING = object()


class _Unit:
    def __init__(self) -> None:
        self.cache: dict[tuple[str, str], Any] = {}       # (kind, key) -> object (None if known absent)
        self.loaded: dict[tuple[str, str], tuple] = {}    # (kind, key) -> (version, snapshot json) as read
        self.deleted: set[tuple[str, str]] = set()
        self.locks: set[str] = set()


class PgTable:
    def __init__(self, store: "PgStore", kind: Kind) -> None:
        self.store, self.kind = store, kind

    # dict protocol --------------------------------------------------------------------------------------
    def get(self, key: str, default=None):
        with self.store.unit():
            o = self.store._get(self.kind, key)
        return default if o is None else o

    def __getitem__(self, key: str):
        o = self.get(key, _MISSING)
        if o is _MISSING:
            raise KeyError(key)
        return o

    def __contains__(self, key) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    def __setitem__(self, key: str, obj) -> None:
        with self.store.unit():
            self.store._put(self.kind, key, obj)

    def pop(self, key: str, default=None):
        with self.store.unit():
            o = self.store._get(self.kind, key)
            if o is not None:
                self.store._delete(self.kind, key)
        return default if o is None else o

    def setdefault(self, key: str, obj):
        o = self.get(key)
        if o is None:
            self[key] = obj
            return obj
        return o

    def values(self) -> list:
        return self.where()

    def items(self) -> list:
        return [(k, self.get(k)) for k in self.keys()]

    def keys(self) -> list:
        with self.store.unit():
            return self.store._keys(self.kind)

    def __iter__(self):
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    # queries -----------------------------------------------------------------------------------------------
    def where(self, *, scope: str | None = None, tag: str | None = None, status: tuple | None = None) -> list:
        with self.store.unit():
            return self.store._where(self.kind, scope, tag, status)

    def scope_of(self, key: str) -> str | None:
        """The object's lock scope, read without a lock (scopes never change after creation)."""
        row = self.store.conn.execute("SELECT scope FROM state_items WHERE kind = %s AND key = %s", (self.kind.name, key)).fetchone()
        return None if row is None else row[0]


class PgStore:
    durable = True

    def __init__(self, conn) -> None:
        """conn: a psycopg connection in autocommit mode, as role state_writer. Not shared across threads unguarded."""
        self.conn = conn
        self._kinds: dict[str, Kind] = {}
        self._live: weakref.WeakValueDictionary = weakref.WeakValueDictionary()  # (kind, key) -> object handed out
        self._u: _Unit | None = None
        self._depth = 0
        self._tx = None

    def table(self, kind: Kind) -> PgTable:
        self._kinds[kind.name] = kind
        return PgTable(self, kind)

    # units ----------------------------------------------------------------------------------------------
    @contextmanager
    def unit(self, scope: str | None = None) -> Iterator[None]:
        outer = self._depth == 0
        if outer:
            self._u = _Unit()
            self._tx = self.conn.transaction()
            self._tx.__enter__()
        self._depth += 1
        try:
            if scope:
                self.lock(scope)
            yield
            if outer:
                self._flush()
        except BaseException as e:
            self._depth -= 1
            if outer:
                try:
                    self._tx.__exit__(type(e), e, e.__traceback__)
                finally:
                    self._release()
            raise
        self._depth -= 1
        if outer:
            try:
                self._tx.__exit__(None, None, None)
            finally:
                self._release()

    def _release(self) -> None:
        u, self._u, self._tx = self._u, None, None
        for scope in sorted(u.locks if u else ()):
            self.conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 7))", ("state:" + scope,))

    def lock(self, scope: str) -> None:
        """Hold the payer's advisory lock (session-level) until the unit ends, across any checkpoints."""
        if self._u is None or not scope or scope in self._u.locks:
            return
        self.conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 7))", ("state:" + scope,))
        self._u.locks.add(scope)

    def checkpoint(self) -> None:
        """Commit what this unit has done so far and carry on, still holding its locks (before an irreversible step)."""
        if self._u is None:
            return
        self._flush()
        self._tx.__exit__(None, None, None)
        self._tx = self.conn.transaction()
        self._tx.__enter__()

    # object access (inside a unit) -------------------------------------------------------------------------
    def _decode(self, kind: Kind, data):
        return kind.load(data)

    def _get(self, kind: Kind, key: str):
        ck = (kind.name, key)
        u = self._u
        if ck in u.cache:
            return u.cache[ck]
        row = self.conn.execute("SELECT version, data FROM state_items WHERE kind = %s AND key = %s", ck).fetchone()
        if row is None:
            u.cache[ck] = None
            return None
        version, data = row
        obj = self._adopt(ck, kind.load(data))
        u.cache[ck] = obj
        u.loaded[ck] = (version, _canon(data))
        return obj

    def _put(self, kind: Kind, key: str, obj) -> None:
        ck = (kind.name, key)
        self._u.deleted.discard(ck)
        self._get(kind, key)  # learn the stored version, if any
        self._u.cache[ck] = obj
        self._adopt(ck, obj, replace=True)

    def _adopt(self, ck, fresh, *, replace: bool = False):
        """Return the object callers already hold for this row, refreshed with fresh's state; else remember fresh."""
        live = None if replace else self._live.get(ck)
        if live is not None and type(live) is type(fresh) and hasattr(live, "__dict__"):
            live.__dict__.update(fresh.__dict__)
            return live
        try:
            self._live[ck] = fresh
        except TypeError:  # dicts cannot be weakly referenced: they are working copies only
            pass
        return fresh

    def _delete(self, kind: Kind, key: str) -> None:
        ck = (kind.name, key)
        self._u.cache[ck] = None
        self._u.deleted.add(ck)

    def _keys(self, kind: Kind) -> list:
        rows = self.conn.execute("SELECT key FROM state_items WHERE kind = %s ORDER BY key", (kind.name,)).fetchall()
        keys = {r[0] for r in rows}
        for (k, key), o in self._u.cache.items():
            if k == kind.name:
                (keys.add if o is not None else keys.discard)(key)
        return sorted(keys)

    def _where(self, kind: Kind, scope, tag, status) -> list:
        sql, args = "SELECT key FROM state_items WHERE kind = %s", [kind.name]
        if scope is not None:
            sql, args = sql + " AND scope = %s", args + [scope]
        if tag is not None:
            sql, args = sql + " AND tag = %s", args + [tag]
        if status is not None:
            sql, args = sql + " AND status = ANY(%s)", args + [list(status)]
        keys = [r[0] for r in self.conn.execute(sql + " ORDER BY key", args).fetchall()]
        out, seen = [], set()
        for key in keys:
            o = self._get(kind, key)
            seen.add(key)
            if o is not None and _matches(kind, o, scope, tag, status):
                out.append(o)
        for (k, key), o in list(self._u.cache.items()):  # objects created or changed in this unit
            if k == kind.name and key not in seen and o is not None and _matches(kind, o, scope, tag, status):
                out.append(o)
        return out

    def _flush(self) -> None:
        u = self._u
        for (kname, key), obj in list(u.cache.items()):
            ck = (kname, key)
            before = u.loaded.get(ck)
            if obj is None:
                if ck in u.deleted and before is not None:
                    n = self.conn.execute("DELETE FROM state_items WHERE kind = %s AND key = %s AND version = %s", (kname, key, before[0])).rowcount
                    if n != 1:
                        raise StateConflict(f"{kname} {key} changed before it could be deleted")
                continue
            kind = self._kind(kname, obj)
            data = kind.dump(obj)
            snap = _canon(data)
            fields = (kind.scope(obj) or "", kind.tag(obj) or "", kind.status(obj) or "", kind.expires(obj))
            if before is None:
                try:
                    with self.conn.transaction():
                        self.conn.execute(
                            "INSERT INTO state_items (kind, key, scope, tag, status, expires_at, data) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (kname, key, *fields, Jsonb(data)))
                except Exception as e:
                    if "duplicate key" in str(e):
                        raise StateConflict(f"{kname} {key} was created by another process") from None
                    raise
            elif snap != before[1]:
                n = self.conn.execute(
                    "UPDATE state_items SET scope = %s, tag = %s, status = %s, expires_at = %s, data = %s, version = version + 1, updated_at = now()"
                    " WHERE kind = %s AND key = %s AND version = %s",
                    (*fields, Jsonb(data), kname, key, before[0])).rowcount
                if n != 1:
                    raise StateConflict(f"{kname} {key} was changed by another process")
            else:
                continue
            u.loaded[ck] = (before[0] + 1 if before else 1, snap)
        u.deleted.clear()

    def _kind(self, name: str, obj) -> Kind:
        return self._kinds[name]

    def purge(self, now: datetime) -> int:
        """Delete objects past their expiry (used challenges, codes, tokens, old nonces). Run from a scheduler."""
        return self.conn.execute("DELETE FROM state_items WHERE expires_at IS NOT NULL AND expires_at < %s", (now,)).rowcount


def _matches(kind: Kind, o, scope, tag, status) -> bool:
    return (scope is None or kind.scope(o) == scope) and (tag is None or kind.tag(o) == tag) and (status is None or kind.status(o) in status)


def _canon(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))
