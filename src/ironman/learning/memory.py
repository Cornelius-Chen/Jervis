"""Bounded learning memory on the existing immutable IRONMAN registry.

The FTS table is disposable. Registry versions and their hash-bound attachments
are the source of truth. Semantic consolidation decisions come from the caller;
this module only recognizes exact content duplicates itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ironman.contracts import ContractLoader
from ironman.lifecycle import LifecycleValidator
from ironman.storage import ObjectNotFoundError, Registry, VersionConflictError


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unique(values: list) -> list:
    return list({_json(value): value for value in values}.values())


class Memory:
    """Single-writer registry adapter; new learning remains candidate/eval-only.

    ``related`` and ``retrieve`` return dictionaries with object_id/id, version,
    memory_type, lifecycle_state, domain, title, body and provenance_refs.
    ``consolidate`` accepts {action, target_id?, reason, merged_candidate?}.
    No lifecycle promotion is performed, including for SUPERSEDES proposals.
    """

    def __init__(self, root: str | Path, database: str | Path, *, rebuild: bool = True):
        self.root = Path(root).resolve()
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.attachments = self.database.parent / (self.database.stem + "_content")
        self.attachments.mkdir(exist_ok=True)
        self.contracts = ContractLoader(self.root / "schemas/ironman.schema.yaml")
        self.registry = Registry(self.database, self.contracts, LifecycleValidator.from_root(self.root))
        self._index = sqlite3.connect(self.database)
        self._index.row_factory = sqlite3.Row
        self._index.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS learning_fts USING fts5(
            object_id UNINDEXED, version UNINDEXED, domain UNINDEXED,
            lifecycle UNINDEXED, memory_type UNINDEXED, text)""")
        self._index.execute("""CREATE TABLE IF NOT EXISTS learning_relations (
            source_id TEXT, target_id TEXT, relation_type TEXT)""")
        self._index.execute("CREATE INDEX IF NOT EXISTS learning_relations_target ON learning_relations(target_id)")
        if rebuild:
            self.rebuild_index()
        else:
            self._index.commit()

    def close(self) -> None:
        self._index.close()
        self.registry.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _file(self, ref: str) -> Path:
        path = Path(ref)
        return path if path.is_absolute() else self.root / path

    def _attachment(self, body: dict) -> tuple[str, str]:
        data = _json(body).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.attachments / (digest + ".json")
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("immutable attachment content mismatch")
        else:
            with path.open("xb") as stream:
                stream.write(data)
        return str(path), digest

    def _payload(self, object_id: str) -> dict:
        obj = self.registry.get_object(object_id)
        return json.loads(self._index.execute(
            "SELECT payload_json FROM object_versions WHERE object_id=? AND version=?",
            (object_id, obj.current_version),
        ).fetchone()[0])

    def _body(self, payload: dict) -> dict:
        if "learning_runtime_v1" not in payload.get("tags", []):
            return payload
        path = self._file(payload["content_ref"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != payload["content_hash"]:
            raise ValueError(f"attachment integrity failure: {payload['object_id']}")
        return json.loads(data)

    def _index_payload(self, payload: dict) -> None:
        self._index.execute("DELETE FROM learning_fts WHERE object_id=?", (payload["object_id"],))
        self._index.execute("DELETE FROM learning_relations WHERE source_id=?", (payload["object_id"],))
        kind = {"WorldClaim": "knowledge", "Capability": "skill", "Trace": "experience"}.get(payload["object_type"])
        if kind:
            body = self._body(payload)
            self._index.execute("INSERT INTO learning_fts VALUES (?,?,?,?,?,?)", (
                payload["object_id"], payload["version"], payload["semantic_owner"],
                payload["lifecycle_state"], kind,
                payload.get("human_name", "") + " " + _json(body),
            ))
            for relation in body.get("relations", []):
                self._index.execute("INSERT INTO learning_relations VALUES (?,?,?)",
                    (payload["object_id"], relation["target_id"], relation["type"]))
        self._index.commit()

    def rebuild_index(self) -> None:
        """Rebuild search from canonical current versions, never from old FTS rows."""
        rows = self._index.execute("""SELECT v.payload_json FROM object_versions v
            JOIN objects o ON o.object_id=v.object_id AND o.current_version=v.version""").fetchall()
        self._index.execute("DELETE FROM learning_fts")
        self._index.execute("DELETE FROM learning_relations")
        self._index.commit()
        for row in rows:
            self._index_payload(json.loads(row[0]))

    def _put(self, payload: dict, expected: str | None = None) -> dict:
        self.registry.put_new_version(payload, expected_current_version=expected)
        self._index_payload(payload)
        return payload

    def register_source(self, source: dict) -> dict:
        """Register a canonical SourceEvidence; processed content stays inspectable."""
        if source.get("object_type") != "SourceEvidence":
            raise ValueError("register_source requires canonical SourceEvidence")
        self.contracts.validate_instance("SourceEvidence", source)
        raw = self._file(source["raw_content_ref"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["content_hash"].removeprefix("sha256:"):
            raise ValueError("source raw content hash mismatch")
        processed = self._file(source["content_ref"]).read_bytes()
        processed.decode("utf-8")
        processed_hash = hashlib.sha256(processed).hexdigest()
        try:
            current = self._payload(source["object_id"])
        except ObjectNotFoundError:
            self._put(deepcopy(source))
            self.registry.events.append(stream_id=f"source-content:{source['object_id']}",
                actor="learning_runtime_v1", event_type="memory.source_processed_registered",
                payload={"source_id": source["object_id"], "version": source["version"],
                         "processed_hash": processed_hash, "content_ref": source["content_ref"]})
            return source
        if current["content_hash"] != source["content_hash"] or current["source_locator"] != source["source_locator"]:
            raise VersionConflictError("source identity already registered with different content")
        self._source_text(current)
        existing_processed = self._file(current["content_ref"]).read_bytes()
        if existing_processed != processed:
            raise VersionConflictError("source processed content differs for immutable identity")
        return current

    def _source_text(self, source: dict) -> str:
        events = self.registry.events.read_stream(f"source-content:{source['object_id']}")
        if not events:
            raise ValueError("source processed content was not registered")
        data = self._file(source["content_ref"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != events[-1].payload["processed_hash"]:
            raise ValueError("source processed content hash mismatch")
        # Match Path.read_text's universal newlines used by the learner, while
        # validating immutable original bytes above. Wording remains exact.
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    def import_designer(self, projection_path: str | Path) -> dict:
        """Copy frozen canonical P1 payloads unchanged; wrap Relation for Registry."""
        projection = Path(projection_path)
        if projection.is_dir():
            projection = projection / "objects.jsonl"
        counts = {"objects": 0, "relations": 0}
        for line in projection.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            try:
                existing = self._payload(payload["object_id"])
            except ObjectNotFoundError:
                self._put(payload)
            else:
                if existing != payload:
                    raise VersionConflictError("Designer frozen projection differs from registered version")
            counts["objects"] += 1
        for line in (projection.parent / "relations.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            relation = json.loads(line)
            self.contracts.validate_instance("Relation", relation)
            ref, digest = self._attachment(relation)
            payload = self._envelope(relation["edge_id"], "ObjectEnvelope", "designer", "candidate", relation["provenance_refs"])
            payload.update(created_at=relation["created_at"], created_by=relation["created_by"], content_ref=ref, content_hash=digest)
            try:
                old = self._payload(payload["object_id"])
            except ObjectNotFoundError:
                self._put(payload)
            else:
                if old != payload:
                    raise VersionConflictError("Designer frozen relation changed")
            counts["relations"] += 1
        return counts

    def related(self, query: str, limit: int = 6, domain: str = "sales", mode: str = "eval") -> list[dict]:
        modes = {"eval": ("candidate", "accepted", "stable", "canonical"),
                 "production": ("stable", "canonical"),
                 "audit": ("raw", "quarantine", "candidate", "accepted", "stable", "canonical")}
        if mode not in modes:
            raise ValueError("mode must be eval, production or audit")
        tokens = list(dict.fromkeys(re.findall(r"\w+", query.lower())))[:64]
        if not tokens or limit <= 0:
            return []
        match = " OR ".join('"' + token + '"' for token in tokens)
        states = modes[mode]
        rows = self._index.execute(
            "SELECT object_id FROM learning_fts WHERE learning_fts MATCH ? AND domain=? "
            f"AND lifecycle IN ({','.join('?' for _ in states)}) ORDER BY bm25(learning_fts), object_id LIMIT ?",
            (match, domain, *states, limit),
        ).fetchall()
        result = []
        for row in rows:
            payload = self._payload(row[0])
            warnings = []
            links = self._index.execute("""SELECT r.source_id, r.relation_type FROM learning_relations r
                JOIN objects o ON o.object_id=r.source_id WHERE r.target_id=?
                AND r.relation_type IN ('contradicts','fails_when','supersedes')
                AND o.lifecycle_state NOT IN ('rejected','retired') LIMIT 6""", (payload["object_id"],)).fetchall()
            for link in links:
                opposing = self._payload(link["source_id"])
                body = self._body(opposing)
                warnings.append({"object_id": opposing["object_id"], "version": opposing["version"],
                    "relation_type": link["relation_type"], "lifecycle_state": opposing["lifecycle_state"],
                    "statement": body.get("statement", ""), "boundaries": body.get("boundaries", []),
                    "evidence": body.get("evidence", []),
                    "notice": "Unresolved evidence; applicability may be narrower. No resolution or promotion inferred."})
            result.append({"object_id": payload["object_id"], "id": payload["object_id"],
                "version": payload["version"], "memory_type": {"WorldClaim": "knowledge", "Capability": "skill", "Trace": "experience"}[payload["object_type"]],
                "lifecycle_state": payload["lifecycle_state"], "domain": payload["semantic_owner"],
                "title": payload.get("human_name", ""), "body": self._body(payload),
                "provenance_refs": payload["provenance_refs"], "warnings": warnings})
        return result

    def retrieve(self, query: str, limit: int = 6, domain: str = "sales", mode: str = "eval", max_chars: int = 18000) -> list[dict]:
        result: list[dict] = []
        for item in self.related(query, limit, domain, mode):
            if len(_json(result + [item])) <= max_chars:
                result.append(item)
        return result

    def _envelope(self, object_id: str, kind: str, domain: str, state: str, provenance: list[str]) -> dict:
        return {"object_id": object_id, "object_type": kind, "version": "0.1.0",
            "semantic_owner": domain, "lifecycle_state": state, "created_at": _now(),
            "created_by": "learning_runtime_v1", "provenance_refs": list(dict.fromkeys(provenance)),
            "tags": ["learning_runtime_v1"], "visibility": "private"}

    def _validate_evidence(self, candidate: dict) -> None:
        if not candidate.get("evidence"):
            raise ValueError("candidate requires source quote evidence")
        for evidence in candidate["evidence"]:
            source = self._payload(evidence["source_id"])
            if source["object_type"] != "SourceEvidence":
                raise ValueError("evidence must reference registered SourceEvidence")
            text = self._source_text(source)
            quote = evidence["quote"]
            if not isinstance(quote, str) or not quote.strip() or quote not in text:
                raise ValueError("evidence quote not found in registered processed source")
        if candidate["source_id"] not in [item["source_id"] for item in candidate["evidence"]]:
            raise ValueError("candidate source_id must have evidence")

    @staticmethod
    def _semantic_content(candidate: dict) -> dict:
        return {key: candidate.get(key) for key in ("memory_type", "statement", "mechanism", "conditions", "boundaries", "steps", "failure_modes", "examples", "counterexamples")}

    def consolidate(self, candidate: dict, decision: dict | None = None) -> dict:
        candidate = deepcopy(candidate)
        self._validate_evidence(candidate)
        if candidate["memory_type"] not in {"knowledge", "skill"}:
            raise ValueError("candidate memory_type must be knowledge or skill")
        domain = candidate.get("domain", "sales")
        related = self.related(candidate["title"] + " " + candidate["statement"], 20, domain, "audit")
        exact = next((item for item in related if self._semantic_content(item["body"]) == self._semantic_content(candidate)), None)
        if exact:
            decision = {"action": "DUPLICATE", "target_id": exact["object_id"], "reason": "Exact structured content duplicate; retain evidence."}
        elif decision is None:
            raise ValueError("semantic consolidation requires an explicit decision")
        decision = deepcopy(decision)
        action = decision["action"].upper()
        if action not in {"NEW", "DUPLICATE", "EXTENSION", "CONFLICT", "COUNTEREXAMPLE", "SUPERSEDES", "REJECT"}:
            raise ValueError("unknown consolidation action")
        target_id = decision.get("target_id")
        target = self._payload(target_id) if target_id else None
        if action not in {"NEW", "REJECT"} and target is None:
            raise ValueError(f"{action} requires target_id")
        if target and (target["semantic_owner"] != domain or target["object_type"] not in {"WorldClaim", "Capability"}):
            raise ValueError("consolidation target must be same-domain knowledge or skill")
        expected = None
        body = deepcopy(decision.get("merged_candidate") or candidate)
        if target and action in {"DUPLICATE", "EXTENSION"}:
            if "learning_runtime_v1" not in target.get("tags", []):
                raise ValueError("legacy memory requires explicit structured adaptation before revision")
            if target["lifecycle_state"] != "candidate":
                raise ValueError("cannot revise non-candidate memory through learning consolidation")
            if target["object_type"] != {"knowledge": "WorldClaim", "skill": "Capability"}[candidate["memory_type"]]:
                raise ValueError("cannot merge different memory types")
            previous = self._body(target)
            if action == "DUPLICATE":
                body = deepcopy(previous)
            elif not decision.get("merged_candidate"):
                for key in ("statement", "mechanism"):
                    if previous.get(key) and previous[key] != body.get(key):
                        body[key] = previous[key] + "\nExtension: " + body.get(key, "")
                for key in ("conditions", "boundaries", "steps", "failure_modes", "examples", "counterexamples"):
                    body[key] = _unique(previous.get(key, []) + body.get(key, []))
            body["evidence"] = _unique(previous.get("evidence", []) + candidate["evidence"] + body.get("evidence", []))
            body["relations"] = _unique(previous.get("relations", []) + candidate.get("relations", []) + body.get("relations", []))
            object_id = target_id
            expected = target["version"]
        else:
            content_id = hashlib.sha256(_json(self._semantic_content(candidate)).encode("utf-8")).hexdigest()[:16]
            object_id = "memory:" + domain + ":" + candidate["candidate_id"] + ":" + content_id
        self._validate_evidence(body)
        body["consolidation"] = {"action": action, "target_id": target_id, "reason": decision.get("reason", ""), "candidate_id": candidate["candidate_id"]}
        prior_candidates = self._body(target).get("source_candidate_ids", []) if expected else []
        body["source_candidate_ids"] = list(dict.fromkeys(prior_candidates + body.get("source_candidate_ids", []) + [candidate["candidate_id"]]))
        relation_type = {"CONFLICT": "contradicts", "COUNTEREXAMPLE": "fails_when", "SUPERSEDES": "supersedes", "EXTENSION": "generalizes"}.get(action)
        if relation_type and target_id and target_id != object_id:
            body.setdefault("relations", []).append({"type": relation_type, "target_id": target_id})
        evidence_ids = list(dict.fromkeys(item["source_id"] for item in body["evidence"]))
        for relation in body.get("relations", []):
            self.registry.get_object(relation["target_id"])
            self.contracts.validate_instance("Relation", {"edge_id": "relation:" + uuid.uuid4().hex,
                "source_id": object_id, "target_id": relation["target_id"], "relation_type": relation["type"],
                "created_at": _now(), "created_by": "learning_runtime_v1", "provenance_refs": evidence_ids})
        state = "quarantine" if action in {"CONFLICT", "COUNTEREXAMPLE", "SUPERSEDES", "REJECT"} else "candidate"
        kind = "WorldClaim" if body["memory_type"] == "knowledge" else "Capability"
        payload = self._envelope(object_id, kind, domain, state, evidence_ids)
        payload.update(human_name=body["title"], status_reason=decision.get("reason", action))
        if expected:
            parts = expected.split(".")
            payload.update(version=f"{parts[0]}.{int(parts[1]) + 1}.0", created_at=target["created_at"], updated_at=_now())
            payload["provenance_refs"] = list(dict.fromkeys(target["provenance_refs"] + evidence_ids))
        if kind == "WorldClaim":
            payload.update(claim_id=object_id, claim_type="interpretation", statement=body["statement"],
                scope=body["module_id"], observed_at=_now(), confidence=body["confidence"], freshness_status="current",
                evidence_for=evidence_ids, evidence_against=[target_id] if action == "CONFLICT" else [])
        else:
            payload.update(job_to_be_done=body["statement"], task_families=[body["module_id"]],
                applies_when=[{"field": "context", "operator": "custom", "value": value} for value in body["conditions"]],
                fails_when=[{"field": "context", "operator": "custom", "value": value} for value in body["boundaries"]],
                input_contract="Task context matching applicability conditions.",
                output_contract="Execute the attached procedure for evaluation; no production authority.",
                eval_suite_refs=body.get("eval_suite_refs", ["tests/learning/test_runtime.py"]),
                known_failure_modes=body["failure_modes"], permission_scope=["candidate_eval_or_shadow_only"])
        payload["content_ref"], payload["content_hash"] = self._attachment(body)
        self._put(payload, expected)
        return {"action": action, "object_id": object_id, "version": payload["version"],
            "lifecycle_state": state, "target_id": target_id, "reason": decision.get("reason", "")}

    def record_experience(self, task_id: str, task: Any, output: Any, used_ids: list[str], outcome: Any, reflection: Any, run_id: str, domain: str = "sales") -> dict:
        versions = [self.registry.get_object(object_id) for object_id in used_ids]
        object_id = "experience:" + uuid.uuid4().hex
        body = {"memory_type": "experience", "task_id": task_id, "task": task, "output": output,
            "used_ids": used_ids, "used_versions": [f"{obj.object_id}@{obj.current_version}" for obj in versions],
            "outcome": outcome, "reflection": reflection, "run_id": run_id,
            "interpretation": "One experimental episode; not universal truth or capability promotion."}
        payload = self._envelope(object_id, "Trace", domain, "candidate", body["used_versions"])
        ref, digest = self._attachment(body)
        timestamp = _now()
        failed = isinstance(outcome, dict) and (outcome.get("checks", {}).get("passed") is False
            or outcome.get("status") in {"failure", "failed", "FAILED"})
        payload.update(human_name=f"Experience: {task_id}", content_ref=ref, content_hash=digest,
            trace_id=object_id, mission_id=run_id, task_id=task_id, actor="learning_runtime_v1",
            capability_versions=body["used_versions"], started_at=timestamp, ended_at=timestamp,
            events=[{"event_id": uuid.uuid4().hex, "event_type": "experience.recorded", "occurred_at": timestamp,
                "actor": "learning_runtime_v1", "payload_ref": ref, "payload_hash": digest}],
            final_status="failure" if failed else "success", status_reason="Episode recorded; outcome assessment is in attached evidence.")
        self._put(payload)
        return {"object_id": object_id, "version": payload["version"], "lifecycle_state": "candidate"}
