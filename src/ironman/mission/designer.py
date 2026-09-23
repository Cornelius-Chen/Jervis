"""Read-only, task-scoped access to the existing Designer advisory corpus.

The caller model chooses scope IDs; this adapter performs no aesthetic scoring.
Frozen Registry candidates remain evaluation evidence, separate from guidance.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


DESIGNER_ROOT = Path("D:/Creativity/Designer")
_REGISTRIES = "CCOS/registries/"
_DIMENSIONS = {
    "task_topologies": "task.", "style_families": "style.",
    "page_archetypes": "page.", "capability_axes": "capability.",
}

# Domain-local interpretation of the user's current Reality / Scope principles.
# Original policies and evidence remain verbatim at their source paths.
ALIGNMENT = {
    'version': 'designer-entry-v2',
    'quality': 'Human goals, audience, intent and constraints define the task. Actual human/environment results calibrate scoped hypotheses; model opinions and expert methods cannot certify themselves.',
    'representations': ['language', 'numeric', 'relation', 'operation', 'lawful_visual_example'],
    'historical_metrics': 'Old fixed scores, reconstruction similarity, champions and popularity are historical proxies, not universal quality truth or proof of learning. No parameter training occurs.',
    'permissions': 'Preserve source access/retrieval restrictions and canonical approval. This interpretation grants no source permission.',
    'feedback': 'Bind feedback to work version and capability/task scope. Missing original formation history stays missing. No automatic global preference promotion.',
}


def entrance(root):
    """Thin provenance entry; selected deep assets are loaded only after scope."""
    route_path=root/(_REGISTRIES+'design_learning_skill_router.yaml')
    route=None
    if route_path.is_file():
        router=yaml.safe_load(route_path.read_text(encoding='utf-8'))
        route=next((r for r in router['routes'] if r['id']=='route.capability_style_scoped_generation.v1'),None)
    return {'entry_version':ALIGNMENT['version'],'route_id':route['id'] if route else None,
            'route_status':route.get('status') if route else 'source_route_unavailable',
            'source_refs':[str(root/p) for p in ['AGENTS.md','CCOS/START_HERE.md','CCOS/CONTROL_INDEX.yaml',
                _REGISTRIES+'design_learning_skill_router.yaml',
                'project_adapters/all_projects/design_guidance_call_contract_v1.md']],
            'alignment':ALIGNMENT,'authority':'advisory; caller owns project implementation'}


def _local(root: Path, reference: str) -> Path | None:
    """External source metadata must not grant access outside its own corpus."""
    path = (root / reference).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def _yaml(root: Path, name: str) -> dict:
    return yaml.safe_load((root / (_REGISTRIES + name)).read_text(encoding="utf-8"))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def discover(memory, brief: str, needs: list[str]) -> dict:
    """Return scope choices first, then up to four matching advisory capabilities.

    Put exact ``task.*``, ``style.*``, ``page.*`` and ``capability.*`` IDs in
    ``needs`` after selecting from the returned taxonomy. Multiple IDs are
    supported for a multi-page project. ``ready`` means guidance was found,
    never that perception, implementation, or human preference is validated.
    """
    root = DESIGNER_ROOT
    packet = {
        "domain": "designer", "authority": "advisory", "ready": False,
        "status": "needs_scope", "capabilities": [], "negatives": [],
        "perception": {"available": False, "images": [], "restricted_sources": [],
            "notice": "Original evidence is inspection-only, not a reusable asset. Availability is not visual inspection or mastery."},
        "missing_evidence": ["Current project human preference has not been supplied to this adapter."],
        "time_weight": 0,
    }
    packet["registry_candidates"] = memory.retrieve(
        brief + " " + " ".join(needs), limit=3, domain="designer", mode="eval", max_chars=4000)
    packet["registry_notice"] = "Frozen Registry retrieval is candidate/evaluation evidence; it does not authorize positive design guidance."
    if not root.is_dir():
        packet.update(status="designer_unavailable", taxonomy={})
        packet["missing_evidence"].append("Original Designer corpus is unavailable.")
        return packet

    packet['entrance']=entrance(root)

    pages = _yaml(root, "page_archetype_registry.yaml")
    catalog = {
        "task_topologies": pages["task_topologies"],
        "style_families": _yaml(root, "style_family_registry.yaml")["families"],
        "page_archetypes": pages["page_archetypes"],
        "capability_axes": _yaml(root, "design_capability_axis_registry.yaml")["axes"],
    }
    packet["taxonomy"] = {
        dimension: [{"id": item["id"], "purpose": item.get("responsibility") or
                    item.get("experience_goal") or item.get("goal") or item.get("label")}
                    for item in items]
        for dimension, items in catalog.items()
    }
    identifiers = set(re.findall(r"(?:task|style|page|capability)\.[a-z_]+\.v\d+", " ".join(needs)))
    scope = {dimension: [item["id"] for item in items if item["id"] in identifiers]
             for dimension, items in catalog.items()}
    packet["scope"] = scope
    packet["selection_instruction"] = (
        "Select task topology, compare style alternatives, select page archetype, then capability axes. "
        "Call discover again with the exact selected IDs in needs. Evaluate fit to the actual brief; "
        "lexical retrieval is not applicability proof. Explain chosen guidance and rejected alternatives in project state.")
    packet["source_refs"] = [str(root / (_REGISTRIES + name)) for name in (
        "active_design_knowledge_index.yaml", "negative_design_knowledge_index.yaml",
        "page_archetype_registry.yaml", "style_family_registry.yaml", "design_capability_axis_registry.yaml")]
    if not all(scope.values()):
        return packet
    packet["taxonomy"] = {dimension: [entry for entry in entries if entry["id"] in scope[dimension]]
                          for dimension, entries in packet["taxonomy"].items()}

    index = _yaml(root, "active_design_knowledge_index.yaml")
    active = index["items"]
    intake = {item["id"]: item for item in _yaml(root, "learning_intake_registry.yaml")["entries"]}
    selected = []
    for item in active:
        if item.get("lifecycle_state") not in {"Accepted", "Canonical"}:
            continue
        if item.get("retrieval_authority") not in {"advisory", "authoritative"}:
            continue
        if not all(set(scope[dimension]) & set(item.get(dimension, [])) or
                   (dimension == "style_families" and "style_agnostic" in item.get(dimension, []))
                   for dimension in _DIMENSIONS):
            continue
        source_id = item.get("source_learning_record") or item["source_record_or_asset"]
        record = intake.get(source_id)
        # The current source lifecycle outranks a possibly stale advisory index.
        if record and (record.get("state") not in {"Accepted", "Canonical"} or
                       record.get("retrieval_allowed") not in {"advisory", "authoritative", True}):
            continue
        selected.append((item, record))

    # A stable enumeration is not a quality ranking. Preserve source-family
    # variety in the bounded packet; the caller compares actual applicability.
    ordered = []
    families = set()
    for pair in selected:
        if pair[0]["source_family"] not in families:
            ordered.append(pair)
            families.add(pair[0]["source_family"])
    ordered.extend(pair for pair in selected if pair not in ordered)
    for item, record in ordered[:4]:
        reference = item["source_record_or_asset"]
        path = _local(root, reference)
        text = ""
        refs = [str(root / (_REGISTRIES + "active_design_knowledge_index.yaml")) + "#" + item["id"]]
        if path and path.is_file() and path.suffix.lower() in {".md", ".yaml", ".txt"}:
            text = path.read_text(encoding="utf-8")
            refs.append(str(path))
        elif record:
            text = json.dumps({key: record[key] for key in (
                "source_name", "accepted_scope", "bias_flags", "do_not_copy") if key in record}, ensure_ascii=False)
        if record:
            refs.append(str(root / (_REGISTRIES + "learning_intake_registry.yaml")) + "#" + record["id"])
            if record.get("source_path_or_url"):
                locator = record["source_path_or_url"]
                local_source = _local(root, locator)
                refs.append(str(local_source) if local_source and local_source.is_file() else locator)
            # Some accepted abstractions point to original frame manifests.
            # Inspect just their availability metadata, never promote the raw
            # quarantined frames into generation guidance.
            for reference in record.get("audit_refs", [])[:6]:
                report = _local(root, reference)
                if not report or report.suffix != ".json" or not report.is_file() or report.stat().st_size > 262144:
                    continue
                evidence = json.loads(report.read_text(encoding="utf-8"))
                if evidence.get("retrieval_allowed") is False:
                    images = [value for value in _strings(evidence) if value.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                    existing = sum(1 for value in images if (image := _local(root, value)) and image.is_file())
                    packet["perception"]["restricted_sources"].append({
                        "source_id": record["id"], "report_ref": str(report),
                        "lifecycle_state": evidence.get("state"), "retrieval_allowed": False,
                        "linked_local_image_count": existing,
                        "notice": "Original source evidence exists but is excluded from positive guidance by its source permission.",
                    })
            for value in _strings(record):
                if len(packet["perception"]["images"]) >= 4:
                    break
                if value.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    image = _local(root, value)
                    if image and image.is_file():
                        packet["perception"]["images"].append({"path": str(image), "source_id": record["id"], "use": "inspection_only"})
        packet["capabilities"].append({
            **{k:v for k,v in item.items() if k not in {'evidence_strength','competition_role'}},
            "historical_assessment":{k:item[k] for k in ('evidence_strength','competition_role') if k in item},
            "guidance": text, "source_refs": refs,
            "applies_when": {dimension: item.get(dimension, []) for dimension in _DIMENSIONS},
            "applicability": "Scope metadata matches; runtime must assess project constraints and observed output.",
            "guidance_available": bool(text),
            "formation_history": 'Original source and audit refs preserved; a complete formation trajectory is not asserted.',
        })

    packet['extensions']=[]
    for item in index.get('local_advisory_extensions_pending_intake_linkage',{}).get('items',[]):
        if not (set(item['axes']) & set(scope['capability_axes']) and set(item['pages']) & set(scope['page_archetypes'])):
            continue
        path=_local(root,item['asset']);audit=_local(root,item['audit_ref'])
        if not path or not audit or not path.is_file() or not audit.is_file():
            continue
        packet['extensions'].append({**item,'guidance':path.read_text(encoding='utf-8'),
            'authority':'original explicitly indexed advisory extension; intake linkage remains pending',
            'source_refs':[str(path),str(audit)]})
    compiler=root/'prompt_library/templates/capability_style_prompt_compiler_v1.md'
    packet['compiler']={'source_ref':str(compiler),'available':compiler.is_file(),
        'original_advisory_text':compiler.read_text(encoding='utf-8') if compiler.is_file() else None,
        'use':'Task topology, style alternatives, page archetype, selected capabilities, state consequences and actual checks. Load only necessary capabilities; old fixed axis counts and score thresholds are advisory heuristics.',
        'required_reason_summary':['selected scope','rejected alternatives','concrete operations','source concentration','missing evidence']}

    tokens = set(re.findall(r"[a-z]+", " ".join(needs).lower()))
    for item in _yaml(root, "negative_design_knowledge_index.yaml")["negative_items"]:
        if tokens & set(re.findall(r"[a-z]+", " ".join(item["failure_scope"]))) or "retrieval" in item["failure_scope"]:
            packet["negatives"].append(item)
    packet["perception"]["available"] = bool(packet["perception"]["images"])
    if not packet["perception"]["available"]:
        packet["missing_evidence"].append("No directly linked local original image is available for the selected sources; text guidance does not establish visual perception.")
    packet["source_concentration"] = {
        "eligible_families": sorted(families),
        "returned_families": sorted({item["source_family"] for item in packet["capabilities"]}),
        "notice": "Enumeration is not aesthetic ranking. Explain any single-family dependence; no recency or popularity bonus is applied.",
    }
    packet["ready"] = any(item["guidance_available"] for item in packet["capabilities"]) or bool(packet['extensions'])
    packet["status"] = "advisory_ready" if packet["ready"] else "no_applicable_guidance"
    return packet
