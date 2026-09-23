"""One bounded source-learning batch inside a mission, using the shared memory."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ironman.learning import resources
from ironman.learning.workers import LEARNING, LEARN_INSTRUCTION, dump
from .workers import WorkerInterrupted


LEARNING_SCHEMA = deepcopy(LEARNING)
LEARNING_SCHEMA["properties"]["candidates"]["maxItems"] = 3


def research(memory, worker, directory, goal: dict, emit) -> dict:
    """Read at most three supplied public sources; return candidates and open gaps.

    Source text is supplied in full or explicitly rejected at the existing learner
    context limit. This does not recurse, promote assets, or infer source quality
    from the user's inclusion of a URL. Files here project Registry/event facts.
    """
    directory = Path(directory)
    question, domain = goal["question"], goal["domain"]
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Research requires a nonempty task gap")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("Research requires an explicit semantic domain")
    result = {"question": question, "domain": domain, "source_ids": [],
              "decisions": [], "gaps": [], "failures": [],
              "model_calls": 0, "promotion_count": 0}
    calls_before = worker.calls

    def failed(stage, identity, exc):
        item = {"stage": stage, "identity": identity,
                "error": f"{type(exc).__name__}: {exc}"}
        result["failures"].append(item)
        result["gaps"].append(f"{stage}: {identity}: {item['error']}")
        emit("RESEARCH_FAILED", item)

    seeds = []
    supplied = goal.get("sources", [])
    if len(supplied) > 3:
        result["gaps"].append(f"Source budget is three; {len(supplied) - 3} supplied sources were not processed.")
    for index, item in enumerate(supplied[:3]):
        try:
            # Reuse the acquisition boundary, including DNS resolution. acquire
            # rechecks before download and its redirect handler checks each hop.
            resources._public_url(item["url"])
            seeds.append({"url": item["url"], "title": item["title"],
                          "description": "User-supplied source for task gap: " + question})
        except Exception as exc:
            # Do not copy a rejected credential-bearing URL into evidence.
            failed("source_permission", f"supplied_source_{index + 1}", exc)

    modules = [{"module_id": "task_gap", "title": question, "keywords": [question]}]
    queue = resources.scout({
        "goal_id": directory.name, "title": question, "domain": domain,
        "modules": modules, "budget": {"max_discovered": 3, "max_sources": 3},
        "scouts": {"resources": seeds},
    }, directory, emit)
    result["queue_ref"] = str((directory / "queue.json").resolve())
    result["selected_sources"] = len(queue)
    if not queue:
        result["gaps"].append("No permitted source was selected; the task gap remains open.")

    for item in queue:
        identity, stage = item["candidate_id"], "acquisition"
        try:
            source = memory.register_source(resources.acquire(item, directory / "sources"))
            identity = source["object_id"]
            result["source_ids"].append(identity)
            emit("RESEARCH_SOURCE_ACQUIRED", {"source_id": identity,
                 "content_ref": source["content_ref"], "raw_content_ref": source["raw_content_ref"],
                 "perceived": "full_processed_text", "unperceived": ["image_pixels", "audio", "interaction"]})
            stage = "learning"
            text = memory._source_text(source)
            if len(text) > 120000:
                raise ValueError("SOURCE_EXCEEDS_FULL_CONTENT_CONTEXT_BUDGET")
            learned = worker.ask("research_" + identity.replace(":", "_") + "_learn",
                                 LEARN_INSTRUCTION,
                                 {"source": source, "curriculum": modules, "full_text": text},
                                 LEARNING_SCHEMA)
            dump(directory / (identity + ".learned.json"), learned)
            Draft202012Validator(LEARNING_SCHEMA).validate(learned)
            result["gaps"].extend(learned["open_gaps"])
            for candidate in learned["candidates"]:
                try:
                    if (candidate["source_id"] != identity or
                            any(e["source_id"] != identity for e in candidate["evidence"])):
                        raise ValueError("Learner evidence must refer to its supplied source")
                    if candidate["module_id"] != "task_gap" or candidate["relations"]:
                        raise ValueError("Learner may only use the supplied module and no unseen memory relations")
                    candidate = {**candidate, "domain": domain}
                    outcome = memory.consolidate(candidate, {"action": "NEW", "target_id": "",
                        "reason": "Exact source evidence; experimental candidate only, no validated effect or promotion."})
                    result["decisions"].append({"candidate_id": candidate["candidate_id"], **outcome})
                    emit("RESEARCH_CANDIDATE_CONSOLIDATED", result["decisions"][-1])
                except Exception as exc:
                    rejection = {"candidate_id": candidate["candidate_id"], "action": "REJECT",
                                 "reason": f"{type(exc).__name__}: {exc}"}
                    result["decisions"].append(rejection)
                    emit("RESEARCH_CANDIDATE_REJECTED", rejection)
                    failed("consolidation", candidate["candidate_id"], exc)
        except WorkerInterrupted:
            # Cancellation belongs to the coordinator, never to another research attempt.
            raise
        except Exception as exc:
            failed(stage, identity, exc)

    result["model_calls"] = worker.calls - calls_before
    result["status"] = "BATCH_COMPLETE_WITH_GAPS" if result["gaps"] else "BATCH_COMPLETE"
    dump(directory / "research.json", result)
    emit("RESEARCH_BATCH_COMPLETED", result)
    return result
