"""Authority types (01, 19 §3): windows, rules and their 14 dimensions, spokes, wheels, rails, parameters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .money import Money
from .timefmt import format_ts

# --- windows -----------------------------------------------------------------

WINDOW_KINDS = ("calendar_month", "calendar_week", "calendar_day", "rolling")


@dataclass(frozen=True)
class Window:
    kind: str
    tz: str
    length_s: int | None = None  # rolling only

    def __post_init__(self) -> None:
        if self.kind not in WINDOW_KINDS:
            raise ValueError(f"unknown window kind {self.kind!r}")
        if (self.kind == "rolling") != (self.length_s is not None):
            raise ValueError("length_s is required for rolling windows and only for them")
        if self.length_s is not None and (type(self.length_s) is not int or self.length_s <= 0):
            raise ValueError("length_s must be a positive int")
        ZoneInfo(self.tz)  # raises if unknown

    def window_id(self, at: datetime) -> str:
        """The window instance `at` falls in (19 §1.2)."""
        if self.kind == "rolling":
            return "rolling:" + format_ts(at - timedelta(seconds=self.length_s))
        local = at.astimezone(ZoneInfo(self.tz))
        if self.kind == "calendar_month":
            return f"{local.year:04d}-{local.month:02d}"
        if self.kind == "calendar_day":
            return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
        y, w, _ = local.isocalendar()
        return f"{y:04d}-W{w:02d}"

    def contains(self, t: datetime, at: datetime) -> bool:
        """Whether something that happened at `t` counts in the window instance `at` falls in. A calendar window
        compares instances; a rolling window is the `length_s` seconds up to and including `at`."""
        if self.kind == "rolling":
            return at - timedelta(seconds=self.length_s) < t <= at
        return self.window_id(t) == self.window_id(at)


@dataclass(frozen=True)
class Limit:
    limit: Money
    window: Window


# --- rules -------------------------------------------------------------------

AUTHORS = ("operator", "payer", "system", "jurisdiction")
ASSURANCE_RANK = {"session_bound": 1, "key_bound": 2}
AUTH_CLASS_RANK = {"none": 0, "human_at_setup": 1, "human_per_payment": 2}
ATTESTATION_RANK = {"injected": 0, "capturer_signed": 1, "payer_presented": 2, "source_signed": 3}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EXTRA_KINDS = ("tip", "discount", "merchant_note", "packaging", "other")

# allowed constraint keys per dimension, with a validator for each value
_STR_LIST = lambda v: isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v)  # noqa: E731
_POS_INT = lambda v: type(v) is int and v >= 1  # noqa: E731
_NONNEG_INT = lambda v: type(v) is int and v >= 0  # noqa: E731


def _time_spans(v) -> bool:
    if not isinstance(v, (list, tuple)):
        return False
    for span in v:
        if not isinstance(span, dict) or set(span) != {"days", "from", "to", "tz"}:
            return False
        if not span["days"] or any(d not in DAYS for d in span["days"]):
            return False
        for k in ("from", "to"):
            hh, _, mm = span[k].partition(":")
            if not (len(span[k]) == 5 and hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
                return False
        ZoneInfo(span["tz"])
    return True


def _aggregate(v) -> bool:
    return isinstance(v, dict) and set(v) == {"limit", "window"} and isinstance(v["limit"], Money) and isinstance(v["window"], Window)


def _extras(v) -> bool:
    return isinstance(v, (list, tuple)) and all(
        isinstance(e, dict) and "kind" in e and e["kind"] in EXTRA_KINDS and set(e) <= {"kind", "max"}
        and ("max" not in e or isinstance(e["max"], Money))
        for e in v
    )


CONSTRAINTS = {
    "caller": {"any_of_callers": _STR_LIST, "min_assurance": lambda v: v in ASSURANCE_RANK},
    "merchant": {"allow": _STR_LIST, "block": _STR_LIST, "allow_categories": _STR_LIST, "block_categories": _STR_LIST},
    "items": {
        "allow_items": _STR_LIST, "block_items": _STR_LIST, "allow_categories": _STR_LIST,
        "block_categories": _STR_LIST, "max_quantity_per_line": _POS_INT, "max_lines": _POS_INT,
    },
    "substitution": {"mode": lambda v: v in ("none", "same_item_variant", "same_category"), "max_price_increase": _NONNEG_INT},
    "item_attributes": {
        "block_terms": _STR_LIST, "require_terms": _STR_LIST, "unknown": lambda v: v in ("block", "allow"),
        "min_attestation": lambda v: v in ATTESTATION_RANK,
    },
    "amount": {"per_transaction_max": lambda v: isinstance(v, Money), "aggregate": _aggregate},
    "velocity": {"max_count": _POS_INT, "window": lambda v: isinstance(v, Window)},
    "payment_method": {
        "allow_rails": _STR_LIST, "allow_authentication": lambda v: _STR_LIST(v) and all(x in AUTH_CLASS_RANK for x in v),
        "require_amount_binding": lambda v: v == "bound_at_rail",
    },
    "time": {"allow": _time_spans, "exclude": _time_spans},
    "approval_mode": {"at_most": lambda v: v in ("explicit_tap", "silent_with_notice")},
    "destination": {"allow": _STR_LIST, "quarantine_new": _POS_INT},
    "conditions": {"when": lambda v: isinstance(v, dict), "effect": lambda v: v in ("block", "force_explicit_tap", "clarify")},
    "extras": {"allow": _extras},
    "currency_geography": {"allow_currencies": _STR_LIST, "allow_jurisdictions": _STR_LIST},
}
REQUIRED = {
    "substitution": {"mode"}, "velocity": {"max_count", "window"}, "approval_mode": {"at_most"},
    "conditions": {"when", "effect"}, "extras": {"allow"},
}
DIMENSIONS = tuple(CONSTRAINTS)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    dimension: str
    constraint: dict
    author: str
    author_ref: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if self.dimension not in CONSTRAINTS:
            raise ValueError(f"unknown dimension {self.dimension!r}")
        if self.author not in AUTHORS:
            raise ValueError(f"unknown author {self.author!r}")
        allowed = CONSTRAINTS[self.dimension]
        extra = set(self.constraint) - set(allowed)
        if extra:
            raise ValueError(f"{self.dimension}: unknown constraint keys {sorted(extra)}")
        missing = REQUIRED.get(self.dimension, set()) - set(self.constraint)
        if missing:
            raise ValueError(f"{self.dimension}: missing {sorted(missing)}")
        if not self.constraint:
            raise ValueError(f"{self.dimension}: empty constraint")
        for k, v in self.constraint.items():
            if not allowed[k](v):
                raise ValueError(f"{self.dimension}.{k}: invalid value {v!r}")
        if self.dimension == "conditions":
            from .facts import validate_predicate

            validate_predicate(self.constraint["when"])


# --- spokes and wheels ---------------------------------------------------------

@dataclass(frozen=True)
class CartLineTemplate:
    item_id: str
    quantity: int
    variant_id: str | None = None


@dataclass(frozen=True)
class StandingInstruction:
    merchant_ref: str
    adapter_id: str
    lines: tuple[CartLineTemplate, ...]
    destination_ref: str
    rail_id: str
    price_tolerance_bp: int
    reference_total: Money


@dataclass(frozen=True)
class Spoke:
    spoke_id: str
    wheel_id: str
    approval_mode: str
    valid_from: datetime
    valid_until: datetime
    parent_id: str | None = None
    callers: tuple[str, ...] | None = None
    instruction: StandingInstruction | None = None
    rules: tuple[Rule, ...] = ()
    caps: tuple[Limit, ...] = ()
    status: str = "active"
    version: int = 1

    def __post_init__(self) -> None:
        if self.approval_mode not in ("explicit_tap", "silent_with_notice"):
            raise ValueError("unknown approval mode")
        if self.approval_mode == "silent_with_notice" and self.instruction is None:
            raise ValueError("silent_with_notice is valid only with a standing instruction (01 §2)")
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if self.status not in ("active", "suspended", "revoked", "expired"):
            raise ValueError("unknown spoke status")
        if len({c.limit.currency for c in self.caps}) != len(self.caps):
            raise ValueError("at most one cap per currency")


@dataclass(frozen=True)
class Wheel:
    wheel_id: str
    payer_ref: str
    home_jurisdiction: str
    budgets: tuple[Limit, ...]
    rules: tuple[Rule, ...] = ()
    status: str = "active"
    version: int = 1

    def __post_init__(self) -> None:
        if self.status not in ("active", "suspended", "revoked"):
            raise ValueError("unknown wheel status")
        if len({b.limit.currency for b in self.budgets}) != len(self.budgets):
            raise ValueError("at most one budget per currency")


# --- rails, callers, parameters --------------------------------------------------

RAIL_STATUSES = ("live", "excluded", "ready_no_merchant", "placeholder", "reference")


@dataclass(frozen=True)
class RailProfile:
    """A rail capability profile (04 §1, 19 §7.1). Jurisdiction mappings supply these as data."""

    rail_id: str
    authentication: str
    amount_binding: str
    approval_modes: tuple[str, ...]
    status: str = "live"
    placement: str = "immediate"
    reversibility: str = "none"
    dispute_route: str = ""
    per_transaction_max: Money | None = None
    period_limit: Limit | None = None
    max_duration_s: int | None = None
    definitive_signals: dict = field(default_factory=dict)   # {success|failure|unknown: [status]}
    retry_policy: dict = field(default_factory=dict)         # {safe_errors, max_retries, window_s}

    def __post_init__(self) -> None:
        if self.authentication not in AUTH_CLASS_RANK:
            raise ValueError("unknown authentication class")
        if self.amount_binding not in ("bound_at_rail", "not_bound"):
            raise ValueError("unknown amount binding")
        if self.status not in RAIL_STATUSES:
            raise ValueError(f"unknown rail status {self.status!r}")
        if self.placement not in ("pending_then_confirm", "immediate"):
            raise ValueError("unknown placement")
        if self.reversibility not in ("refund_only", "chargeback", "none"):
            raise ValueError("unknown reversibility")
        if not self.approval_modes or any(m not in ("explicit_tap", "silent_with_notice") for m in self.approval_modes):
            raise ValueError("approval_modes must list explicit_tap and/or silent_with_notice")
        if ":" not in self.rail_id:
            raise ValueError("rail_id is namespaced by jurisdiction: <jurisdiction>:<rail>")


@dataclass(frozen=True)
class Caller:
    caller_id: str
    assurance: str  # key_bound | session_bound (claimed callers never reach evaluate)
    status: str = "active"

    def __post_init__(self) -> None:
        if self.assurance not in ASSURANCE_RANK:
            raise ValueError("callers reaching evaluate() are key_bound or session_bound")


@dataclass(frozen=True)
class Params:
    """The policy parameters evaluate() reads (policy_parameters.md). No defaults for policy values."""

    clarify_threshold: int  # PerMille
    duplicate_window_s: int
    verified_sources: frozenset[str]
    max_depth: int
    silent_max_amount: Money | None  # None: silent execution not permitted
    silent_preconditions: tuple[dict, ...] = ()
    caller_min_assurance: dict = field(default_factory=dict)  # {approval_mode: assurance}

    def __post_init__(self) -> None:
        if type(self.clarify_threshold) is not int or not 0 <= self.clarify_threshold <= 1000:
            raise ValueError("clarify_threshold is a PerMille")
        from .facts import validate_predicate

        for p in self.silent_preconditions:
            validate_predicate(p)
