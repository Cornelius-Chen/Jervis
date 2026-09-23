#!/usr/bin/env python3
"""High-throughput, metadata-only learning candidate triage.

This prototype uses public, documented APIs. It does not scrape pages, fetch
media, copy full text, or make learning/mastery claims. Its sole decision is
whether a candidate deserves the next, more expensive evidence stage.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ALGORITHM_VERSION = "jervis-metadata-triage-v3.3"
USER_AGENT = f"JervisLearningTriage/{ALGORITHM_VERSION} metadata-only research"
CURRENT_YEAR = datetime.now(timezone.utc).year
# Calibrated to the deliberately de-weighted provenance/popularity scale below.
# A 30-point floor from v2 would reject a direct field experiment after those
# non-content signals were reduced; 24 keeps that case while the two-axis topic
# gate still blocks CRM/medical/traffic lexical collisions.
MIN_REVIEW_SCORE = 24.0
MIN_METADATA_CONFIDENCE = 0.62

SALES_QUERIES = [
    ("sales_conversation", "sales conversation"),
    ("adaptive_selling", "adaptive selling"),
    ("sales_listening", "sales listening customer"),
    ("objection_diagnosis", "sales objection diagnosis"),
    ("buyer_seller_negotiation", "buyer seller negotiation"),
    ("procurement_negotiation", "B2B procurement negotiation"),
    ("trust_in_selling", "salesperson trust customer"),
    ("question_asking", "sales questions conversation"),
    ("follow_up", "sales follow up communication"),
    ("ethical_selling", "ethical selling deceptive sales"),
    ("call_analysis", "sales call transcript analysis"),
    ("conversation_analysis", "conversation analysis telephone sales"),
    ("value_selling", "value selling business customer"),
    ("customer_resistance", "customer resistance salesperson"),
    ("sales_performance", "sales performance meta analysis"),
]

GITHUB_QUERIES = [
    ("sales_conversation_tools", '"sales conversation" in:name,description,readme stars:>=3'),
    ("sales_call_analysis_tools", '"sales call" analysis in:name,description,readme stars:>=3'),
]

S2_QUERIES = [
    ("sales_conversation", '"sales conversation"'),
    ("adaptive_selling", '"adaptive selling"'),
]

RELEVANCE_TERMS = {
    "sales": 5,
    "selling": 5,
    "salesperson": 5,
    "buyer": 4,
    "customer": 3,
    "procurement": 5,
    "negotiation": 5,
    "objection": 6,
    "conversation": 4,
    "dialogue": 4,
    "communication": 3,
    "listening": 5,
    "question": 3,
    "trust": 3,
    "persuasion": 3,
    "purchase": 3,
    "follow up": 4,
    "成交": 5,
    "销售": 5,
    "采购": 5,
    "谈判": 5,
    "异议": 6,
    "沟通": 3,
    "倾听": 5,
}

# These are deliberately narrower than RELEVANCE_TERMS.  A query result is not
# eligible for evidence-review budget merely because an index matched "sales"
# somewhere in hidden full text or attached a broad topic label.  The retained
# title must establish a professional selling/negotiation context, or combine a
# commercial actor/context with an interaction skill.
PROFESSIONAL_ANCHORS = {
    "salesperson": 6,
    "salespersons": 6,
    "salespeople": 6,
    "sales force": 6,
    "sales call": 6,
    "sales conversation": 6,
    "sales training": 6,
    "sales performance": 6,
    "sales strategy": 6,
    "sales profession": 6,
    "sales assistant": 6,
    "sales target": 5,
    "sales agent": 5,
    "sales listening": 6,
    "sales question": 6,
    "sales objection": 6,
    "sales resistance": 6,
    "sales communication": 6,
    "sales negotiation": 6,
    "sales ethics": 6,
    "selling effectiveness": 6,
    "selling": 5,
    "personal selling": 6,
    "direct selling": 6,
    "relationship selling": 6,
    "adaptive selling": 6,
    "buyer seller": 6,
    "business to business": 5,
    "b2b": 5,
    "procurement": 5,
    "销售员": 6,
    "销售人员": 6,
    "销售对话": 6,
    "销售通话": 6,
    "销售培训": 6,
    "销售绩效": 6,
    "顾问式销售": 6,
    "采购谈判": 6,
}

COMMERCIAL_CONTEXT_TERMS = {
    "seller": 3,
    "buyer": 3,
    "customer": 2,
    "supplier": 3,
    "vendor": 3,
    "procurement": 4,
    "purchase": 2,
    "purchasing": 3,
    "retail": 2,
    "business": 2,
    "b2b": 4,
    "客户": 2,
    "买方": 3,
    "供应商": 3,
    "采购": 4,
    "零售": 2,
}

# High-precision exclusions for recurring lexical collisions in this sales-
# interaction mission. They are not universal content bans; a future medical,
# finance, transport, or legal mission would use a different domain config.
NEGATIVE_DOMAIN_TERMS = {
    "kidney sale": 12,
    "organ sale": 12,
    "safe cycling": 10,
    "traffic sound": 10,
    "earnings surprise": 8,
    "conference call transcript": 8,
    "international sales law": 10,
    "after sales service": 7,
    "adverse event signaling": 10,
}

INTERACTION_SKILL_TERMS = {
    "negotiation": 4,
    "conversation": 3,
    "listening": 3,
    "question": 2,
    "objection": 4,
    "resistance": 3,
    "rapport": 3,
    "trust": 2,
    "communication": 2,
    "follow up": 3,
    "adaptive": 3,
    "behavior": 2,
    "behaviour": 2,
    "qualification": 3,
    "value communication": 3,
    "ethical": 3,
    "ethics": 3,
    "honesty": 3,
    "deception": 3,
    "谈判": 4,
    "对话": 3,
    "倾听": 3,
    "提问": 2,
    "异议": 4,
    "抗拒": 3,
    "信任": 2,
    "沟通": 2,
    "跟进": 3,
    "适应": 3,
    "行为": 2,
    "资格判断": 3,
    "价值沟通": 3,
    "伦理": 3,
    "诚实": 3,
    "欺骗": 3,
}

TOOL_SPECIFICITY_TERMS = {
    "sales call": 6,
    "sales conversation": 6,
    "call analysis": 5,
    "conversation analysis": 4,
    "sales transcript": 6,
    "sales coaching": 5,
    "sales assistant": 5,
    "crm": 3,
    "销售通话": 6,
    "销售对话": 6,
    "通话分析": 5,
    "销售转写": 6,
}

EVIDENCE_TERMS = {
    "experiment": 4,
    "field study": 5,
    "meta-analysis": 6,
    "systematic review": 5,
    "conversation analysis": 5,
    "randomized": 5,
    "longitudinal": 4,
    "dataset": 3,
    "benchmark": 3,
    "evaluation": 3,
    "empirical": 3,
    "transcript": 4,
    "recording": 4,
    "case study": 2,
    "实验": 4,
    "实证": 3,
    "元分析": 6,
    "语料": 4,
}

MECHANISM_TERMS = {
    "mechanism": 3,
    "framework": 2,
    "model": 1,
    "adaptive": 3,
    "turn-taking": 4,
    "discourse": 3,
    "diagnosis": 3,
    "behavior": 2,
    "strategy": 1,
    "workflow": 2,
    "机制": 3,
    "框架": 2,
    "诊断": 3,
    "行为": 2,
}

RISK_TERMS = {
    "guaranteed close": 10,
    "close anyone": 10,
    "secret script": 8,
    "manipulation": 5,
    "fake scarcity": 10,
    "mass outreach": 5,
    "spam": 6,
    "保证成交": 10,
    "逼单": 6,
    "操控": 6,
    "群发": 5,
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "to",
    "using", "with", "through", "towards", "toward", "study", "analysis",
}


@dataclass
class Candidate:
    candidate_id: str
    source: str
    source_family: str
    direction: str
    query: str
    title: str
    url: str
    description: str = ""
    description_kind: str = "none"
    topic_labels: list[str] = field(default_factory=list)
    year: int | None = None
    popularity: int = 0
    popularity_kind: str = "unknown"
    venue: str = ""
    doi: str = ""
    license: str = ""
    open_access: bool | None = None
    updated_at: str = ""
    raw_reusable: bool = False
    retrieval_allowed: bool = False
    authority: str = "none"
    flags: list[str] = field(default_factory=list)
    score_components: dict[str, float] = field(default_factory=dict)
    quality_score: float = 0.0
    metadata_confidence: float = 0.0
    duplicate_of: str | None = None
    near_duplicate_of: str | None = None
    decision: str = "unscored"
    decision_reason: str = ""
    audit_sample: bool = False
    audit_inclusion_probability: float = 0.0
    audit_stratum: str = "none"
    pre_audit_decision: str = ""
    evidence_eligible: bool = False


def http_json(url: str, timeout: int = 30, attempts: int = 5) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = min(30.0, float(retry_after) if retry_after else 1.5 * (2**attempt))
            time.sleep(delay)
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, ConnectionError):
            if attempt + 1 == attempts:
                raise
            time.sleep(1.5 * (2**attempt))
    raise RuntimeError("unreachable")


def build_url(base: str, params: dict[str, Any]) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def stable_id(source: str, value: str) -> str:
    digest = hashlib.sha256(f"{source}|{value}".encode("utf-8")).hexdigest()[:20]
    return f"candidate.{source}.{digest}"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_doi(value: Any) -> str:
    text = clean_text(value).lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", text)


def normalize_title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", value.lower()).strip()


def features(value: str) -> set[str]:
    text = normalize_title(value)
    words = {word for word in text.split() if len(word) > 1 and word not in STOPWORDS}
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    words.update(cjk[i : i + 2] for i in range(max(0, len(cjk) - 1)))
    return words


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def simhash64(tokens: Iterable[str]) -> int:
    vector = [0] * 64
    for token in tokens:
        value = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            result |= 1 << bit
    return result


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def fetch_openalex(per_query: int = 100, pages: int = 1) -> list[Candidate]:
    results: list[Candidate] = []
    select = "id,doi,title,publication_year,cited_by_count,type,open_access,primary_topic,primary_location"
    for direction, query in SALES_QUERIES:
        for page in range(1, pages + 1):
            url = build_url(
                "https://api.openalex.org/works",
                {"search": query, "per-page": per_query, "page": page, "select": select},
            )
            payload = http_json(url)
            for item in payload.get("results", []):
                title = clean_text(item.get("title"))
                if not title:
                    continue
                topic = clean_text((item.get("primary_topic") or {}).get("display_name"))
                location = item.get("primary_location") or {}
                venue = clean_text((location.get("source") or {}).get("display_name"))
                oa = item.get("open_access") or {}
                source_id = clean_text(item.get("id")) or title
                results.append(
                    Candidate(
                        candidate_id=stable_id("openalex", source_id),
                        source="openalex",
                        source_family="academic_metadata",
                        direction=direction,
                        query=query,
                        title=title,
                        url=clean_text(item.get("doi")) or source_id,
                        description="",
                        description_kind="missing",
                        topic_labels=[topic] if topic else [],
                        year=item.get("publication_year"),
                        popularity=int(item.get("cited_by_count") or 0),
                        popularity_kind="citations",
                        venue=venue,
                        doi=canonical_doi(item.get("doi")),
                        open_access=bool(oa.get("is_oa")),
                    )
                )
            time.sleep(0.05)
    return results


def fetch_semantic_scholar(cap_per_query: int = 200) -> list[Candidate]:
    results: list[Candidate] = []
    fields = "title,url,year,citationCount,venue,publicationTypes,externalIds,openAccessPdf"
    for direction, query in S2_QUERIES:
        url = build_url(
            "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
            {"query": query, "fields": fields},
        )
        payload = http_json(url)
        for item in payload.get("data", [])[:cap_per_query]:
            title = clean_text(item.get("title"))
            if not title:
                continue
            external = item.get("externalIds") or {}
            paper_id = clean_text(item.get("paperId")) or title
            oa_pdf = item.get("openAccessPdf") or {}
            results.append(
                Candidate(
                    candidate_id=stable_id("s2", paper_id),
                    source="semantic_scholar",
                    source_family="academic_metadata",
                    direction=direction,
                    query=query,
                    title=title,
                    url=clean_text(item.get("url")),
                    year=item.get("year"),
                    popularity=int(item.get("citationCount") or 0),
                    popularity_kind="citations",
                    venue=clean_text(item.get("venue")),
                    doi=canonical_doi(external.get("DOI")),
                    open_access=bool(oa_pdf.get("url")),
                )
            )
        time.sleep(1.1)
    return results


def fetch_crossref(rows_per_query: int = 0) -> list[Candidate]:
    if rows_per_query <= 0:
        return []
    results: list[Candidate] = []
    select = "DOI,title,published,container-title,type,is-referenced-by-count,URL,license"
    for direction, query in SALES_QUERIES:
        url = build_url(
            "https://api.crossref.org/works",
            {
                "query.bibliographic": query,
                "rows": min(1000, rows_per_query),
                "select": select,
            },
        )
        payload = http_json(url, timeout=45)
        for item in (payload.get("message") or {}).get("items", []):
            titles = item.get("title") or []
            title = clean_text(titles[0] if titles else "")
            if not title:
                continue
            doi = canonical_doi(item.get("DOI"))
            source_id = doi or clean_text(item.get("URL")) or title
            containers = item.get("container-title") or []
            published = item.get("published") or {}
            date_parts = published.get("date-parts") or []
            year = None
            if date_parts and date_parts[0]:
                try:
                    year = int(date_parts[0][0])
                except (TypeError, ValueError):
                    pass
            licenses = item.get("license") or []
            license_value = clean_text(licenses[0].get("URL")) if licenses else ""
            results.append(
                Candidate(
                    candidate_id=stable_id("crossref", source_id),
                    source="crossref",
                    source_family="academic_metadata",
                    direction=direction,
                    query=query,
                    title=title,
                    url=clean_text(item.get("URL")) or (f"https://doi.org/{doi}" if doi else ""),
                    description_kind="missing",
                    year=year,
                    popularity=int(item.get("is-referenced-by-count") or 0),
                    popularity_kind="citations",
                    venue=clean_text(containers[0] if containers else ""),
                    doi=doi,
                    license=license_value,
                    open_access=None,
                )
            )
        time.sleep(0.2)
    return results


def fetch_github(per_query: int = 50) -> list[Candidate]:
    results: list[Candidate] = []
    for direction, query in GITHUB_QUERIES:
        url = build_url(
            "https://api.github.com/search/repositories",
            {"q": query, "sort": "stars", "order": "desc", "per_page": per_query},
        )
        payload = http_json(url)
        for item in payload.get("items", []):
            full_name = clean_text(item.get("full_name"))
            if not full_name:
                continue
            license_value = clean_text((item.get("license") or {}).get("spdx_id"))
            results.append(
                Candidate(
                    candidate_id=stable_id("github", full_name.lower()),
                    source="github",
                    source_family="open_source_tool",
                    direction=direction,
                    query=query,
                    title=full_name,
                    url=clean_text(item.get("html_url")),
                    description=clean_text(item.get("description")),
                    description_kind="repository_description",
                    popularity=int(item.get("stargazers_count") or 0),
                    popularity_kind="stars",
                    license=license_value,
                    updated_at=clean_text(item.get("updated_at")),
                )
            )
        time.sleep(0.2)
    return results


def exact_and_near_dedupe(candidates: list[Candidate]) -> tuple[list[Candidate], dict[str, int]]:
    seen: dict[str, str] = {}
    unique: list[Candidate] = []
    exact_count = 0
    near_count = 0
    buckets: dict[int, list[tuple[int, str, set[str]]]] = defaultdict(list)
    for candidate in candidates:
        title_key = normalize_title(candidate.title)
        exact_key = f"doi:{candidate.doi}" if candidate.doi else f"title:{title_key}"
        if exact_key in seen:
            candidate.duplicate_of = seen[exact_key]
            exact_count += 1
            continue
        seen[exact_key] = candidate.candidate_id
        token_set = features(candidate.title)
        signature = simhash64(token_set)
        bucket = signature >> 52
        near_match = None
        for other_hash, other_id, other_tokens in buckets[bucket]:
            if hamming(signature, other_hash) <= 4 and jaccard(token_set, other_tokens) >= 0.88:
                near_match = other_id
                break
        if near_match:
            candidate.near_duplicate_of = near_match
            near_count += 1
            continue
        buckets[bucket].append((signature, candidate.candidate_id, token_set))
        unique.append(candidate)
    return unique, {"exact_duplicates": exact_count, "near_duplicates": near_count}


def has_term(text: str, term: str) -> bool:
    lower = text.lower()
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in lower
    # Match whole English phrases and tolerate spaces/dashes between words.
    pattern = re.escape(term.lower()).replace(r"\ ", r"[\s\-\u2013\u2014]+")
    plural = "" if term.lower().endswith("s") else r"(?:s|es)?"
    return re.search(rf"(?<![a-z0-9]){pattern}{plural}(?![a-z0-9])", lower) is not None


def term_score(text: str, terms: dict[str, int], cap: float) -> float:
    return min(cap, float(sum(weight for term, weight in terms.items() if has_term(text, term))))


def professional_context_score(title: str) -> float:
    anchor = term_score(title, PROFESSIONAL_ANCHORS, 12)
    commercial = term_score(title, COMMERCIAL_CONTEXT_TERMS, 8)
    interaction = term_score(title, INTERACTION_SKILL_TERMS, 8)
    if commercial >= 2 and interaction >= 2:
        anchor = max(anchor, 6.0)
    return min(12.0, anchor)


def score_candidate(candidate: Candidate) -> None:
    text = f"{candidate.title} {candidate.description}"
    title_relevance = term_score(candidate.title, RELEVANCE_TERMS, 35)
    description_relevance = term_score(candidate.description, RELEVANCE_TERMS, 20)
    relevance = min(35.0, title_relevance + 0.50 * description_relevance)
    topic_label_relevance = term_score(" ".join(candidate.topic_labels), RELEVANCE_TERMS, 20)
    topic_label_bonus = min(2.0, 0.15 * topic_label_relevance)
    sales_context = professional_context_score(candidate.title)
    interaction_context = term_score(candidate.title, INTERACTION_SKILL_TERMS, 12)
    tool_specificity = term_score(text, TOOL_SPECIFICITY_TERMS, 12)
    evidence = term_score(candidate.title, EVIDENCE_TERMS, 18)
    mechanism = term_score(candidate.title, MECHANISM_TERMS, 12)
    risk = term_score(text, RISK_TERMS, 20)
    negative_domain = term_score(candidate.title, NEGATIVE_DOMAIN_TERMS, 20)
    provenance = 1.0
    if candidate.doi:
        provenance += 2
    if candidate.venue:
        provenance += 1
    if candidate.license:
        provenance += 2
    if candidate.open_access:
        provenance += 1
    provenance = min(5, provenance)
    popularity_signal = min(3.0, math.log1p(candidate.popularity) * 0.45)
    source_fit = 3.0 if candidate.source_family == "academic_metadata" else 2.0
    maintenance = 0.0
    if candidate.source_family == "open_source_tool":
        if candidate.updated_at:
            try:
                year = int(candidate.updated_at[:4])
                maintenance = 4.0 if year >= CURRENT_YEAR - 1 else 2.0 if year >= CURRENT_YEAR - 3 else 0.0
            except ValueError:
                pass
        if not candidate.license:
            candidate.flags.append("license_missing")
    if risk:
        candidate.flags.append("ethical_or_hype_risk_needs_review")
    if negative_domain:
        candidate.flags.append("mission_specific_negative_domain")
    if relevance < 8:
        candidate.flags.append("weak_direct_relevance")
    direct_interaction = sales_context >= 5 and interaction_context >= 2
    if candidate.source_family == "academic_metadata":
        topic_eligible = negative_domain == 0 and (
            direct_interaction or (sales_context >= 5 and evidence >= 3)
        )
    else:
        topic_eligible = negative_domain == 0 and relevance >= 8 and tool_specificity >= 5
    components = {
        "direct_relevance": relevance,
        "title_relevance": title_relevance,
        "professional_context": sales_context,
        "interaction_context": interaction_context,
        "direct_interaction_gate": 1.0 if direct_interaction else 0.0,
        "tool_specificity": tool_specificity,
        "topic_label_bonus": topic_label_bonus,
        "evidence_signal": evidence,
        "mechanism_signal": mechanism,
        "provenance": provenance,
        "popularity_signal_capped": popularity_signal,
        "source_fit": source_fit,
        "maintenance_feasibility": maintenance,
        "risk_penalty": -risk,
        "negative_domain_penalty": -negative_domain,
    }
    candidate.score_components = {key: round(value, 2) for key, value in components.items()}
    context_bonus = min(
        8.0,
        tool_specificity if candidate.source_family == "open_source_tool" else (sales_context + interaction_context) / 2,
    )
    total = max(
        0.0,
        min(
            100.0,
            relevance
            + topic_label_bonus
            + evidence
            + mechanism
            + context_bonus
            + provenance
            + popularity_signal
            + source_fit
            + maintenance
            - risk
            - negative_domain,
        ),
    )
    # An API search match can come from hidden full text, README content, or a
    # broad topic label.  A relative rank must never override this absolute
    # eligibility gate, and an ineligible result may not consume evidence slots.
    if not topic_eligible:
        total = min(total, 19.0)
    known_fields = sum(
        bool(value)
        for value in [
            candidate.title,
            candidate.url,
            candidate.description or candidate.topic_labels,
            candidate.venue,
            candidate.doi,
            candidate.license,
        ]
    )
    candidate.metadata_confidence = round(min(0.98, 0.46 + known_fields * 0.08), 2)
    candidate.evidence_eligible = (
        topic_eligible
        and total >= MIN_REVIEW_SCORE
        and candidate.metadata_confidence >= MIN_METADATA_CONFIDENCE
    )
    if not candidate.evidence_eligible:
        candidate.flags.append("not_evidence_eligible_on_retained_metadata")
    candidate.quality_score = round(total, 2)


def cap_candidate_pool(candidates: list[Candidate], target: int) -> list[Candidate]:
    if len(candidates) <= target:
        return candidates
    # Source identity is not a quality guarantee. Keep the strongest explicitly
    # eligible items first and use deterministic tie-breaking; source/direction
    # diversity is enforced later as an upper bound on review allocation.
    ranked = sorted(
        candidates,
        key=lambda item: (
            not item.evidence_eligible,
            -item.quality_score,
            -item.metadata_confidence,
            item.source,
            item.candidate_id,
        ),
    )
    return ranked[:target]


def popularity_rank(candidates: list[Candidate], count: int) -> list[str]:
    return [
        item.candidate_id
        for item in sorted(candidates, key=lambda value: (-value.popularity, value.candidate_id))[:count]
    ]


def quality_rank(candidates: list[Candidate], count: int) -> list[str]:
    return [
        item.candidate_id
        for item in sorted(candidates, key=lambda value: (-value.quality_score, value.candidate_id))[:count]
    ]


def diverse_rank(candidates: list[Candidate], count: int, *, enforce_family_cap: bool = True) -> list[str]:
    remaining = sorted(candidates, key=lambda value: (-value.quality_score, value.candidate_id))
    selected: list[Candidate] = []
    direction_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    source_name_counts: Counter[str] = Counter()
    max_per_direction = max(1, math.ceil(count * 0.20)) if count else 0
    max_per_source = max(1, math.ceil(count * 0.45)) if count else 0
    max_per_family = max(1, math.ceil(count * 0.80)) if count else 0

    while remaining and len(selected) < count:
        best = None
        best_value = -1e9
        for candidate in remaining:
            if direction_counts[candidate.direction] >= max_per_direction:
                continue
            if source_name_counts[candidate.source] >= max_per_source:
                continue
            if enforce_family_cap and source_counts[candidate.source_family] >= max_per_family:
                continue
            candidate_features = features(candidate.title)
            redundancy = max(
                (jaccard(candidate_features, features(item.title)) for item in selected),
                default=0.0,
            )
            diversity_bonus = 8.0 / (1 + direction_counts[candidate.direction])
            source_bonus = 3.0 / (1 + source_counts[candidate.source_family])
            value = 0.82 * candidate.quality_score - 18.0 * redundancy + diversity_bonus + source_bonus
            if value > best_value:
                best = candidate
                best_value = value
        if best is None:
            # Budgets are ceilings, not quotas. Never violate a concentration
            # guard merely to fill a nominal percentage.
            break
        selected.append(best)
        direction_counts[best.direction] += 1
        source_counts[best.source_family] += 1
        source_name_counts[best.source] += 1
        remaining.remove(best)
    return [item.candidate_id for item in selected]


def assign_successive_halving(candidates: list[Candidate], seed: int) -> dict[str, int]:
    ranked = sorted(candidates, key=lambda item: (-item.quality_score, -item.metadata_confidence, item.candidate_id))
    eligible = [item for item in ranked if item.evidence_eligible]
    total = len(ranked)
    stage1_n = math.ceil(total * 0.20)
    stage2_n = math.ceil(total * 0.05)
    stage3_n = math.ceil(total * 0.01)
    stage1_items = eligible[: min(stage1_n, len(eligible))]
    stage1 = {item.candidate_id for item in stage1_items}
    stage2 = set(diverse_rank(stage1_items, min(stage2_n, len(stage1_items))))
    stage3_pool = [
        item
        for item in stage1_items
        if item.candidate_id in stage2
        and item.source_family == "academic_metadata"
        and item.score_components.get("direct_interaction_gate", 0) == 1
        and (
            item.score_components.get("evidence_signal", 0) >= 3
            or item.score_components.get("mechanism_signal", 0) >= 3
        )
    ]
    stage3 = set(
        diverse_rank(
            stage3_pool,
            min(stage3_n, len(stage3_pool)),
            enforce_family_cap=False,
        )
    )
    for candidate in candidates:
        if not candidate.evidence_eligible:
            if "mission_specific_negative_domain" in candidate.flags:
                candidate.decision = "skip_for_now"
                candidate.decision_reason = "mission_specific_negative_domain"
            elif (
                candidate.score_components.get("professional_context", 0) >= 5
                or candidate.score_components.get("direct_relevance", 0) >= 8
            ):
                candidate.decision = "abstain_needs_metadata"
                candidate.decision_reason = "possible_topic_match_without_absolute_professional_context"
            else:
                candidate.decision = "skip_for_now"
                candidate.decision_reason = "absolute_relevance_gate_not_met"
        elif candidate.candidate_id in stage3:
            candidate.decision = "priority_evidence_review"
            candidate.decision_reason = "top_1_percent_after_quality_and_diversity_cascade"
        elif candidate.candidate_id in stage2:
            candidate.decision = "secondary_evidence_review"
            candidate.decision_reason = "top_5_percent_after_metadata_screen"
        elif candidate.candidate_id in stage1:
            candidate.decision = "metadata_shortlist"
            candidate.decision_reason = "top_20_percent_metadata_screen"
        elif candidate.candidate_id not in stage1:
            candidate.decision = "skip_for_now"
            candidate.decision_reason = "below_current_attention_budget"
        if candidate.metadata_confidence < 0.62 and candidate.decision != "skip_for_now":
            candidate.decision = "abstain_needs_metadata"
            candidate.decision_reason = "insufficient_metadata_for_safe_priority_decision"
        if "ethical_or_hype_risk_needs_review" in candidate.flags and candidate.decision == "priority_evidence_review":
            candidate.decision = "secondary_evidence_review"
            candidate.decision_reason = "high_score_but_ethical_or_hype_risk_requires_review"
    skipped = [item for item in candidates if item.decision == "skip_for_now"]
    rng = random.Random(seed)
    # Keep an unbiased uniform stratum for inference and separate diagnostic
    # strata for threshold errors and coverage gaps. Diagnostic samples must not
    # be pooled into a population miss-rate estimate.
    audit_count = min(len(skipped), max(100, math.ceil(len(skipped) * 0.05))) if skipped else 0
    uniform_n = min(len(skipped), round(audit_count * 0.60))
    uniform = rng.sample(skipped, uniform_n)
    uniform_ids = {item.candidate_id for item in uniform}
    remaining_audit = [item for item in skipped if item.candidate_id not in uniform_ids]
    boundary_n = min(len(remaining_audit), round(audit_count * 0.25))
    boundary = sorted(
        remaining_audit,
        key=lambda item: (
            -item.score_components.get("professional_context", 0),
            -item.score_components.get("direct_relevance", 0),
            -item.quality_score,
            item.candidate_id,
        ),
    )[:boundary_n]
    boundary_ids = {item.candidate_id for item in boundary}
    coverage_pool = [item for item in remaining_audit if item.candidate_id not in boundary_ids]
    coverage_n = min(len(coverage_pool), audit_count - uniform_n - boundary_n)
    coverage_ids = set(diverse_rank(coverage_pool, coverage_n, enforce_family_cap=False))
    uniform_probability = uniform_n / len(skipped) if skipped else 0.0
    for candidate in uniform:
        candidate.audit_sample = True
        candidate.pre_audit_decision = candidate.decision
        candidate.audit_stratum = "uniform_inference"
        candidate.audit_inclusion_probability = round(uniform_probability, 8)
    for candidate in boundary:
        candidate.audit_sample = True
        candidate.pre_audit_decision = candidate.decision
        candidate.audit_stratum = "boundary_diagnostic"
    for candidate in coverage_pool:
        if candidate.candidate_id in coverage_ids:
            candidate.audit_sample = True
            candidate.pre_audit_decision = candidate.decision
            candidate.audit_stratum = "coverage_diagnostic"
    return {
        "metadata_shortlist_budget": stage1_n,
        "metadata_shortlist_selected": len(stage1),
        "secondary_evidence_budget": stage2_n,
        "secondary_evidence_selected": len(stage2),
        "priority_evidence_budget": stage3_n,
        "priority_evidence_selected": len(stage3),
        "skip_audit_budget": audit_count,
        "skip_audit_uniform_selected": uniform_n,
        "skip_audit_boundary_selected": boundary_n,
        "skip_audit_coverage_selected": len(coverage_ids),
        "skip_audit_uniform_inclusion_probability": round(uniform_probability, 8),
    }


def direction_coverage(ids: list[str], by_id: dict[str, Candidate]) -> int:
    return len({by_id[value].direction for value in ids if value in by_id})


def source_coverage(ids: list[str], by_id: dict[str, Candidate]) -> int:
    return len({by_id[value].source for value in ids if value in by_id})


def overlap(left: list[str], right: list[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set and not right_set:
        return 1.0
    return round(len(left_set & right_set) / max(1, len(left_set | right_set)), 3)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: Iterable[Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def reset_computed_state(candidate: Candidate) -> None:
    candidate.flags = []
    candidate.score_components = {}
    candidate.quality_score = 0.0
    candidate.metadata_confidence = 0.0
    candidate.duplicate_of = None
    candidate.near_duplicate_of = None
    candidate.decision = "unscored"
    candidate.decision_reason = ""
    candidate.audit_sample = False
    candidate.audit_inclusion_probability = 0.0
    candidate.audit_stratum = "none"
    candidate.pre_audit_decision = ""
    candidate.evidence_eligible = False


def load_frozen_candidates(path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                candidate = Candidate(**json.loads(line))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid candidate JSONL at line {line_number}: {exc}") from exc
            reset_computed_state(candidate)
            candidates.append(candidate)
    return candidates


def run(
    output_dir: Path,
    target: int,
    seed: int,
    pages: int,
    input_jsonl: Path | None = None,
    crossref_rows_per_query: int = 0,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fetched: list[Candidate] = []
    fetch_errors: list[dict[str, str]] = []
    if input_jsonl is not None:
        fetched = load_frozen_candidates(input_jsonl)
    else:
        for source_name, loader in [
            ("github", fetch_github),
            ("semantic_scholar", fetch_semantic_scholar),
            ("crossref", lambda: fetch_crossref(crossref_rows_per_query)),
            ("openalex", lambda: fetch_openalex(pages=pages)),
        ]:
            try:
                fetched.extend(loader())
            except Exception as exc:  # keep the batch useful when one public API is down
                fetch_errors.append({"source": source_name, "error": f"{type(exc).__name__}: {exc}"})
    unique, dedupe = exact_and_near_dedupe(fetched)
    for candidate in unique:
        score_candidate(candidate)
    candidates = cap_candidate_pool(unique, target)
    budgets = assign_successive_halving(candidates, seed)
    by_id = {item.candidate_id: item for item in candidates}
    eligible_for_comparison = [item for item in candidates if item.evidence_eligible]
    comparison_budget = math.ceil(len(candidates) * 0.05)
    compare_count = min(comparison_budget, len(eligible_for_comparison))
    popularity = popularity_rank(eligible_for_comparison, compare_count)
    quality = quality_rank(eligible_for_comparison, compare_count)
    diversity = diverse_rank(eligible_for_comparison, compare_count)
    cascade = [
        item.candidate_id
        for item in candidates
        if item.decision in {"priority_evidence_review", "secondary_evidence_review"}
    ]
    decision_counts = Counter(item.decision for item in candidates)
    audit_counts = Counter(item.audit_stratum for item in candidates if item.audit_sample)
    source_counts = Counter(item.source for item in candidates)
    direction_counts = Counter(item.direction for item in candidates)
    comparisons = {
        "selection_size": compare_count,
        "selection_budget_cap": comparison_budget,
        "popularity_only": {
            "selected_count": len(popularity),
            "direction_coverage": direction_coverage(popularity, by_id),
            "source_coverage": source_coverage(popularity, by_id),
        },
        "quality_only": {
            "selected_count": len(quality),
            "direction_coverage": direction_coverage(quality, by_id),
            "source_coverage": source_coverage(quality, by_id),
        },
        "diversity_aware": {
            "selected_count": len(diversity),
            "direction_coverage": direction_coverage(diversity, by_id),
            "source_coverage": source_coverage(diversity, by_id),
        },
        "risk_coverage_cascade": {
            "selected_count": len(cascade),
            "direction_coverage": direction_coverage(cascade, by_id),
            "source_coverage": source_coverage(cascade, by_id),
            "abstention_enabled": True,
            "skip_audit_enabled": True,
        },
        "jaccard_popularity_vs_quality": overlap(popularity, quality),
        "jaccard_quality_vs_diversity": overlap(quality, diversity),
    }
    source_manifest = {
        "version": 2,
        "algorithm_version": ALGORITHM_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "source_kind": "official_public_metadata_apis",
        "input_mode": "frozen_jsonl_replay" if input_jsonl is not None else "live_official_api_metadata",
        "replayed_from": str(input_jsonl.resolve()) if input_jsonl is not None else None,
        "sources": [
            {"name": "OpenAlex", "url": "https://api.openalex.org/", "content_used": "metadata_only"},
            {"name": "Semantic Scholar Academic Graph", "url": "https://api.semanticscholar.org/graph/v1", "content_used": "metadata_only"},
            {"name": "Crossref REST API", "url": "https://api.crossref.org/works", "content_used": "metadata_only"},
            {"name": "GitHub REST Search", "url": "https://api.github.com/search/repositories", "content_used": "repository_metadata_only"},
        ],
        "fetch_errors": fetch_errors,
        "raw_asset_reusable": False,
        "full_text_fetched": False,
        "media_fetched": False,
        "retrieval_allowed": False,
        "authority": "none",
        "purpose": "decide whether a source deserves more evidence budget",
    }
    report = {
        "version": 3,
        "algorithm_version": ALGORITHM_VERSION,
        "status": "quarantine_metadata_triage_only",
        "input_mode": "frozen_jsonl_replay" if input_jsonl is not None else "live_official_api_metadata",
        "requested_target": target,
        "fetched_count": len(fetched),
        "unique_before_cap": len(unique),
        "evaluated_count": len(candidates),
        "dedupe": dedupe,
        "source_counts": dict(source_counts),
        "direction_counts": dict(direction_counts),
        "decision_counts": dict(decision_counts),
        "audit_counts": dict(audit_counts),
        "budgets": budgets,
        "strategy_comparison": comparisons,
        "limitations": [
            "No candidate was read in full or learned from this run.",
            "Metadata scores estimate next-step value, not truth or professional quality.",
            "Popularity is capped and cannot independently qualify a candidate.",
            "False negatives are only sampled, not yet measured against human labels.",
            "Only the uniform_inference audit stratum may estimate a skip-pool miss rate; diagnostic strata only find errors.",
            "Douyin favorites are intentionally not fetched because no ordinary-user official favorites-list API was identified.",
        ],
    }
    write_json(output_dir / "source_manifest.json", source_manifest)
    write_jsonl(output_dir / "candidate_cards.jsonl", (asdict(item) for item in sorted(candidates, key=lambda value: value.candidate_id)))
    write_json(output_dir / "run_report.json", report)
    top = sorted(
        (
            item
            for item in candidates
            if item.decision in {"priority_evidence_review", "secondary_evidence_review", "metadata_shortlist"}
        ),
        key=lambda item: (-item.quality_score, item.candidate_id),
    )[:30]
    lines = [
        "# High-throughput learning triage run",
        "",
        f"- evaluated: {len(candidates)} metadata-only candidates",
        f"- fetched before dedupe: {len(fetched)}",
        f"- decisions: `{dict(decision_counts)}`",
        f"- audit strata: `{dict(audit_counts)}`",
        f"- source mix: `{dict(source_counts)}`",
        f"- duplicate removal: `{dedupe}`",
        "- state: Quarantine; no learned/mastered claim; no source is retrieval-authoritative",
        "",
        "## Strategy comparison",
        "",
        "```json",
        json.dumps(comparisons, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Top metadata candidates (evidence review only)",
        "",
    ]
    for item in top:
        lines.append(f"- {item.quality_score:05.2f} | {item.decision} | [{item.title}]({item.url}) | {item.source}/{item.direction}")
    lines.extend(["", "## Boundary", "", *[f"- {value}" for value in report["limitations"]]])
    (output_dir / "run_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def self_test() -> None:
    candidates = [
        Candidate("a", "x", "academic_metadata", "d1", "sales", "Field experiment on sales listening", "https://a", doi="10.x/a", venue="J", popularity=50),
        Candidate("b", "x", "academic_metadata", "d1", "sales", "Field experiment on sales listening!", "https://b", doi="10.x/a", popularity=5),
        Candidate("c", "github", "open_source_tool", "d2", "tool", "demo/spam", "https://c", description="secret script guaranteed close anyone", popularity=1000),
    ]
    unique, stats = exact_and_near_dedupe(candidates)
    assert len(unique) == 2 and stats["exact_duplicates"] == 1
    for candidate in unique:
        score_candidate(candidate)
    assert unique[0].quality_score > unique[1].quality_score
    assert unique[0].evidence_eligible
    assert not unique[1].evidence_eligible
    assert professional_context_score("The Case for Living Kidney Sales") == 0
    assert professional_context_score("Knowledge, Motivation, and Adaptive Behavior: A Framework for Improving Selling Effectiveness") >= 5
    budgets = assign_successive_halving(unique, 7)
    assert budgets["priority_evidence_budget"] == 1
    assert all(item.decision != "skip_audit_sample" for item in unique)
    print(json.dumps({"self_test": "PASS", "unique": len(unique), "dedupe": stats}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("learning_quarantine/high_throughput/run_sales_1200"))
    parser.add_argument("--target", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=20260902)
    parser.add_argument("--openalex-pages", type=int, default=1)
    parser.add_argument("--crossref-rows-per-query", type=int, default=0)
    parser.add_argument("--input-jsonl", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    report = run(
        args.output_dir,
        args.target,
        args.seed,
        args.openalex_pages,
        args.input_jsonl,
        args.crossref_rows_per_query,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["evaluated_count"] >= min(args.target, 100) else 2


if __name__ == "__main__":
    raise SystemExit(main())
