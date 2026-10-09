# Jurisdictions (layer 3)

Country-specific law, rails and schemes that the infrastructure (`../specs/`) reads as versioned mapping data. The infrastructure never names a country, rail or regulator; everything of that kind lives here. Each deployment chooses which of these it uses.

| File | Jurisdiction | Status |
|---|---|---|
| `india.md` | India (primary) | Notes; machine-readable mapping to be built (`mappings/in/`) |
| `uk.md` | United Kingdom | Reference notes; mapping `status: reference` |
| `../research/18_silence_and_approval_rules.md` | India | Rules on explicit approval and silence |
| `../research/21_npci_uap_deep_dive.md` | India | NPCI UAP, Reserve Pay, UPI Circle |

Each jurisdiction mapping must supply: the **authentication-strength scale**, **rail capability profiles** (`../specs/04_rails.md` §1), **silent-approval limits**, **consent clauses**, **dispute routes**, **data-protection classes**, `evidence.retention`, and how national schemes map to wheel constructs.
