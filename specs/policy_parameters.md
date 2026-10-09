# Policy Parameters

Every value the infrastructure needs but does not choose. Each deployment sets them: **Tier 0** (operator, may be constrained by the jurisdiction mapping) or **Tier 1** (payer, may only narrow Tier 0). **No defaults are baked into the infrastructure**; a deployment that leaves a required parameter unset fails to start.

Example values will ship as Tier 0 configs in `tier0/examples/` (a personal operator and a B2B procurement operator).

| Parameter | Type | Set at | Used by | Meaning |
|---|---|---|---|---|
| `auth.strength.create` | authentication strength | Tier 0 | `01` | Payer authentication required to create a spoke |
| `auth.strength.widen` | authentication strength | Tier 0 | `01` | Required to widen or renew |
| `auth.strength.resume` | authentication strength | Tier 0 | `01` | Required to resume after suspension |
| `auth.strength.override` | authentication strength | Tier 0 | `02` | Required for a one-payment override |
| `auth.strength.no_auth_rail` | authentication strength | Tier 0 | `04` | Required at the Trusted Surface before executing on a rail with `authentication = none` |
| `auth.strength.enrol_surface` | authentication strength | Tier 0 | `07` | Required to enrol a Trusted Surface (first or additional) |
| `auth.strength.enrol_caller` | authentication strength | Tier 0 | `07` | Required for the payer to approve a caller's enrolment |
| `auth.strength.recovery` | authentication strength | Tier 0 | `07` | Required to recover a wheel after losing every surface |
| `identity.recovery_delay` | duration | Tier 0 | `07` | Waiting period in which any old surface can cancel a recovery |
| `identity.surface.min_assurance` | protection class | Tier 0 | `07` | Lowest key protection class a surface may enrol with |
| `identity.caller.min_assurance` | assurance per approval mode / dimension | Tier 0 / Tier 1 | `02`, `07` | Lowest caller assurance (`key_bound`, `session_bound`) allowed to cite a spoke |
| `identity.caller.max_validity` | duration | Tier 0 | `07` | Longest a caller enrolment lasts before re-approval |
| `identity.proof_max_age` | duration | Tier 0 | `07` | Maximum age of a proposal's proof before it is refused as a replay |
| `keys.algorithms` | list | Tier 0 | `07` | Signature algorithms accepted in the key log |
| `keys.anchor_interval` | duration | Tier 0 | `07` | How often chain heads are published to witnesses |
| `keys.anchor_witnesses` | list | Tier 0 | `07` | Parties that receive chain heads (payer surfaces always do) |
| `trusted_surfaces` | list of surface types | Tier 0 | `01`, `02` | Which surfaces may present and authorise (e.g. a registered application on the payer's device) |
| `wheel.max_depth` | integer | Tier 0 | `01` | Spoke nesting depth allowed |
| `budget.window` | window definition + time zone | Tier 0 / Tier 1 | `03` | How budget windows are cut (e.g. calendar month) |
| rule values for each of the 14 dimensions | per dimension | Tier 0 / Tier 1 | `01` | Caps, allowlists, time windows, substitution policy, item-attribute terms, destination rules, extras, … |
| `approval.silent.preconditions` | list of fact predicates | Tier 0 / Tier 1 | `01`, `02` | Facts that must all hold for `silent_with_notice` (e.g. notice delivery confirmed, outside excluded time windows); any failing forces `explicit_tap` |
| `approval.silent.notice_duration` | duration | Tier 0 / Tier 1 | `02` | How long a silent proposal waits before proceeding |
| `approval.silent.max_amount` | Money | Tier 0 (and jurisdiction) | `01`, `02` | Ceiling for silent execution |
| `instruction.max_validity` | duration | Tier 0 | `01` | Longest a standing instruction may be valid before renewal |
| `instruction.price_tolerance` | percentage | Tier 1 (per instruction) | `02`, `03` | Drift allowed for a silent repeat; part of what the payer signs |
| `conditional_unlocks` | rules over facts | Tier 0 / Tier 1 | `01` | E.g. a rail or approval mode unlocked when success-rate / dispute / account-age facts hold, and re-locked when they fail |
| `decision.clarify_threshold` | number | Tier 0 | `02` | Agent confidence below which the engine returns `clarify` |
| `decision.duplicate_window` | duration | Tier 0 | `02` | Window for the content-based duplicate guard |
| `facts.verified_sources` | list of sources | Tier 0 | `02` | Which sources may clear a check (all others only add caution) |
| `money.max_pending_age` | duration | Tier 0 | `03` | When a `pending_outcome` hold escalates to reconciliation |
| `recon.sweep_interval` | duration | Tier 0 | `05` | Scheduled reconciliation cadence per merchant adapter |
| `recon.sweep_before_hold` | boolean | Tier 0 | `05` | Whether a sweep runs before every new hold |
| `external.count_spend` | boolean | Tier 1 | `05` | Whether orders placed outside the layer draw down the hub |
| `external.notify` | none / all / unusual | Tier 1 | `05` | Notification behaviour for external orders |
| `external.unusual_predicates` | list of fact predicates | Tier 1 | `05` | What counts as "unusual" for notifications |
| `evidence.retention` | duration | **Jurisdiction mapping** | `06` | How long minimum evidence is held after a deletion request or payment |
| `notifications.channels` | list | Tier 0 | all | Where payer notices are delivered (hooks; delivery is product) |

**Authentication strength** is an ordered scale defined by the jurisdiction mapping (for example: none < possession < possession + knowledge or inherence), so the infrastructure never names specific authentication methods or devices.
