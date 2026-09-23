"""Bounded discovery and acquisition, extending the existing Sales triage.

Catalog links and supplied research seeds are metadata candidates. Only acquired
full bodies become SourceEvidence. The frozen queue never expands during reading.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bs4 import BeautifulSoup


TRIAGE_PATH = Path(__file__).with_name("triage.py")
_spec = importlib.util.spec_from_file_location("jervis_sales_triage", TRIAGE_PATH)
triage = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = triage
_spec.loader.exec_module(triage)
Candidate = triage.Candidate

MAX_CONTENT_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_url(url: str) -> None:
    """External research cannot turn a discovered link into local service access."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only public HTTP(S) source URLs without credentials are allowed")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Source URL resolves to a non-public address")


class _PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str) -> tuple[bytes, str, str]:
    _public_url(url)
    request = Request(url, headers={"User-Agent": "JervisLearningResearch/1.0", "Accept": "text/html,application/pdf,text/plain"})
    with build_opener(_PublicRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read(MAX_CONTENT_BYTES + 1)
        if len(raw) > MAX_CONTENT_BYTES:
            raise ValueError(f"Source exceeds {MAX_CONTENT_BYTES} byte acquisition budget")
        return raw, response.headers.get_content_type(), response.url


def _module(title: str, goal: dict) -> str:
    lowered = title.lower()
    return max(goal["modules"], key=lambda module: sum(word.lower() in lowered for word in module["keywords"]))["module_id"]


def scout(goal: dict, run_dir: Path, emit: Callable[[str, dict], None]) -> list[dict]:
    """Discover once, dedupe with triage, rank for module/family diversity, freeze.

    Queue entries serialize triage.Candidate and add only context needed by the
    runtime: module_id, creator_or_org, provenance_refs, and domain. Old Candidate
    cards and their metadata-only authority flags are never rewritten.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "queue.json").exists():
        raise FileExistsError("Learning queue is already frozen; use recorded queue for replay")
    limit = goal["budget"]["max_discovered"]
    queue_size = goal["budget"]["max_sources"]
    found: list[Candidate] = []
    contexts: dict[str, dict] = {}
    known_urls: dict[str, Candidate] = {}

    def add(item: dict, channel: str, parent: str) -> None:
        if len(found) >= limit:
            return
        url = urldefrag(item["url"])[0]
        if url in known_urls:
            refs = contexts[known_urls[url].candidate_id]["provenance_refs"]
            if parent not in refs:
                refs.append(parent)
            return
        title = triage.clean_text(item["title"])
        direction = item.get("module_id") or _module(title, goal)
        candidate = Candidate(
            candidate_id=triage.stable_id("learning", url),
            source=item.get("organization", urlsplit(url).hostname),
            source_family=item.get("source_family", "practitioner_primary"),
            direction=direction, query=channel, title=title, url=url,
            description=item.get("description", ""), description_kind="scout_metadata",
            doi=triage.canonical_doi(item.get("doi", "")),
            license=item.get("license", ""),
        )
        found.append(candidate)
        known_urls[url] = candidate
        contexts[candidate.candidate_id] = {
            "module_id": direction, "domain": goal["domain"],
            "creator_or_org": item.get("organization", candidate.source),
            "provenance_refs": [parent, *item.get("provenance_refs", [])],
        }
        emit("RESOURCE_DISCOVERED", {"candidate_id": candidate.candidate_id, "title": title, "url": url, "source_family": candidate.source_family, "query": channel})

    # Catalogs are a real bounded discovery stage, not recursive citation chasing.
    for catalog in goal.get("scouts", {}).get("catalogs", []):
        if len(found) >= limit:
            break
        emit("SCOUT_STARTED", {"catalog": catalog["url"], "query": catalog["query"]})
        try:
            raw, _, final_url = _download(catalog["url"])
            soup = BeautifulSoup(raw, "html.parser")
            for anchor in soup.find_all("a", href=True):
                url = urldefrag(urljoin(final_url, anchor["href"]))[0]
                if urlsplit(url).hostname != urlsplit(final_url).hostname:
                    continue
                title = anchor.get_text(" ", strip=True)
                if title and re.search(catalog["include_pattern"], url + " " + title, re.I):
                    add({**catalog, "url": url, "title": title}, "catalog: " + catalog["query"], catalog["url"])
            emit("SCOUT_COMPLETED", {"catalog": catalog["url"], "discovered_total": len(found)})
        except Exception as exc:
            emit("SCOUT_FAILED", {"catalog": catalog["url"], "error": f"{type(exc).__name__}: {exc}"})
    for seed in goal.get("scouts", {}).get("resources", []):
        add(seed, "seed: " + seed.get("query", goal["title"]), "goal:" + goal["goal_id"])

    unique, dedupe_counts = triage.exact_and_near_dedupe(found)
    for candidate in unique:
        triage.score_candidate(candidate)
        text = (candidate.title + " " + candidate.description).lower()
        matches = sum(keyword.lower() in text for module in goal["modules"] for keyword in module["keywords"])
        # Broad Sales title heuristics exclude adjacent experimental research;
        # narrow module relevance replaces that gate for this explicit curriculum.
        candidate.quality_score = min(50, matches * 5) + min(20, candidate.quality_score * .25)
        candidate.score_components["curriculum_keyword_matches"] = matches
        candidate.evidence_eligible = matches > 0
    selected: list[Candidate] = []
    remaining = [candidate for candidate in unique if candidate.evidence_eligible]
    modules, families, organizations = Counter(), Counter(), Counter()
    while remaining and len(selected) < queue_size:
        # Coverage priorities are soft, so a narrow curriculum is not subject to
        # the historical broad-sales 20%-per-direction quota.
        candidate = max(remaining, key=lambda c: (
            c.quality_score + 20 / (1 + modules[c.direction]) + 25 / (1 + families[c.source_family]) + 30 / (1 + organizations[c.source]), c.candidate_id))
        selected.append(candidate)
        remaining.remove(candidate)
        modules[candidate.direction] += 1
        families[candidate.source_family] += 1
        organizations[candidate.source] += 1
    selected_ids = {candidate.candidate_id for candidate in selected}
    for candidate in found:
        if candidate.candidate_id in selected_ids:
            candidate.decision = "selected_for_full_content"
            candidate.decision_reason = "Curriculum relevance and complementary module/source coverage within the fixed source budget"
        else:
            candidate.decision = "rejected_for_this_run"
            candidate.decision_reason = (
                "Duplicate of " + (candidate.duplicate_of or candidate.near_duplicate_of)
                if candidate.duplicate_of or candidate.near_duplicate_of else
                "No curriculum keyword match" if not candidate.evidence_eligible else
                "Lower marginal curriculum/source coverage within the fixed source budget")
        emit("RESOURCE_SELECTED" if candidate.candidate_id in selected_ids else "RESOURCE_REJECTED", {
            "candidate_id": candidate.candidate_id, "reason": candidate.decision_reason,
            "score": candidate.quality_score, "module_id": candidate.direction,
        })
    queue = [{**asdict(candidate), **contexts[candidate.candidate_id]} for candidate in selected]
    triage.write_jsonl(run_dir / "candidates.jsonl", ({**asdict(c), **contexts[c.candidate_id]} for c in found))
    with (run_dir / "queue.json").open("x", encoding="utf-8") as handle:
        json.dump(queue, handle, ensure_ascii=False, indent=2)
    emit("QUEUE_FROZEN", {"discovered": len(found), "selected": len(queue), **dedupe_counts, "queue_ref": str((run_dir / "queue.json").resolve())})
    return queue


def acquire(resource: dict, source_dir: Path) -> dict:
    """Acquire full content and return a canonical SourceEvidence, or raise.

    Raw evidence is immutable. A failed read stays failed; it is not substituted
    with metadata, another URL, or a different queue resource.
    """
    raw, media_type, final_url = _download(resource["url"])
    digest = hashlib.sha256(raw).hexdigest()
    source_id = "source.learning." + resource["candidate_id"].rsplit(".", 1)[-1] + "." + digest[:16]
    directory = source_dir / source_id
    directory.mkdir(parents=True, exist_ok=False)
    pdf = media_type == "application/pdf" or raw.startswith(b"%PDF-")
    html = "html" in media_type or raw.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    suffix = ".pdf" if pdf else ".html" if html else ".txt"
    raw_path = directory / ("raw" + suffix)
    raw_path.write_bytes(raw)
    processed_path = directory / "fulltext.txt"
    if pdf:
        executable = shutil.which("pdftotext")
        if not executable:
            raise RuntimeError("PDF acquisition requires installed pdftotext")
        subprocess.run([executable, "-layout", "-enc", "UTF-8", str(raw_path), str(processed_path)], check=True, capture_output=True, timeout=45)
        text = processed_path.read_text(encoding="utf-8")
    elif html:
        soup = BeautifulSoup(raw, "html.parser")
        for element in soup(["script", "style", "nav", "header", "footer", "noscript", "svg", "form"]):
            element.decompose()
        body = soup.find("main") or soup.find("article") or soup.body or soup
        text = body.get_text("\n", strip=True)
    elif media_type.startswith("text/") or urlsplit(final_url).path.lower().endswith((".md", ".txt")):
        text = raw.decode("utf-8-sig")
    else:
        raise ValueError(f"Unsupported source media type: {media_type}")
    if len(text.strip()) < 500 or re.search(r"checking your browser|verify you are human|enable javascript and cookies to continue", text[:1500], re.I):
        raise ValueError("Source has no usable full body or returned an access challenge")
    processed_path.write_text(text, encoding="utf-8")
    now = _now()
    evidence = {
        "object_id": source_id, "object_type": "SourceEvidence", "human_name": resource["title"],
        "version": "0.1.0", "semantic_owner": resource.get("domain", "sales"), "lifecycle_state": "quarantine",
        "created_at": now, "created_by": "learning-runtime.acquire",
        "provenance_refs": [resource["candidate_id"], *resource.get("provenance_refs", [])],
        "source_type": "paper" if pdf else "webpage" if html else "other", "source_locator": final_url,
        "creator_or_org": resource.get("creator_or_org", resource["source"]), "captured_at": now,
        "usage_status": "review_required", "license": resource.get("license", ""),
        "discovery_reason": resource["decision_reason"], "discovery_channel": resource["query"],
        "raw_content_ref": str(raw_path.resolve()), "content_ref": str(processed_path.resolve()),
        "content_hash": digest, "tags": [resource["source_family"], resource["module_id"], "full_content_acquired"],
        "status_reason": "Publicly accessible research evidence; no production authority or redistribution license inferred",
    }
    triage.write_json(directory / "source.json", evidence)
    return evidence
