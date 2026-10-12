"""Load jurisdiction mappings and Tier 0 configs (19 §13) into core types.

Everything is closed: unknown keys are errors. A Tier 0 config that leaves a required parameter
unset fails to load (policy_parameters.md: "fails to start").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import yaml

from gap_core.authority import ASSURANCE_RANK, Limit, Params, RailProfile, Rule, Window
from gap_core.facts import validate_predicate
from gap_core.money import Money


class ConfigError(ValueError):
    """A mapping or Tier 0 config is malformed or inconsistent."""


# --- small readers ---------------------------------------------------------------

def _closed(obj, where: str, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(obj, dict):
        raise ConfigError(f"{where}: expected a mapping")
    keys = set(obj)
    if missing := required - keys:
        raise ConfigError(f"{where}: missing {sorted(missing)}")
    if extra := keys - required - set(optional):
        raise ConfigError(f"{where}: unknown keys {sorted(extra)}")
    return obj


def _money(obj, where: str) -> Money:
    _closed(obj, where, {"amount_minor", "currency"})
    try:
        return Money(obj["amount_minor"], obj["currency"])
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: {e}") from None


def _window(obj, where: str) -> Window:
    _closed(obj, where, {"kind", "tz"}, {"length_s"})
    try:
        return Window(obj["kind"], obj["tz"], obj.get("length_s"))
    except Exception as e:  # noqa: BLE001  (zoneinfo raises several types)
        raise ConfigError(f"{where}: {e}") from None


def _int(v, where: str, lo: int = 0) -> int:
    if type(v) is not int or v < lo:
        raise ConfigError(f"{where}: expected an integer ≥ {lo}")
    return v


def _str_list(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{where}: expected a list of strings")
    return v


_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def _rule(obj, where: str, author: str) -> Rule:
    _closed(obj, where, {"rule_id", "dimension", "constraint"}, {"author_ref"})
    c = dict(obj["constraint"]) if isinstance(obj["constraint"], dict) else obj["constraint"]
    d = obj["dimension"]
    try:
        if d == "amount" and isinstance(c, dict):
            if "per_transaction_max" in c:
                c["per_transaction_max"] = _money(c["per_transaction_max"], f"{where}.per_transaction_max")
            if "aggregate" in c:
                agg = _closed(c["aggregate"], f"{where}.aggregate", {"limit", "window"})
                c["aggregate"] = {"limit": _money(agg["limit"], f"{where}.aggregate.limit"), "window": _window(agg["window"], f"{where}.aggregate.window")}
        if d == "velocity" and isinstance(c, dict) and "window" in c:
            c["window"] = _window(c["window"], f"{where}.window")
        if d == "extras" and isinstance(c, dict) and isinstance(c.get("allow"), list):
            c["allow"] = [{**e, "max": _money(e["max"], f"{where}.allow.max")} if isinstance(e, dict) and "max" in e else e for e in c["allow"]]
        return Rule(obj["rule_id"], d, c, author, obj.get("author_ref", ""))
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: {e}") from None


# --- jurisdiction mapping ---------------------------------------------------------------

@dataclass(frozen=True)
class AuthLevel:
    level: str
    requires: tuple  # factor classes, or ("any_of", (...)) entries


@dataclass(frozen=True)
class Mapping:
    jurisdiction: str
    version: str
    status: str
    auth_levels: tuple[AuthLevel, ...]
    rails: dict  # {rail_id: RailProfile}
    silent_max_amount: Money | None
    evidence_retention_s: int | None
    dispute_routes: dict  # {id: description}
    clauses: tuple[Rule, ...]
    scheme_mappings: tuple[dict, ...]
    open_questions: tuple[str, ...]

    @property
    def ref(self) -> str:
        return f"{self.jurisdiction}@{self.version}"

    def level_rank(self, level: str) -> int:
        for i, lvl in enumerate(self.auth_levels):
            if lvl.level == level:
                return i
        raise ConfigError(f"{self.ref}: unknown authentication level {level!r}")


_FACTORS = ("possession", "knowledge", "inherence")


def _rail(obj, where: str, jurisdiction: str, routes: dict) -> RailProfile:
    _closed(obj, where, {"rail_id", "authentication", "placement", "amount_binding", "approval_modes", "status", "reversibility", "dispute_route"},
            {"definitive_signals", "retry_policy", "limits", "note"})
    if not obj["rail_id"].startswith(jurisdiction + ":"):
        raise ConfigError(f"{where}: rail_id must start with '{jurisdiction}:'")
    if obj["dispute_route"] not in routes:
        raise ConfigError(f"{where}: unknown dispute_route {obj['dispute_route']!r}")
    limits = _closed(obj.get("limits", {}), f"{where}.limits", set(), {"per_transaction", "per_period", "max_duration_s"})
    per_tx = _money(limits["per_transaction"], f"{where}.limits.per_transaction") if "per_transaction" in limits else None
    period = None
    if "per_period" in limits:
        pp = _closed(limits["per_period"], f"{where}.limits.per_period", {"limit", "window"})
        period = Limit(_money(pp["limit"], f"{where}.per_period.limit"), _window(pp["window"], f"{where}.per_period.window"))
    signals = _closed(obj.get("definitive_signals", {}), f"{where}.definitive_signals", set(), {"success", "failure", "unknown"})
    retry = _closed(obj.get("retry_policy", {}), f"{where}.retry_policy", set(), {"safe_errors", "max_retries", "window_s"})
    try:
        return RailProfile(
            rail_id=obj["rail_id"], authentication=obj["authentication"], amount_binding=obj["amount_binding"],
            approval_modes=tuple(_str_list(obj["approval_modes"], f"{where}.approval_modes")), status=obj["status"],
            placement=obj["placement"], reversibility=obj["reversibility"], dispute_route=obj["dispute_route"],
            per_transaction_max=per_tx, period_limit=period,
            max_duration_s=_int(limits["max_duration_s"], f"{where}.max_duration_s", 1) if "max_duration_s" in limits else None,
            definitive_signals={k: tuple(_str_list(v, f"{where}.definitive_signals.{k}")) for k, v in signals.items()},
            retry_policy=dict(retry),
        )
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from None


def load_mapping(path: str | Path) -> Mapping:
    doc = yaml.safe_load(Path(path).read_text())
    m = _closed(_closed(doc, str(path), {"mapping"})["mapping"], "mapping",
                {"jurisdiction", "version", "status", "auth_strength_scale", "rails", "silent", "evidence_retention_s",
                 "dispute_routes", "clauses", "scheme_mappings", "open_questions"}, {"notes"})
    j = m["jurisdiction"]
    if not isinstance(j, str) or not re.fullmatch(r"[a-z]{2}", j):
        raise ConfigError("mapping.jurisdiction: a two-letter lowercase code")
    if not isinstance(m["version"], str) or not _SEMVER.fullmatch(m["version"]):
        raise ConfigError("mapping.version: semver x.y.z")
    if m["status"] not in ("interpretation", "reference"):
        raise ConfigError("mapping.status: interpretation or reference")

    levels = []
    for i, lvl in enumerate(m["auth_strength_scale"] or []):
        _closed(lvl, f"auth_strength_scale[{i}]", {"level", "requires"})
        for req in lvl["requires"]:
            ok = req in _FACTORS or (isinstance(req, dict) and set(req) == {"any_of"} and all(x in _FACTORS for x in req["any_of"]))
            if not ok:
                raise ConfigError(f"auth_strength_scale[{i}]: unknown factor {req!r}")
        levels.append(AuthLevel(lvl["level"], tuple(lvl["requires"])))
    if len(levels) < 2 or len({l.level for l in levels}) != len(levels):
        raise ConfigError("auth_strength_scale: at least two distinct levels, weakest first")

    routes = {}
    for i, r in enumerate(m["dispute_routes"] or []):
        _closed(r, f"dispute_routes[{i}]", {"id", "description"})
        routes[r["id"]] = r["description"]

    rails = {}
    for i, r in enumerate(m["rails"] or []):
        rail = _rail(r, f"rails[{i}]", j, routes)
        if rail.rail_id in rails:
            raise ConfigError(f"rails: duplicate {rail.rail_id}")
        rails[rail.rail_id] = rail

    silent = _closed(m["silent"], "silent", {"max_amount"})
    silent_max = None if silent["max_amount"] is None else _money(silent["max_amount"], "silent.max_amount")
    retention = None if m["evidence_retention_s"] is None else _int(m["evidence_retention_s"], "evidence_retention_s", 1)
    if retention is None and m["status"] == "interpretation":
        raise ConfigError("evidence_retention_s: required for a mapping used in deployments (status interpretation)")

    clauses = tuple(_rule(c, f"clauses[{i}]", "jurisdiction") for i, c in enumerate(m["clauses"] or []))
    schemes = []
    for i, s in enumerate(m["scheme_mappings"] or []):
        _closed(s, f"scheme_mappings[{i}]", {"scheme", "construct", "note"})
        if s["construct"] not in ("spoke", "standing_instruction", "ledger_export", "caller_identity", "explicit_tap_spoke"):
            raise ConfigError(f"scheme_mappings[{i}]: unknown construct {s['construct']!r}")
        schemes.append(dict(s))
    return Mapping(j, m["version"], m["status"], tuple(levels), rails, silent_max, retention, routes, clauses,
                   tuple(schemes), tuple(_str_list(m["open_questions"] or [], "open_questions")))


# --- Tier 0 -------------------------------------------------------------------------------

AUTH_PARAMS = (
    "auth.strength.create", "auth.strength.widen", "auth.strength.resume", "auth.strength.override",
    "auth.strength.no_auth_rail", "auth.strength.enrol_surface", "auth.strength.enrol_caller", "auth.strength.recovery",
)
DURATION_PARAMS = (
    "identity.recovery_delay", "identity.caller.max_validity", "identity.proof_max_age", "keys.anchor_interval",
    "approval.silent.notice_duration", "instruction.max_validity", "decision.duplicate_window",
    "money.max_pending_age", "recon.sweep_interval",
)
REQUIRED_TIER0 = set(AUTH_PARAMS) | set(DURATION_PARAMS) | {
    "identity.surface.min_assurance", "identity.caller.min_assurance", "keys.algorithms", "keys.anchor_witnesses",
    "trusted_surfaces", "wheel.max_depth", "budget.window", "approval.silent.preconditions", "approval.silent.max_amount",
    "conditional_unlocks", "decision.clarify_threshold", "facts.verified_sources", "recon.sweep_before_hold",
    "notifications.channels",
}


@dataclass(frozen=True)
class Tier0:
    operator_ref: str
    jurisdiction: str
    version: int
    vocabularies: dict
    params: Params
    rules: tuple[Rule, ...]
    raw: dict = field(repr=False)


def load_tier0(path: str | Path, mapping: Mapping) -> Tier0:
    doc = yaml.safe_load(Path(path).read_text())
    t = _closed(_closed(doc, str(path), {"tier0"})["tier0"], "tier0",
                {"operator_ref", "jurisdiction", "version", "vocabularies", "parameters", "rules"}, {"notes"})
    if t["jurisdiction"] != mapping.jurisdiction:
        raise ConfigError(f"tier0.jurisdiction {t['jurisdiction']!r} does not match mapping {mapping.ref}")
    vocab = _closed(t["vocabularies"], "vocabularies", {"categories", "item_attributes"})
    p = _closed(t["parameters"], "parameters", REQUIRED_TIER0)

    for k in AUTH_PARAMS:
        mapping.level_rank(p[k])  # must be a level on the mapping's scale
    for k in DURATION_PARAMS:
        _int(p[k], f"parameters.{k}")
    if p["identity.surface.min_assurance"] not in ("hardware_attested", "hardware", "software"):
        raise ConfigError("parameters.identity.surface.min_assurance: unknown protection class")
    cma = _closed(p["identity.caller.min_assurance"], "parameters.identity.caller.min_assurance", {"explicit_tap", "silent_with_notice"})
    if any(v not in ASSURANCE_RANK for v in cma.values()):
        raise ConfigError("parameters.identity.caller.min_assurance: key_bound or session_bound")
    if _str_list(p["keys.algorithms"], "parameters.keys.algorithms") != ["ES256"]:
        raise ConfigError("parameters.keys.algorithms: only ES256 is supported in v1")
    _str_list(p["keys.anchor_witnesses"], "parameters.keys.anchor_witnesses")
    _str_list(p["trusted_surfaces"], "parameters.trusted_surfaces")
    _str_list(p["notifications.channels"], "parameters.notifications.channels")
    if type(p["recon.sweep_before_hold"]) is not bool:
        raise ConfigError("parameters.recon.sweep_before_hold: true or false")
    _window(p["budget.window"], "parameters.budget.window")
    for i, pre in enumerate(p["approval.silent.preconditions"] or []):
        try:
            validate_predicate(pre)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"parameters.approval.silent.preconditions[{i}]: {e}") from None
    for i, cu in enumerate(p["conditional_unlocks"] or []):
        _closed(cu, f"parameters.conditional_unlocks[{i}]", {"unlock", "when"})
        validate_predicate(cu["when"])

    # strictest wins: the operator's silent ceiling can never exceed the jurisdiction's (when the mapping sets one)
    op_silent = None if p["approval.silent.max_amount"] is None else _money(p["approval.silent.max_amount"], "parameters.approval.silent.max_amount")
    silent = op_silent
    if op_silent is not None and mapping.silent_max_amount is not None:
        if op_silent.currency != mapping.silent_max_amount.currency:
            raise ConfigError("approval.silent.max_amount: currency differs from the jurisdiction's silent limit")
        silent = min(op_silent, mapping.silent_max_amount)

    params = Params(
        clarify_threshold=_int(p["decision.clarify_threshold"], "parameters.decision.clarify_threshold"),
        duplicate_window_s=p["decision.duplicate_window"],
        verified_sources=frozenset(_str_list(p["facts.verified_sources"], "parameters.facts.verified_sources")),
        max_depth=_int(p["wheel.max_depth"], "parameters.wheel.max_depth", 1),
        silent_max_amount=silent,
        silent_preconditions=tuple(p["approval.silent.preconditions"] or ()),
        caller_min_assurance=dict(cma),
    )
    rules = tuple(_rule(r, f"rules[{i}]", "operator") for i, r in enumerate(t["rules"] or []))
    return Tier0(t["operator_ref"], t["jurisdiction"], _int(t["version"], "version", 1), dict(vocab), params, rules, p)


def evaluation_context(mapping: Mapping, tier0: Tier0) -> dict:
    """The parts of an EvaluationInput that come from configuration: use with dataclasses.replace."""
    if tier0.jurisdiction != mapping.jurisdiction:
        raise ConfigError("tier0 and mapping are for different jurisdictions")
    return {
        "jurisdiction": mapping.jurisdiction,
        "rails": dict(mapping.rails),
        "params": tier0.params,
        "tier0_rules": tier0.rules,
        "jurisdiction_rules": mapping.clauses,
    }
