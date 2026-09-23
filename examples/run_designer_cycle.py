"""Offline synthetic worker replay through the original Jervis mission and learning code.

The responses in synthetic/responses.json are authored fixtures, not new model calls.
Browser observations, Registry/EventLog writes, version checks and work commits are real.
"""
from __future__ import annotations

import argparse
import json
import shutil
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ironman.mission.runtime import Mission
from ironman.mission.toolkit import observe_page


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIXTURES = HERE / "synthetic"
RESPONSES = json.loads((FIXTURES / "responses.json").read_text(encoding="utf-8"))


class SyntheticResponseReplay:
    """Supplies declared fixture responses; every domain action stays in Jervis."""

    def __init__(self, names):
        self.names = iter(names)
        self.calls = 0
        self.usage = {}
        self.source_ref = None
        self.receipts = []

    def ask(self, name, instruction, data, schema, *, images=None):
        key = next(self.names)
        if data.get("source_ref"):
            self.source_ref = data["source_ref"]
        bindings = {
            "$SOURCE": self.source_ref or "",
            "$PRACTICE_HTML": (FIXTURES / "practice.html").read_text(encoding="utf-8"),
            "$VARIANT_HTML": (FIXTURES / "variant.html").read_text(encoding="utf-8"),
            "$DISPATCH_HTML": (FIXTURES / "dispatch.html").read_text(encoding="utf-8"),
        }
        response = deepcopy(RESPONSES[key])

        def bind(value):
            if isinstance(value, str):
                for token, replacement in bindings.items():
                    value = value.replace(token, replacement)
                return value
            if isinstance(value, list):
                return [bind(item) for item in value]
            if isinstance(value, dict):
                return {field: bind(item) for field, item in value.items()}
            return value

        response = bind(response)
        Draft202012Validator(schema).validate(response)
        if key in {"contact", "comparison", "visual_use", "visual_reject"} and not images:
            raise AssertionError(f"{key} did not receive image inputs")
        self.receipts.append({"call": name, "fixture": key, "images": len(images or [])})
        self.calls += 1
        return response


def node(identity, tool, *, dependencies=(), scope="page", instruction="Create and observe the page"):
    return {
        "id": identity, "title": identity.replace("-", " ").title(), "tool": tool,
        "args": {"question": instruction} if tool == "design_study" else {"instruction": instruction},
        "requires_all": list(dependencies), "requires_any": [], "joint_with": [],
        "conflicts_with": [], "resources": [], "scopes": [scope],
        "alternative_group": "", "priority": 1,
        "expected": "A file and a recorded observation", "falsified_by": "Missing artifact or failed observation",
        "assumptions": [], "status": "PENDING", "version": 1, "attempt": 0,
    }


def execute_node(mission, item):
    active = next(entry for entry in mission.state["nodes"] if entry["id"] == item["id"])
    identity, order = mission.work_order(active)
    mission.save()
    result = mission.execute_tool(order, mission.worker)
    mission.commit(identity, order, result)
    current = next(entry for entry in mission.state["nodes"] if entry["id"] == item["id"])
    if current["status"] != "COMPLETE":
        raise AssertionError(f"{item['id']} did not commit: {current['status']}")
    return current["result"]


def create_catalog(output):
    reference = output / "reference-observation"
    reference.mkdir()
    observation = observe_page(FIXTURES / "reference.html", reference)
    if not observation["screenshots"] or not Path(observation["screenshots"][0]).is_file():
        raise AssertionError("reference screenshot was not captured")
    catalog = {
        "version": 1, "status": "SYNTHETIC_SELF_AUTHORED_REFERENCE",
        "works": [{"id": "fictional-studio", "title": "Fictional community studio",
                   "images": [observation["screenshots"][0]],
                   "rights": {"usage_status": "self_authored_synthetic"}}],
        "methods": [{"id": "grouped-action-note", "title": "Self-authored grouping note",
                     "sections": ["Explore whether actions near their choice details alter visible association."],
                     "rights": {"usage_status": "self_authored_synthetic"}}],
    }
    target = output / "examples" / "apprenticeship" / "reference_catalog.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    return observation


def new_mission(output, database, project_id, brief, worker):
    mission = Mission.create(output, database, project_id, brief, worker=worker)
    mission.form_domain("designer")
    mission.save()
    return mission


def downstream(output, database, project_id, brief, fixture_suffix):
    worker = SyntheticResponseReplay([
        f"fit_{fixture_suffix}", f"page_{fixture_suffix}",
        f"semantic_{fixture_suffix}", f"visual_{fixture_suffix}",
    ])
    mission = new_mission(output, database, project_id, brief, worker)
    try:
        page = node("page", "page")
        inspect = node("inspect", "inspect", dependencies=["page"])
        mission.state["nodes"] = [page, inspect]
        mission.save()
        page_result = execute_node(mission, page)
        inspect_result = execute_node(mission, inspect)
        application = page_result["output"]["judgment_application"]
        return {
            "project": mission.id,
            "selected_judgments": application["selected_ids"],
            "rejected_as_inapplicable": application["not_applicable"],
            "domain_version": application["domain_version"],
            "page": page_result["artifacts"][0],
            "observation": inspect_result["artifacts"][0],
            "browser_checks": inspect_result["output"]["checks"],
            "workflow_passed": inspect_result["output"]["workflow_observation"]["passed"],
            "human_effect": inspect_result["output"]["visual_review"]["human_effect"],
            "worker_replay": worker.receipts,
        }
    finally:
        mission.close()


def run(output):
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Choose an empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for relative in ("schemas/ironman.schema.yaml", "control/LIFECYCLE_POLICY.yaml"):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    reference = create_catalog(output)
    database = output / "jervis.sqlite"
    worker = SyntheticResponseReplay(["source_selection", "contact", "practice", "variant", "comparison"])
    mission = new_mission(output, database, "designer-study", "Create a fictional community workshop selection page", worker)
    try:
        study = node("study", "design_study", instruction="Compare a whole page and one targeted arrangement variant")
        mission.state["nodes"] = [study]
        mission.save()
        study_result = execute_node(mission, study)
        revision = study_result["output"]["revision"]
        if revision["status"] != "experimental":
            raise AssertionError("study judgment was not retained as experimental")
        study_summary = {
            "project": mission.id,
            "decision": revision,
            "practice_checks": study_result["output"]["practice_checks"],
            "practice_page": next(path for path in study_result["artifacts"] if Path(path).parts[-2:] == ("practice", "index.html")),
            "variant_page": next(path for path in study_result["artifacts"] if Path(path).parts[-2:] == ("variant", "index.html")),
            "comparison": str(Path(study_result["artifacts"][0]).parent / "comparison.json"),
            "worker_replay": worker.receipts,
        }
    finally:
        mission.close()

    selected = downstream(output, database, "workshop-page", "Create a page with two workshop choices and an action for one choice", "use")
    rejected = downstream(output, database, "dispatch-table", "Create a dense operational dispatch table with one action for the whole table", "reject")
    if selected["selected_judgments"] != ["choice-action-group"] or rejected["selected_judgments"]:
        raise AssertionError("candidate applicability did not split across the two briefs")
    if not selected["workflow_passed"] or not rejected["workflow_passed"]:
        raise AssertionError("actual browser interaction failed")
    summary = {"mode": "SYNTHETIC_MODEL_RESPONSE_REPLAY", "new_model_calls": 0,
               "reference_observation": reference,
               "study": study_summary, "new_task_selected": selected, "new_task_rejected": rejected,
               "claim_limit": "Functional path observed; human aesthetic or capability gain not established."}
    report = output / "result.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(report), "selected": selected["selected_judgments"],
                      "rejected": rejected["rejected_as_inapplicable"],
                      "browser_passed": True, "human_effect": selected["human_effect"]}, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / ".demo" / "designer")
    args = parser.parse_args()
    run(args.output.resolve())
