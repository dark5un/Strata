# ADR-0001: Fork boundary — additive cut points only

Date: 2026-10-09
Status: accepted

## Context
strata-harness is a fork of Strata (upstream https://github.com/Niko1221/Strata)
intended to be contributed back. Upstream moves weekly (0.1.40.x). Every line
we change inside existing upstream files is a future merge conflict and a
review burden for upstream maintainers.

## Decision
All harness code lives in new files:
- `serve/harness/` (Python package, stdlib-only)
- `serve/web/harness.js` (the tab, vanilla JS)
- `docs/harness/` (ADRs, user guide)
- `ruff.toml`, `pyrightconfig.json`, `eslint.config.mjs`, `tools/test_harness.sh`

Upstream files are touched only at these additive points:
1. `serve/server.py`: one import + one `routes.py` mount call in the handler's
   do_GET/_post dispatch tails (the route table's 404 fallthrough), plus
   Service gains a `harness` attribute set at startup when enabled.
2. `serve/web/index.html`: one tab button + one view section + one script tag.
3. `serve/web/app.js`: extend showTab()'s tab list (3-element array).
Harness is gated by runconfig key `harness` (default off) so upstream review
sees a feature flag, not a rewrite.

## Consequences
- Rebase onto upstream/main is cheap and mechanical.
- A PR series (ADR-0001..0005 + M0-M2 slices) is plausible for contribution.
- We cannot restructure upstream code for convenience; if a cut point grows
  beyond ~30 lines per file, revisit this ADR (supersede, don't edit).
