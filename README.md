# Jervis: learning that survives a task

Jervis is an experimental architecture for turning source-bound observations into **conditional, revisable domain judgments**. A short-lived worker may propose a decision or create an artifact; the persistent project, Registry and EventLog retain the source, version, work order, result and limits of what was learned.

This repository is a runnable public slice of the local Jervis implementation. It focuses on Designer as the first domain example. The original Designer corpus is a separate advisory source; the single judgment in this repository is an explicitly synthetic exercise and is **not** a copy of that corpus.

| One run, two new tasks | Observed result |
|---|---|
| Workshop choice page | The persisted `choice-action-group` candidate was selected and a full HTML page was committed. |
| Operational dispatch table | The same candidate was rejected as inapplicable; the table page was committed without it. |
| Both pages | Headless browser checks exercised the real expanding control. Human comprehension and aesthetic effect remain unmeasured. |

<p align="center"><img src="assets/workshop-page.png" width="48%" alt="Synthetic workshop page with the action inside its choice card"><img src="assets/dispatch-table.png" width="48%" alt="Synthetic dispatch table that did not use the card-specific judgment"></p>

## Run the complete public example

Python 3.12 or newer is required. Chromium runs headlessly; no account, API key, model download, market data or external source is needed.

```bash
python -m pip install ".[test]"
python -m playwright install chromium
python examples/run_designer_cycle.py
python examples/run_composition_cycle.py
python -m pytest
```

On Linux, install Chromium's system dependencies if Playwright asks for them (`python -m playwright install --with-deps chromium`). The example writes to `.demo/designer/` and refuses to overwrite an existing nonempty run. Use `--output PATH` for another run.

Open `.demo/designer/result.json` after execution. It links to the two practice pages, comparison, persisted task artifacts and browser observations. The example calls the original `Mission`, `DomainLearning`, `StateStore`, `Registry`, `EventLog`, `work_order`, `execute_tool` and `commit` paths. `examples/synthetic/responses.json` supplies fixed, visibly labeled model responses. **This run makes zero new model calls.**

The second command writes `.demo/composition/result.json`. It exercises a **separate** vNext composition on fictional material with fixed model responses. It also makes zero new model calls.

### What happens inside

```text
self-authored reference image + note
    → actual study node: practice + variant + browser capture
    → synthetic comparison response proposes one bounded judgment
    → Registry stores experimental version and source references
    → two fresh projects load that version
    → applicability selects it for one brief, rejects it for another
    → WorkOrder → HTML artifact → browser interaction → EventLog commit
```

The practice and variant use the same task and button behavior; only the action's grouping changes. The screenshots below are from the executable example, not borrowed reference imagery.

<p align="center"><img src="assets/study-practice.png" width="48%" alt="Practice arrangement"><img src="assets/study-variant.png" width="48%" alt="Variant arrangement"></p>

## Architecture decisions worth inspecting

| Question | Choice in the implementation | Evidence |
|---|---|---|
| What persists when a worker ends? | Project state and versioned objects live in `StateStore` / `Registry`; events capture attempted and committed transitions. | [`state.py`](src/ironman/mission/state.py), [`storage.py`](src/ironman/storage.py), generated SQLite file |
| How is a judgment kept tentative? | `DomainLearning.record_observed` retains the proposal, prior version, evidence and `experimental`/`no_update`/`rejected` decision. | [`domains.py`](src/ironman/mission/domains.py), [`test_apprenticeship.py`](tests/mission/test_apprenticeship.py) |
| How does a later task avoid blindly inheriting style? | `work_order` calls Designer applicability on the saved version; an empty selection is valid. | [`runtime.py`](src/ironman/mission/runtime.py), [`design_judgment.py`](src/ironman/mission/design_judgment.py), generated `result.json` |
| How do workers receive only relevant facts? | The scoped path compiles a permission-checked context packet and binds entity/input versions before dispatch. | [`scopes.py`](src/ironman/mission/scopes.py), [`test_scopes.py`](tests/mission/test_scopes.py) |

More detail: [architecture and tradeoffs](docs/architecture.md) · [Designer run and evidence](docs/case-designer.md) · [source and claim map](docs/evidence.md).

## A second, independent composition run

The fictional scene begins with a wood floor. The original `scene` tool produces timed contacts; the original `audio` tool renders a real WAV; `page` binds it into an HTML interaction; `compose_inspect` checks playback and seeking in Chromium. An owner-shaped synthetic intervention changes `floor.material` to gravel.

| After the fact changes | Actual runtime result |
| --- | --- |
| Sound + page + combined observation | Rebuilt at revision 2. The WAV bytes change. |
| Motion + independent production note | Their original result references and revisions remain. |
| A sound worker result returned after a handoff | Commit stored it as `STALE`; a successor result was accepted. |

![Actual headless-browser screenshot of the fictional composed page](assets/composition-page.png)

[Hear the wood fixture](assets/composition-wood.wav) · [Hear the gravel fixture](assets/composition-gravel.wav) · [Inspect the case and its exact source entry points](docs/case-composition.md)

These are procedural sounds chosen by a fixed response fixture. Their acoustic realism, design quality and human effect were not evaluated. The material change, dependency impact, versioned result references, WAV render, browser checks and stale-result rejection run through the original Mission and Toolkit code.

## Repository map

```text
src/ironman/storage.py        Registry and EventLog
src/ironman/learning/         evidence, candidate memory, source intake, workers
src/ironman/mission/          project state, work orders, scope, Designer, execution
schemas/ + control/          original object schema and lifecycle policy
examples/synthetic/          self-authored pages and fixed model responses
examples/run_designer_cycle.py  domain-learning replay
examples/run_composition_cycle.py  independent vNext composition replay
tests/                      original focused tests plus clean-output integration
docs/                       architecture choices, case narrative and evidence limits
```

The public slice includes the real source modules needed by the run and the surrounding architecture. The only release portability edits change the local Sales triage import to an in-package source file and use Playwright's bundled Chromium instead of a machine-specific Edge channel. No private Registry, original Designer media, model credentials or local user data is included.

## Evidence boundary

- The public run proves that the **mechanism executes** with synthetic model responses: it persists a candidate, reopens it in new projects, selects or rejects it, writes artifacts and checks browser behavior.
- The fixed responses do not prove that a live model would make the same design judgment. Functional browser checks do not prove better design, user preference, comprehension or general capability gain.
- The public composition replay independently verifies a field-specific invalidation, preservation of unrelated work, actual WAV and browser artifacts, and a rejected late worker result. It does not claim the Designer learning happened in the same execution.

No open-source license has been selected for this public source release.
