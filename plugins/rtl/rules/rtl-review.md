---
description: RTL review rules for clock domain crossings, resets and X-propagation. Always on in projects that use the rtl plugin.
alwaysApply: true
trigger: always_on
---

# rtl review

1. A change that touches clocks, resets, synchronizers, `case` statements or
   cross-module signals gets `rtl:review` before it is called ready.
2. Reviewers work independently: each writes its own `reviews/<reviewer>.json`
   before seeing anyone else's. Merge with `control-eda review merge`.
3. Disagreements (`single`, `contested`) and scan findings no reviewer raised go to
   a human. Never settle them by picking the reviewer you agree with.
4. If every reviewer ran on the same model, say so: the merge reports
   `single_model`, and agreement between copies of one model is not independent.
5. `control-eda review scan` is a heuristic. A clean scan is not a CDC sign-off;
   a site CDC tool, when configured under `[review.tools]`, is.
