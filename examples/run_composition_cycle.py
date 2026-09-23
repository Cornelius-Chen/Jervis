"""Offline vNext replay: a shared fact changes, only dependent work is rebuilt."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil

from ironman.mission.runtime import Mission
from ironman.mission.toolkit import Toolkit


ROOT = Path(__file__).resolve().parents[1]
BRIEF = "Pip is a fictional creature. The floor is wood. Make a motion and contact-sound page, plus an independent production note."


def contract(scope, fields=(), purpose="Compose a fictional scene"):
    return {
        "purpose": purpose, "receiver_scope": scope,
        "entity_fields": [{"entity_id": entity, "fields": names} for entity, names in fields],
        "input_conditions": [], "units_and_timing": [], "expected_outputs": [], "checks": [],
        "assumptions": [], "missing_information": ["No measured creature mass or human listening judgment."],
        "validity_conditions": [], "memory_query": "fictional contact composition",
    }


def node(identity, tool, scope, *, requires=(), fields=()):
    return {
        "id": identity, "title": identity, "tool": tool,
        "args_json": json.dumps({"instruction": f"Create the {identity} part of this fictional work."}),
        "requires_all": list(requires), "requires_any": [], "conflicts_with": [], "joint_with": [],
        "resources": [], "scopes": [scope], "alternative_group": "", "priority": 1,
        "expected": "A source-bound artifact", "falsified_by": "Missing or inconsistent artifact",
        "assumptions": [], "contract": contract(scope, fields),
    }


def plan():
    return {
        "goal_summary": "Fictional motion, contact audio and page",
        "domains": ["motion", "audio", "writing", "observation"], "needs": [],
        "entities": [
            {"entity_id": "pip", "fields": []},
            {"entity_id": "floor", "fields": [{"name": "material", "value_json": '"wood"',
                "status": "confirmed", "source_quote": "The floor is wood.", "unit": "",
                "owner_scope": "audio/material"}]},
        ],
        "nodes": [
            node("motion", "scene", "motion", fields=(("pip", []), ("floor", []))),
            node("sound", "audio", "audio", requires=("motion",),
                 fields=(("pip", []), ("floor", ["material"]))),
            node("page", "page", "designer", requires=("motion", "sound")),
            node("observe", "compose_inspect", "observation", requires=("motion", "sound", "page")),
            node("notes", "text", "writing"),
        ],
        "alternatives": [], "reason": "The sound depends on material; motion and notes do not.",
        "unknowns": ["Whether people prefer the resulting sound and page."],
        "hypotheses": [], "questions": [], "decision": "execute",
    }


def page_html():
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pip contact study</title>
<style>body{font:18px system-ui;max-width:760px;margin:40px auto;padding:20px;background:#f7f5ef;color:#183a33}h1{font-size:2.5rem}button{padding:12px;margin:4px}#stage{height:130px;border:2px solid #4b705e;position:relative}#scene-entity{width:56px;height:56px;background:#b3d19c;border-radius:50%;position:absolute;top:35px;left:30px}#contact-marker{min-height:32px}</style>
<h1>Pip · contact study</h1><p>A fictional sound and movement sketch. Material fidelity and audience response are untested.</p>
<audio id="scene-audio" src="JERVIS_AUDIO_URI" preload="auto"></audio><button data-action="play">Play</button><button data-action="pause">Pause</button><label>Time <input aria-label="Time" type="range" data-action="seek" min="0" max="1.5" step=".01"></label><div id="stage"><div id="scene-entity" data-entity-id="pip"></div></div><div id="contact-marker" aria-live="polite"></div>
<script>const a=document.querySelector('audio'),e=document.querySelector('#scene-entity'),m=document.querySelector('#contact-marker'),s=document.querySelector('input');function render(){e.dataset.timeS=a.currentTime;e.style.transform='translateX('+a.currentTime*280+'px)';m.dataset.eventId=a.currentTime>=.8?'step-2':a.currentTime>=.2?'step-1':'';m.textContent=m.dataset.eventId?('Contact '+m.dataset.eventId):'Waiting for contact';}document.querySelector('[data-action=play]').onclick=()=>a.play();document.querySelector('[data-action=pause]').onclick=()=>a.pause();s.oninput=()=>{a.currentTime=Number(s.value);render()};a.ontimeupdate=render;a.onseeked=render;function tick(){render();requestAnimationFrame(tick)}tick();</script></html>'''


class FixedWorker:
    """Replaces external model output; the Mission and tool implementations stay original."""

    def __init__(self):
        self.calls = 0
        self.usage = {}

    def ask(self, name, instruction, data, schema, **kwargs):
        self.calls += 1
        if "plan" in name:
            return deepcopy(plan())
        if name.startswith("motion-"):
            return {"entity_id": "pip", "label": "Pip", "duration_s": 1.5,
                    "parts": [{"id": "foot", "shape": "ellipse", "x": .1, "y": .1,
                               "width": .15, "height": .15, "fill": "#b3d19c"}],
                    "keyframes": [{"time_s": 0, "x": .1, "y": .5, "angle_deg": 0},
                                  {"time_s": 1.5, "x": .8, "y": .5, "angle_deg": 0}],
                    "events": [{"event_id": f"step-{number}", "entity_id": "pip", "limb_id": "foot",
                                "surface_id": "floor", "time_s": moment}
                               for number, moment in ((1, .2), (2, .8))],
                    "limitations": ["Fictional motion, not a physical simulation."]}
        if name.startswith("sound-"):
            refs = data["semantic_contract"]["entity_refs"]
            material = next(ref["fields"]["material"]["value"] for ref in refs if ref["entity_id"] == "floor")
            tone = 180 if material == "wood" else 650
            return {"intent": f"Constructed {material} contact texture",
                    "voices": [{"surface_id": "floor", "reason": "Fictional texture experiment",
                                "duration_s": .1, "gain": .3, "noise_mix": .3, "decay": 6,
                                "tones": [{"frequency_hz": tone, "weight": 1}]}],
                    "limitations": ["No listening or material-fidelity assessment."]}
        if name.startswith("page-"):
            return {"html": page_html(), "used_capability_ids": [],
                    "limitations": ["No human design judgment."]}
        if name.startswith("notes-"):
            return {"text": "# Production note\n\nPip is fictional. Contact sound is a procedural study. Human listening and design effects are unknown.\n",
                    "limitations": ["No human review."]}
        raise ValueError(f"Unexpected model fixture request: {name}")


def dispatch(mission, identity):
    item = next(item for item in mission.state["nodes"] if item["id"] == identity)
    work_id, order = mission.work_order(item)
    mission.save()
    result = mission.execute_tool(order, mission.worker)
    return work_id, order, result


def complete(mission, identity):
    work_id, order, result = dispatch(mission, identity)
    mission.commit(work_id, order, result)
    return next(item for item in mission.state["nodes"] if item["id"] == identity)["result"]


def run(output: Path):
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Choose an empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for relative in ("schemas/ironman.schema.yaml", "control/LIFECYCLE_POLICY.yaml"):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    mission = Mission.create(output, output / "state.sqlite", "pip-composition", BRIEF,
                             architecture="vnext", worker=FixedWorker(), toolkit=Toolkit())
    try:
        mission.replan()
        for identity in ("motion", "notes", "sound", "page", "observe"):
            complete(mission, identity)
        before = {item["id"]: {"version": item["version"], "result_ref": item["result"]["result_ref"]}
                  for item in mission.state["nodes"]}
        old_wav = next(item for item in mission.state["nodes"] if item["id"] == "sound")["result"]["output"]["interface"]["wav_path"]

        mission.store.control(mission.id, "fact_change", {"entity_id": "floor", "field": "material",
            "value": "gravel", "expected_version": 1, "source_kind": "test_intervention",
            "source_ref": "public synthetic owner intervention"})
        mission.apply_controls()
        impact = deepcopy(mission.state["impact_history"][-1])
        mission.replan()
        late_id, late_order, late_result = dispatch(mission, "sound")
        mission.store.control(mission.id, "handoff", {"node": "sound", "reason": "Synthetic worker replacement"})
        mission.apply_controls()
        mission.commit(late_id, late_order, late_result)
        late_record, _ = mission.store.load(late_id + ":result")
        for identity in ("sound", "page", "observe"):
            complete(mission, identity)
        after = {item["id"]: {"version": item["version"], "result_ref": item["result"]["result_ref"]}
                 for item in mission.state["nodes"]}
        new_wav = next(item for item in mission.state["nodes"] if item["id"] == "sound")["result"]["output"]["interface"]["wav_path"]
        report = {"mode": "SYNTHETIC_MODEL_RESPONSE_REPLAY", "impact": impact,
                  "before": before, "after": after, "late_commit_status": late_record["commit_status"],
                  "old_wav": old_wav, "new_wav": new_wav,
                  "unchanged": [name for name in before if before[name] == after[name]],
                  "rebuilt": [name for name in before if before[name] != after[name]],
                  "human_quality_status": "AWAITING_HUMAN_EVIDENCE"}
        if report["unchanged"] != ["motion", "notes"] or set(report["rebuilt"]) != {"sound", "page", "observe"}:
            raise AssertionError(report)
        if report["late_commit_status"] != "STALE" or Path(old_wav).read_bytes() == Path(new_wav).read_bytes():
            raise AssertionError("Late result or material-sensitive audio did not change as expected")
        path = output / "result.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"result": str(path), "unchanged": report["unchanged"],
                          "rebuilt": report["rebuilt"], "late": report["late_commit_status"]}))
        return report
    finally:
        mission.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".demo" / "composition")
    run(parser.parse_args().output.resolve())
