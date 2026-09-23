# Evidence and source map

## Code provenance

The Python code under `src/ironman/` and the focused tests under `tests/mission/` were selected from the local Jervis implementation on 2026-09-23. The public example and test under `examples/` and `tests/test_public_cycle.py` were added for this release. The original worktree was not modified by this extraction.

Two portability differences from the local implementation are limited to:

1. `learning/resources.py` now loads the existing `triage.py` from the package, rather than a sibling directory outside a clean checkout.
2. Browser launch in `mission/toolkit.py`, `mission/browser_actions.py` and `mission/compound.py` uses Playwright's installed Chromium instead of a local Edge channel.

## Claim to check

| Claim | Public readback | Ceiling |
|---|---|---|
| The study ran full practice and variant pages. | `result.json` → `study.practice_checks`; generated HTML and screenshots | Browser behavior and render checks, not visual preference. |
| A tentative Designer candidate persisted. | SQLite Registry object `domain:designer:experimental`, `result.json` → `study.decision` | Experimental candidate, not accepted global skill. |
| New tasks selected and rejected it. | `result.json` → `new_task_selected` / `new_task_rejected`; worker response receipts | Fixed synthetic model replay and actual applicability path, not fresh model judgment. |
| A WorkOrder produced actual files. | Generated project `work/page/1-1/index.html`, `work/inspect/1-1/observation.json` | Local artifact and browser observation, not user outcome. |
| Old results are guarded by versions. | `src/ironman/mission/runtime.py` `Mission.commit`; focused original tests | Tested mechanism; no public crash-recovery replay in this batch. |
| Scope is checked before memory body retrieval. | `src/ironman/mission/scopes.py` and `tests/mission/test_scopes.py` | Focused scope test, not proof that every route in the original private system is isolated. |

The continuous test command runs 15 checks: 14 selected original Jervis tests and one public end-to-end test with real headless browser observations. The integration test starts with an empty data directory and verifies files and uncertainty labels after completion.

No private database, original Designer source captures, external reference images, market data, provider credentials, worker transcripts, user account data or unreleased evaluation windows are included.
