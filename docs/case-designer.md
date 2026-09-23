# Case: learn a limited design relation, then challenge it

The run uses an entirely fictional workshop, a self-authored reference page and a short self-authored method note. The model's source-selection, comparison, applicability, HTML and semantic responses are fixed synthetic fixtures. The task coordinator, study orchestration, Registry, browser checks and commits use the real Jervis code.

| Stage | Actual public-run artifact | What it establishes |
|---|---|---|
| Reference contact | `reference-observation/desktop.png` | A local image file was captured and passed to the study worker interface. The synthetic response describes its intended observation. |
| Practice and variant | `practice/index.html`, `variant/index.html` and desktop/mobile screenshots | Two complete pages were written and browser checked. They differ in action grouping. |
| Comparison | `comparison.json` | The fixed model response proposes a conditional relation with source references and `AWAITING_HUMAN_EVIDENCE`. |
| Candidate commit | Registry object `domain:designer:experimental` | The real versioned store retained the proposal and active experimental judgment after `Mission.commit`. |
| New tasks | `workshop-page` and `dispatch-table` projects | One reloaded the candidate and selected it; the other reloaded it and rejected it as inapplicable. |
| Actual output | Both `index.html` files and `observation.json` files | Browser screenshots, responsive overflow checks, JavaScript errors and button state changes were read back. |

The local historical apprenticeship had a more complex result: a new archive task rejected the learned judgment, while another task applied it; model preference comparisons were mixed. The public synthetic run illustrates the same **kind of selection boundary**. It does not reproduce or replace the historical evaluation, and the source images from that evaluation are not redistributed here.

The public run can be repeated with `python examples/run_designer_cycle.py --output <empty-directory>`. Its `result.json` contains every output path and the exact worker fixture sequence. A fresh model could disagree; the lesson's current status remains experimental.
