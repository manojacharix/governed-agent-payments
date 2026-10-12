"""Readable views of an evidence bundle (06 §1): a timeline in plain words, as text or as an HTML page.

A rendering is never the evidence. Every view says so and names the bundle it was rendered from; anyone who
needs proof runs the open verifier on the bundle itself. Rendering is pure and deterministic: the same
bundle and verifier report always give the same bytes. Everything taken from the bundle is HTML-escaped.
"""

from __future__ import annotations

from html import escape

ACTOR = {"proposal": "agent", "determination": "evaluator", "surface_assertion": "payer's device", "resolution": "host",
         "posting": "ledger", "execution_attempt": "merchant", "finding": "reconciliation", "sweep": "reconciliation"}
CHECKS = (("bundle_signature", "Bundle signed by the host"), ("key_log", "Key log anchored to the pinned host key"),
          ("signatures", "Every record's signature"), ("chain", "Records sit where the chain says"), ("keys", "Keys valid when used"),
          ("replay", "Decision replays from its inputs"), ("presented_hash", "Payer approved exactly what was shown"))


def _money(m: dict) -> str:
    return f"{m['amount_minor'] / 100:,.2f} {m['currency']}"


def _when(rec: dict) -> str:
    for f in ("received_at", "evaluated_at", "occurred_at", "posted_at", "started_at", "opened_at", "ended_at", "at"):
        if f in rec:
            return rec[f]
    return ""


def describe(rec: dict) -> str:
    t = rec.get("record_type")
    if t == "proposal":
        return f"Proposed paying {_money(rec['total'])} to {rec['merchant_ref']} under spoke {rec['spoke_id']} (intent {rec['intent_id']})."
    if t == "determination":
        stage = "At proposal" if rec.get("stage") == "proposal" else "At approval, with the cart fetched again"
        s = f"{stage}: {rec['outcome'].replace('_', ' ')}"
        if rec["outcome"] == "allow":
            s += f", {rec['approval_mode'].replace('_', ' ')}" + (f" on {rec['rail_id']}" if rec.get("rail_id") else "")
        fails = rec.get("failing_clauses", [])
        if fails:
            s += ". Failing: " + "; ".join(f"{c['rule_id']} ({c['dimension']}, written by the {c['author']}"
                                           + (", the payer may override it" if c["overridable"] else "") + ")" for c in fails)
        if rec.get("clarify_fields"):
            s += ". Needs clarifying: " + ", ".join(rec["clarify_fields"])
        return s + "."
    if t == "surface_assertion":
        verb = {"authorise": "approved", "decline": "declined", "override": "overrode the payer's own rule for this payment and approved"}
        return f"The payer {verb.get(rec.get('action'), rec.get('action'))} on device {rec.get('surface_key_id')} (strength: {rec.get('auth_strength_achieved', 'n/a')})."
    if t == "resolution":
        s = {"approved": "Resolved: approved by the payer", "approved_with_override": "Resolved: approved with an override of "
             + ", ".join(rec.get("overridden_clauses", [])), "proceeded_on_silence": "Resolved: ran as a standing instruction after its notice",
             "declined": f"Resolved: declined by the {rec.get('by')}", "expired": "Resolved: expired without an answer",
             "superseded": "Resolved: superseded; the cart changed and was shown to the payer again"}.get(rec["state"], rec["state"])
        return s + "."
    if t == "posting":
        what = {"hold": "Held", "capture": "Captured", "release": "Released", "overcapture": "Charged beyond the hold",
                "refund": "Refunded"}[rec["kind"]]
        src = f" on evidence from {rec['signal_ref'].split(':', 1)[0]}" if rec.get("signal_ref") else ""
        return f"{what} {_money(rec['amount'])} on {rec['account_set']['owner']} ({rec['account_set']['window_id']}){src}."
    if t == "execution_attempt":
        s = f"Attempt {rec['attempt_number']} on {rec['rail_id']} at {rec['adapter_id']}: {rec['response_status']} -> {rec['outcome']}"
        return s + (f", order {rec['merchant_order_ref']}" if rec.get("merchant_order_ref") else "") + "."
    if t == "finding":
        state = "opened" if rec["status"] == "open" else f"closed by {rec.get('closed_by', 'evidence')}"
        extra = (f" ({', '.join(f'{k}: {v} minor units' if isinstance(v, int) and not isinstance(v, bool) else f'{k}: {v}' for k, v in rec['detail'].items())})"
                 if isinstance(rec.get("detail"), dict) else "")
        return f"Finding {rec['kind'].replace('_', ' ')} {state}{extra}."
    if t == "sweep":
        return f"Reconciliation sweep ({rec['trigger']})."
    return t or "record"


def timeline(bundle: dict) -> list[dict]:
    return [{"seq": r["chain"]["seq"], "when": _when(r), "actor": ACTOR.get(r.get("record_type"), "host"), "what": describe(r)}
            for r in sorted(bundle.get("records", []), key=lambda r: r["chain"]["seq"])]


def render_text(bundle: dict, report: dict) -> str:
    lines = [f"EVIDENCE (a rendering of bundle {bundle['bundle_id']}; the bundle is the evidence)",
             f"Proposal {bundle['proposal_id']} on wheel {bundle['wheel_id']}, disclosed to the {bundle['requester_role']} ({bundle['basis']}).", ""]
    lines.append("Verifier")
    for key, label in CHECKS:
        lines.append(f"  {label:<45} {report.get(key)}")
    lines.append(f"  {'All checks pass':<45} {report.get('ok')}")
    d = bundle["divergence"]
    lines += ["", f"Where authority and reality diverged: {d['primary'].replace('_', ' ')}"
              + (f" (also: {', '.join(c.replace('_', ' ') for c in d['contributing'])})" if d["contributing"] else ""), "", "Timeline"]
    for e in timeline(bundle):
        lines.append(f"  {e['when']:<25} {e['actor']:<16} {e['what']}")
    if report.get("attestations"):
        lines += ["", "Facts the decision used, and who vouches for them"]
        seen = set()
        for a in report["attestations"]:
            k = (a["fact"], a["class"], a["verified"])
            if k not in seen:
                seen.add(k)
                lines.append(f"  {a['fact']:<40} {a['class']:<16} {'verified source' if a['verified'] else 'can only add caution'}")
    if bundle.get("withheld"):
        lines += ["", f"Withheld: {bundle['withheld']}"]
    return "\n".join(lines) + "\n"


CSS = """
:root{--bg:#f7f8f6;--fg:#1b211d;--muted:#5d675f;--line:#d8ddd6;--ok:#2f7a4b;--bad:#b3362c;--card:#fff;--accent:#2d5f7a}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#111513;--fg:#e4e8e3;--muted:#9aa49b;--line:#2b332d;--ok:#6cc08b;--bad:#ee7d72;--card:#181d1a;--accent:#8cc1dd}}
:root[data-theme="dark"]{--bg:#111513;--fg:#e4e8e3;--muted:#9aa49b;--line:#2b332d;--ok:#6cc08b;--bad:#ee7d72;--card:#181d1a;--accent:#8cc1dd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px 16px}
main{max-width:920px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
h1{font-size:1.5rem;margin:0}h2{font-size:1rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 8px}
.note{border-left:3px solid var(--accent);padding:8px 12px;background:var(--card);color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.85rem}.mono{font-family:ui-monospace,Menlo,monospace;font-size:.82rem;white-space:nowrap}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.tl td:first-child{width:12rem}.wrap{overflow-x:auto}
"""


def render_html(bundle: dict, report: dict) -> str:
    e = escape
    d = bundle["divergence"]
    checks = "".join(f"<tr><td>{e(label)}</td><td class='{'ok' if report.get(k) in ('ok', 'reproduced', 'matches') else ('bad' if report.get(k) in ('failed', 'broken', 'differs', 'signed_after_compromise') else '')}'>{e(str(report.get(k)))}</td></tr>"
                     for k, label in CHECKS)
    rows = "".join(f"<tr><td class='mono'>{e(x['when'])}</td><td>{e(x['actor'])}</td><td>{e(x['what'])}</td></tr>" for x in timeline(bundle))
    facts = ""
    if report.get("attestations"):
        seen, items = set(), []
        for a in report["attestations"]:
            k = (a["fact"], a["class"], a["verified"])
            if k not in seen:
                seen.add(k)
                items.append(f"<tr><td class='mono'>{e(a['fact'])}</td><td>{e(a['class'])}</td><td>{'verified source' if a['verified'] else 'can only add caution'}</td></tr>")
        facts = f"<section class='card'><h2>Facts and who vouches for them</h2><div class='wrap'><table><tr><th>Fact</th><th>Attestation</th><th>Weight</th></tr>{''.join(items)}</table></div></section>"
    contributing = f" <span class='muted'>(also: {e(', '.join(c.replace('_', ' ') for c in d['contributing']))})</span>" if d["contributing"] else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evidence {e(bundle['proposal_id'])}</title><style>{CSS}</style></head><body><main>
<h1>Evidence for proposal <span class="mono">{e(bundle['proposal_id'])}</span></h1>
<p class="note">This page is a rendering of bundle <span class="mono">{e(bundle['bundle_id'])}</span>. The bundle is the evidence: verify it with the open verifier, not this page. Disclosed to the {e(bundle['requester_role'])} ({e(bundle['basis'])}).</p>
<section class="card"><h2>Verifier</h2><table>{checks}<tr><td><strong>All checks pass</strong></td><td class="{'ok' if report.get('ok') else 'bad'}">{e(str(report.get('ok')))}</td></tr></table></section>
<section class="card"><h2>Where authority and reality diverged</h2><p><strong>{e(d['primary'].replace('_', ' '))}</strong>{contributing}</p></section>
<section class="card tl"><h2>Timeline</h2><div class="wrap"><table><tr><th>When (UTC)</th><th>Who</th><th>What</th></tr>{rows}</table></div></section>
{facts}
</main></body></html>
"""
