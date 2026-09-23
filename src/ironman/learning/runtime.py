"""Finite control plane. Cognitive work is delegated; all commits are serialized."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .memory import Memory
from .resources import scout, acquire
from .workers import (CodexWorker, ReplayWorker, dump, LEARNING, CONSOLIDATION,
                      APPLICATION, REFLECTION, LEARN_INSTRUCTION, CONSOLIDATE_INSTRUCTION,
                      APPLY_INSTRUCTION, REFLECT_INSTRUCTION)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def plain(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def render_report(directory, state):
    """An inspectable local status view, including failures and artifact links."""
    def block(title, value):
        return f'<h2>{html.escape(title)}</h2><pre>{html.escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>'
    links = [("goal.json", "Goal & budget"), ("queue.json", "Frozen resource queue"),
             ("candidates.jsonl", "Discovery decisions"), ("acquired.json", "Full-content provenance"),
             ("learned.json", "Structured candidates"), ("consolidation.json", "Memory decisions"),
             ("events.jsonl", "Durable ledger export"), ("task/application.json", "Fresh-task application"),
             ("task/context.json", "Retrieved context"), ("task/experience.json", "Outcome & reflection")]
    body = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Jervis learning run</title>'
            '<style>body{font:16px system-ui;margin:40px auto;max-width:1100px;padding:0 24px;background:#f4f5f7;color:#182534}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px;border-radius:8px;font-size:13px}'
            'a{color:#12617e}nav{display:flex;flex-wrap:wrap;gap:20px}h1{font-size:32px}</style>'
            f'<h1>{html.escape(state["goal"])}</h1><p>{html.escape(state["status"])} · '
            f'{html.escape(state.get("stop_reason", ""))}</p><nav>')
    body += ''.join(f'<a href="{p}">{label}</a>' for p, label in links if (directory / p).exists()) + '</nav>'
    for key in ("resources", "knowledge", "modules", "workers", "budget_consumed", "open_gaps", "failures", "integration"):
        body += block(key.replace("_", " ").title(), state.get(key, {}))
    body += '<p>Candidate/evaluation use. This run does not establish sales outcomes or production authority.</p></html>'
    (directory / "report.html").write_text(body, encoding="utf-8")


class Runtime:
    def __init__(self, root, database, run_dir, goal, worker=None, replay=None, reuse_learning=None):
        self.root, self.database, self.directory = Path(root).resolve(), Path(database).resolve(), Path(run_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.goal = goal
        self.replay = Path(replay).resolve() if replay else None
        self.reuse_learning = Path(reuse_learning).resolve() if reuse_learning else None
        self.source_run = self.replay or self.reuse_learning
        self.run_id = self.directory.name
        self.memory = Memory(self.root, self.database)
        budget = goal["budget"]
        if not (1 <= budget["max_workers"] <= 3 and 1 <= budget["max_sources"] <= budget["max_discovered"]
                and budget["max_model_calls"] > 0):
            raise ValueError("Invalid bounded goal budget")
        self.worker = worker or (ReplayWorker(self.replay / "workers") if self.replay else
                                 CodexWorker(self.directory / "workers", budget["max_model_calls"], budget["worker_timeout_seconds"]))
        self.state = {"run_id": self.run_id, "goal": goal["title"], "status": "CREATED", "stop_reason": "",
                      "started_at": now(), "mode": "replay" if replay else "reprocess_saved_learning" if reuse_learning else "live",
                      "modules": [{**m, "status": "QUEUED", "candidates": 0} for m in goal["modules"]],
                      "resources": {k: 0 for k in ("discovered", "shortlisted", "acquired", "learned")},
                      "knowledge": {k: 0 for k in ("candidates", "skills_candidates", "consolidated", "duplicates", "conflicts", "rejected")},
                      "workers": {"active": [], "completed": 0, "failed": 0},
                      "budget_consumed": {}, "open_gaps": [], "failures": [], "integration": {}}
        dump(self.directory / "goal.json", goal)
        self.emit("GOAL_CREATED", {"goal": goal, "mode": self.state["mode"]})
        self.emit("CURRICULUM_CREATED", {"modules": goal["modules"]})
        if self.reuse_learning:
            self.emit("PRIOR_LEARNING_REUSED", {"source_run": str(self.reuse_learning), "new_cognition": "consolidation_and_fresh_task_only"})

    def emit(self, event_type, payload):
        self.memory.registry.events.append(stream_id=f"learning:{self.run_id}", actor="learning_orchestrator",
                                           event_type=event_type, payload=payload)
        if event_type == "RESOURCE_DISCOVERED":
            self.state["resources"]["discovered"] += 1
        if event_type == "SCOUT_FAILED":
            self.state["failures"].append({"stage": "SCOUT", **payload})
        self.save()

    def save(self):
        self.state["budget_consumed"] = {"model_calls": self.worker.calls, **self.worker.usage}
        dump(self.directory / "state.json", self.state)
        render_report(self.directory, self.state)

    def gap(self, description, evidence, stage):
        if description in self.state["open_gaps"]:
            return
        self.state["open_gaps"].append(description)
        identity = f"gap:{self.run_id}:{len(self.state['open_gaps'])}"
        self.memory.registry.put_new_version({
            "object_id": identity, "object_type": "CapabilityGap", "version": "0.1.0",
            "semantic_owner": self.goal["domain"], "lifecycle_state": "candidate", "created_at": now(),
            "created_by": "learning_orchestrator", "provenance_refs": [evidence],
            "gap_id": identity, "mission_id": self.run_id, "desired_outcome": self.goal["title"],
            "failed_stage": stage, "evidence_refs": [evidence], "insufficiency_reason": description,
            "target_domains": [self.goal["domain"]], "priority": "medium", "status": "open"})
        self.emit("GAP_IDENTIFIED", {"gap_id": identity, "description": description, "status": "OPEN_GAP", "automatic_followup": False})

    def fail(self, stage, worker_id, exc):
        self.state["workers"]["failed"] += 1
        item = {"stage": stage, "worker_id": worker_id, "error": str(exc)}
        self.state["failures"].append(item)
        self.emit("WORKER_FAILED", item)
        self.gap(f"{stage}: {worker_id}: {exc}", f"learning:{self.run_id}", stage)

    def learn(self, source):
        text = Path(source["content_ref"]).read_text(encoding="utf-8")
        # Do not silently learn a prefix and call it the full source.
        if len(text) > self.goal["budget"].get("max_source_chars", 120000):
            raise ValueError("SOURCE_EXCEEDS_FULL_CONTENT_CONTEXT_BUDGET")
        name = source["object_id"].replace(":", "_") + "_learn"
        worker = ReplayWorker(self.reuse_learning / "workers") if self.reuse_learning else self.worker
        if self.reuse_learning:
            for suffix in (".input.json", ".json"):
                shutil.copyfile(self.reuse_learning / "workers" / (name + suffix), self.directory / "workers" / (name + suffix))
        return worker.ask(name, LEARN_INSTRUCTION,
                               {"source": source, "curriculum": self.goal["modules"], "full_text": text}, LEARNING)

    def parallel(self, items, function, identity, stage):
        """Only submitted work is active; waiting resources stay visibly queued."""
        waiting = iter(items)
        with ThreadPoolExecutor(max_workers=self.goal["budget"]["max_workers"]) as pool:
            pending = {}
            def submit():
                item = next(waiting, None)
                if item is None:
                    return False
                pending[pool.submit(function, item)] = item
                self.state["workers"]["active"].append(item[identity])
                self.state["workers"]["queued"] -= 1
                self.emit(stage + "_STARTED", {"worker_id": item[identity]})
                return True
            self.state["workers"]["queued"] = len(items)
            for _ in range(self.goal["budget"]["max_workers"]):
                submit()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    item = pending.pop(future)
                    yield future, item
                    self.state["workers"]["active"].remove(item[identity])
                    submit()
                    self.save()

    def execute(self, task_path=None):
        learned, acquired, decisions = [], [], []
        try:
            self.state["integration"] = self.memory.import_designer(self.root / "registry/designer/p1_s0c")
            self.emit("EXISTING_REGISTRY_CONNECTED", self.state["integration"])
            self.state["status"] = "SCOUTING"
            if self.source_run:
                queue = json.loads((self.source_run / "queue.json").read_text(encoding="utf-8"))
                old_state = json.loads((self.source_run / "state.json").read_text(encoding="utf-8"))
                self.state["resources"]["discovered"] = old_state["resources"]["discovered"]
                dump(self.directory / "queue.json", queue)
                shutil.copyfile(self.source_run / "candidates.jsonl", self.directory / "candidates.jsonl")
                self.emit("SCOUT_REPLAYED", {"origin": str(self.source_run), "queue_size": len(queue)})
            else:
                queue = scout(self.goal, self.directory, self.emit)
            self.state["resources"]["shortlisted"] = len(queue)
            self.emit("QUEUE_FROZEN", {"selected_ids": [r["candidate_id"] for r in queue], "size": len(queue)})
            self.state["status"] = "ACQUIRING"
            if self.source_run:
                acquired = json.loads((self.source_run / "acquired.json").read_text(encoding="utf-8"))
                for source in acquired:
                    self.memory.register_source(source)
                    self.emit("RESOURCE_ACQUIRED", {"source_id": source["object_id"], "provenance": source, "replayed": True})
            else:
                for future, resource in self.parallel(queue, lambda r: acquire(r, self.directory / "sources"), "candidate_id", "ACQUISITION"):
                        try:
                            source = self.memory.register_source(future.result())
                            acquired.append(source)
                            self.state["workers"]["completed"] += 1
                            self.emit("RESOURCE_ACQUIRED", {"source_id": source["object_id"], "provenance": source})
                        except Exception as exc:
                            self.fail("ACQUISITION", resource["candidate_id"], exc)
                        self.state["resources"]["acquired"] = len(acquired)
                        self.save()
            acquired.sort(key=lambda s: s["object_id"])
            dump(self.directory / "acquired.json", acquired)
            self.state["resources"]["acquired"] = len(acquired)
            self.state["status"] = "LEARNING"
            for module in self.state["modules"]:
                module["status"] = "LEARNING"
            for future, source in self.parallel(acquired, self.learn, "object_id", "LEARNING"):
                    try:
                        result = future.result()
                        allowed_modules = {m["module_id"] for m in self.goal["modules"]}
                        if len(result["candidates"]) > 3:
                            raise ValueError("PER_SOURCE_CANDIDATE_BUDGET_EXCEEDED")
                        for candidate in result["candidates"]:
                            if candidate["source_id"] != source["object_id"] or candidate["module_id"] not in allowed_modules:
                                raise ValueError("CANDIDATE_SOURCE_OR_MODULE_MISMATCH")
                            if any(e["source_id"] != source["object_id"] for e in candidate["evidence"]):
                                raise ValueError("CROSS_SOURCE_EVIDENCE_IN_ISOLATED_LEARNER")
                        learned.append({"source_id": source["object_id"], **result})
                        self.state["resources"]["learned"] += 1
                        self.state["workers"]["completed"] += 1
                        for candidate in result["candidates"]:
                            key = "skills_candidates" if candidate["memory_type"] == "skill" else "candidates"
                            self.state["knowledge"][key] += 1
                            self.emit("SKILL_CANDIDATE_CREATED" if key == "skills_candidates" else "KNOWLEDGE_CANDIDATE_CREATED", {"candidate": candidate})
                        for gap in result["open_gaps"]:
                            self.gap(gap, source["object_id"], "LEARNING")
                    except Exception as exc:
                        self.fail("LEARNING", source["object_id"], exc)
                    self.save()
            learned.sort(key=lambda x: x["source_id"])
            dump(self.directory / "learned.json", learned)
            self.state["status"] = "CONSOLIDATING"
            for group in learned:
                candidates = group["candidates"]
                if not candidates:
                    continue
                for candidate in candidates:
                    candidate["domain"] = self.goal["domain"]
                comparisons = [{"candidate": c, "related": [m for m in self.memory.related(c["statement"], domain=self.goal["domain"], limit=5) if m["memory_type"] != "experience"]} for c in candidates]
                try:
                    self.state["workers"]["active"] = ["consolidator:" + group["source_id"]]
                    self.emit("CONSOLIDATION_STARTED", {"source_id": group["source_id"], "candidate_ids": [c["candidate_id"] for c in candidates]})
                    result = self.worker.ask(group["source_id"].replace(":", "_") + "_consolidate",
                                             CONSOLIDATE_INSTRUCTION, {"comparisons": comparisons}, CONSOLIDATION)
                    decision_map = {d["candidate_id"]: d for d in result["decisions"]}
                    if len(decision_map) != len(candidates) or set(decision_map) != {c["candidate_id"] for c in candidates}:
                        raise ValueError("CONSOLIDATOR_MISSING_OR_DUPLICATE_DECISION")
                    for comparison in comparisons:
                        c = comparison["candidate"]
                        d = decision_map[c["candidate_id"]]
                        if d["target_id"] and d["target_id"] not in {r["object_id"] for r in comparison["related"]}:
                            raise ValueError("CONSOLIDATION_TARGET_NOT_RETRIEVED")
                    for c in candidates:
                        try:
                            outcome = self.memory.consolidate(c, decision_map[c["candidate_id"]])
                            decisions.append({"candidate_id": c["candidate_id"], **outcome})
                            action = outcome["action"]
                            key = "duplicates" if action == "DUPLICATE" else "conflicts" if action == "CONFLICT" else "rejected" if action == "REJECT" else "consolidated"
                            self.state["knowledge"][key] += 1
                            event = "CONFLICT_FOUND" if action == "CONFLICT" else "CANDIDATE_REJECTED" if action == "REJECT" else "KNOWLEDGE_MERGED" if action in ("EXTENSION", "DUPLICATE", "SUPERSEDES") else "MEMORY_UPDATED"
                            self.emit(event, {"candidate_id": c["candidate_id"], **outcome})
                            if outcome["lifecycle_state"] == "candidate":
                                for module in self.state["modules"]:
                                    if module["module_id"] == c["module_id"]:
                                        module["candidates"] += 1
                        except Exception as exc:
                            self.fail("CONSOLIDATION", c["candidate_id"], exc)
                except Exception as exc:
                    self.fail("CONSOLIDATION", group["source_id"], exc)
                finally:
                    self.state["workers"]["active"] = []
                    self.save()
            dump(self.directory / "consolidation.json", decisions)
            for module in self.state["modules"]:
                module["status"] = "COMPLETE" if module["candidates"] else "OPEN_GAP"
                if not module["candidates"]:
                    self.gap(f"No consolidated material for module: {module['title']}", f"learning:{self.run_id}", "COVERAGE")
            if not decisions or not any(d.get("object_id") and d.get("lifecycle_state") == "candidate" for d in decisions):
                raise RuntimeError("NO_USABLE_MEMORY_COMMITTED")
            if task_path:
                self.state["status"] = "FRESH_TASK"
                self.state["workers"]["active"] = ["fresh-task"]
                self.save()
                task_dir = self.directory / "task"
                remaining = self.goal["budget"]["max_model_calls"] - self.worker.calls
                if remaining < 3:
                    raise RuntimeError("INSUFFICIENT_BUDGET_FOR_FRESH_TASK")
                command = [sys.executable, "-m", "ironman.learning", "task", "--root", str(self.root),
                           "--database", str(self.database), "--task", str(Path(task_path).resolve()),
                           "--output", str(task_dir), "--run-id", self.run_id,
                           "--timeout", str(self.goal["budget"]["worker_timeout_seconds"])]
                if self.replay:
                    command += ["--replay", str(self.replay / "task/workers")]
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                           timeout=3 * self.goal["budget"]["worker_timeout_seconds"] + 60)
                if completed.returncode:
                    raise RuntimeError(f"FRESH_TASK_FAILED: {completed.stderr[-1500:]}")
                task_result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
                self.worker.calls += task_result["model_calls"]
                for key in self.worker.usage:
                    self.worker.usage[key] += task_result["usage"].get(key, 0)
                self.state["retrieval"] = task_result
                self.emit("FRESH_TASK_COMPLETED", task_result)
                if not task_result["checks"]["passed"]:
                    raise RuntimeError("FRESH_TASK_APPLICATION_CHECKS_FAILED")
            self.state["status"] = "STOP"
            self.state["stop_reason"] = "BOUNDED_QUEUE_COMPLETED_WITH_VISIBLE_FAILURES" if self.state["failures"] else "BOUNDED_QUEUE_COMPLETED"
            self.emit("BATCH_COMPLETED", {"resources": self.state["resources"], "knowledge": self.state["knowledge"], "stop_reason": self.state["stop_reason"]})
        except Exception as exc:
            self.state["status"] = "STOP_FAILED"
            self.state["stop_reason"] = str(exc)
            self.emit("BATCH_FAILED", {"reason": str(exc)})
        finally:
            self.state["workers"]["active"] = []
            self.state["ended_at"] = now()
            self.save()
            self.export_events()
        return self.state

    def export_events(self):
        events = self.memory.registry.events.read_stream(f"learning:{self.run_id}")
        with (self.directory / "events.jsonl").open("w", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps({"sequence": event.stream_sequence, "event_type": event.event_type,
                                         "occurred_at": event.occurred_at, "payload": plain(event.payload),
                                         "event_hash": event.event_hash}, ensure_ascii=False) + "\n")
        self.memory.registry.events.verify_stream(f"learning:{self.run_id}")


def fresh_task(root, database, task_path, output, run_id, timeout=240, replay=None):
    """Invoked in a new Python process. Receives no learner transcript or curriculum."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    memory = Memory(Path(root), Path(database))
    worker = ReplayWorker(Path(replay)) if replay else CodexWorker(output / "workers", 3, timeout)
    def event(kind, payload):
        memory.registry.events.append(stream_id=f"learning:{run_id}", actor="fresh_task", event_type=kind, payload=payload)
    event("TASK_STARTED", {"task_id": task["task_id"], "fresh_process": True, "inputs": ["task", "bounded_registry_retrieval"]})
    packet = memory.retrieve(task["query"], domain=task.get("domain", "sales"), limit=task.get("limit", 8), max_chars=22000)
    dump(output / "context.json", packet)
    event("RETRIEVAL_EXECUTED", {"task_id": task["task_id"], "query": task["query"], "memory_ids": [m["object_id"] for m in packet], "context_chars": len(json.dumps(packet, ensure_ascii=False))})
    baseline = worker.ask("baseline", APPLY_INSTRUCTION, {"task": task["task"], "memory_packet": []}, APPLICATION)
    application = worker.ask("application", APPLY_INSTRUCTION, {"task": task["task"], "memory_packet": packet}, APPLICATION)
    dump(output / "baseline.json", baseline)
    dump(output / "application.json", application)
    (output / "answer.md").write_text(application["output"], encoding="utf-8")
    used, available = set(application["used_memory_ids"]), {m["object_id"] for m in packet}
    checks = {"retrieved": bool(packet), "used": bool(used), "used_ids_in_packet": used <= available,
              "inline_citations": all(identity in application["output"] for identity in used),
              "substantive_output": len(application["output"]) >= 500,
              "real_sales_outcome_observed": False}
    checks["passed"] = all(checks[k] for k in ("retrieved", "used", "used_ids_in_packet", "inline_citations", "substantive_output"))
    outcome = {"kind": "local_retrieval_application_smoke_test", "checks": checks,
               "result": "deliverable_generated" if checks["passed"] else "application_failed",
               "limitation": "Structural checks do not prove business improvement; independent review is required for semantic transfer."}
    for identity in sorted(used & available):
        event("MEMORY_USED", {"task_id": task["task_id"], "object_id": identity})
    reflection = worker.ask("reflection", REFLECT_INSTRUCTION, {"task": task["task"], "application": application, "outcome": outcome}, REFLECTION)
    dump(output / "reflection.json", reflection)
    # The same serialized admission boundary holds proposals as evidence, not direct knowledge revisions.
    for proposal in reflection["proposals"]:
        event("CONSOLIDATION_PROPOSAL_HELD", {**proposal, "action": "HOLD", "reason": "Single local episode; corroboration and candidate evidence required"})
    experience = memory.record_experience(task["task_id"], task["task"], application["output"], sorted(used & available), outcome, reflection, run_id, domain=task.get("domain", "sales"))
    dump(output / "experience.json", experience)
    event("EXPERIENCE_CREATED", {"task_id": task["task_id"], "experience": experience})
    result = {"task_id": task["task_id"], "retrieval_results": len(packet), "used_memory_ids": sorted(used),
              "checks": checks, "model_calls": worker.calls, "usage": worker.usage, "experience_id": experience.get("object_id", experience.get("id"))}
    dump(output / "result.json", result)
    memory.close()
    return result


def main():
    parser = argparse.ArgumentParser(description="Jervis bounded learning runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--goal", required=True)
    run.add_argument("--run-dir", required=True)
    run.add_argument("--task")
    run.add_argument("--replay")
    run.add_argument("--reuse-learning", help="Reuse captured sources and learner outputs; run new consolidation and application")
    task = commands.add_parser("task")
    task.add_argument("--task", required=True)
    task.add_argument("--output", required=True)
    task.add_argument("--run-id", required=True)
    task.add_argument("--timeout", type=int, default=240)
    task.add_argument("--replay")
    status = commands.add_parser("status")
    status.add_argument("--run-dir", required=True)
    retrieve = commands.add_parser("retrieve")
    retrieve.add_argument("--query", required=True)
    retrieve.add_argument("--domain", default="sales")
    retrieve.add_argument("--mode", choices=["eval", "production", "audit"], default="eval")
    for command in (run, task, retrieve):
        command.add_argument("--root", default=str(Path(__file__).resolve().parents[3]))
        command.add_argument("--database", default="registry/jervis.sqlite")
    args = parser.parse_args()
    if args.command == "status":
        print((Path(args.run_dir) / "state.json").read_text(encoding="utf-8"))
        return
    if args.command == "run":
        if args.replay and args.reuse_learning:
            parser.error("Choose replay or reuse-learning, not both")
        runtime = Runtime(args.root, args.database, args.run_dir, json.loads(Path(args.goal).read_text(encoding="utf-8")), replay=args.replay, reuse_learning=args.reuse_learning)
        state = runtime.execute(args.task)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        runtime.memory.close()
        if state["status"] != "STOP":
            raise SystemExit(1)
    elif args.command == "task":
        print(json.dumps(fresh_task(args.root, args.database, args.task, args.output, args.run_id, args.timeout, args.replay)))
    else:
        memory = Memory(Path(args.root), Path(args.database))
        print(json.dumps(memory.retrieve(args.query, domain=args.domain, mode=args.mode), ensure_ascii=False, indent=2))
        memory.close()


if __name__ == "__main__":
    main()
