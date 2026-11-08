# Corrections log

Every repeated review comment that became structure. Newest first. Kept by `dv:correct`.

| Date | Comment | Layer | Rule | Links |
| --- | --- | --- | --- | --- |
| 2026-11-08 | "Don't use casex" | Lint | `rules.yaml` no-casex (example block) | formal plugin |
| 2026-11-08 | "No # delays in RTL" | Lint | `rules.yaml` no-delay-in-rtl (example block) | formal plugin |
| 2026-11-08 | "full must mean DEPTH entries, not almost-full" | SVA | `a_full_iff_depth`, caught `FIFO_BUG_FULL_EARLY` in formal | formal plugin |
| 2026-11-08 | "This assertion can pass vacuously" | Hook | `no_weaken.py` blocks cover removal and unlisted assumes; `formal check` requires a reached witness | formal plugin |
| 2026-11-08 | "Don't relax the assertion to make the test pass" | Hook | `no_weaken.py` blocks assert removal, `disable iff`, bin exclusions, waivers | initial |
| 2026-11-08 | "Where's the evidence this was actually simulated?" | Hook | `evidence_gate.py` refuses to stop without `evidence/<sha>.json` | initial |
| 2026-11-08 | "Port changes need a human" | Hook + CLI | `door_check.py`, `control-eda door classify` | initial |
| 2026-11-08 | "No `casex`, no `#` delays in RTL, no hierarchical refs from sequences" | Hook | `lint_on_save.py` | initial |
