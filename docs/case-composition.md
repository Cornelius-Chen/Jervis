# Composition case · change one fact, preserve the rest

[← Jervis](../README.md) · [Runnable entry point](../examples/run_composition_cycle.py)

The public fixture supplies an ordinary brief: a fictional creature moves across a wood floor, with contact sound, a page, and an independent production note. A fixed response worker stands in for external model calls. The plan goes through the existing vNext `Mission.replan`; the `scene`, `audio`, `page`, and `compose_inspect` work goes through the existing `Toolkit`.

## What the visitor can inspect

1. `python examples/run_composition_cycle.py` creates two complete versions in `.demo/composition/`.
2. The initial `scene.json` holds a moving entity, two contact events and their timestamps. The `audio.json` and `contacts.wav` bind those same events to actual PCM sample positions. The HTML page embeds that WAV; Chromium observes play, pause, seek and contact-marker state.
3. `Mission.apply_controls` changes `floor.material` from wood to gravel with the previous field version as its precondition. `scopes.semantic_impact` finds the sound branch. `dependent_closure` propagates to page and combined observation. Motion and the independent note retain their original result references.
4. A synthetic handoff revokes an in-flight sound attempt. Its late output is recorded as `STALE` by `Mission.commit`; a replacement attempt commits the new gravel sound.
5. `result.json` gives the exact before/after revisions, retained and rebuilt nodes, impact record, stale status, and both WAV paths. Inspect `.demo/composition/projects/pip-composition/work/observe/` for the real browser observations and screenshots.

The actual tool entry points are [`runtime.py`](../src/ironman/mission/runtime.py) (`replan`, `apply_controls`, `work_order`, `commit`), [`scopes.py`](../src/ironman/mission/scopes.py) (`change_fact`, `semantic_impact`, `compile_context`), [`compound.py`](../src/ironman/mission/compound.py) (`scene`, `audio`, `inspect`), and [`execution.py`](../src/ironman/mission/execution.py) (attempt lifecycle).

## Design decision

**Problem:** a changed shared fact can make one specialty's output stale without making every other artifact stale. Replaying the whole project would lose useful completed work; keeping every result would let obsolete inputs leak into the final composition.

**Choice:** contracts name the entity fields each node reads. The runtime invalidates those nodes and their actual dependents, retains independent completed nodes, and binds each dispatched attempt to input and entity versions. A revoked or outdated attempt is saved as a rejected result rather than overwriting current state.

**Cost:** workers must declare semantic dependencies accurately. A missing field dependency can hide an affected branch. The independent browser observation checks synchronized page behavior but cannot measure whether listeners perceive the procedural sound as wood or gravel. The fixed worker responses demonstrate runtime mechanics, not model planning quality or improved creative judgment.
