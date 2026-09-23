"""Cold-start governance loading, path admission, and repository validation."""

from __future__ import annotations

import copy
import hashlib
import re
import subprocess
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from .contracts import ContractError, ContractLoader


class GovernanceValidationError(RuntimeError):
    """Raised when one or more governance checks fail."""


@dataclass(frozen=True, slots=True)
class PathDecision:
    path: str
    allowed: bool
    zone: str | None
    matched_rule: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class GovernanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class GovernanceReport:
    checks: tuple[GovernanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[GovernanceCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def require_success(self) -> None:
        if self.failures:
            details = "; ".join(
                f"{check.name}: {check.detail}" for check in self.failures
            )
            raise GovernanceValidationError(details)


@dataclass(frozen=True, slots=True)
class ColdStartSnapshot:
    program_id: str
    purpose: str
    purpose_ref: str
    root_metric: str
    active_phase: str
    phase_file: str
    invariant_objective: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    acceptance_commands: tuple[str, ...]
    exit_conditions: tuple[str, ...]
    audit_requirements: tuple[str, ...]
    ledger_open_proposals: tuple[str, ...]
    discovered_proposals: tuple[str, ...]
    known_blockers: tuple[str, ...]
    truth_refs: tuple[str, ...]
    projection_warning: str


@dataclass(frozen=True, slots=True)
class _ControllerTransaction:
    transaction_id: str
    repair_base: str
    proposal_commit: str
    authority_commit: str
    transaction_commit: str
    frozen_paths: frozenset[str]
    historical_exemptions: frozenset[str]


@dataclass(frozen=True, slots=True)
class _PhaseAuthorityState:
    active_id: str
    phase_order: tuple[str, ...]
    active_index: int
    baseline_commit: str | None
    completed_transition_commits: tuple[str, ...]
    controller_transaction: _ControllerTransaction


@dataclass(frozen=True, slots=True)
class _ValidatedGate:
    decision_path: str
    decision_commit: str
    gate_result: str
    phase_id: str
    next_phase_id: str | None
    phase_baseline_commit: str
    next_phase_plan_ref: str
    completion_evidence: tuple[str, ...]
    decision_authority_ref: str
    controller_actor: str
    decided_at: str
    transition_manifest: tuple[Mapping[str, Any], ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceValidationError(f"{label} must be a mapping")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GovernanceValidationError(f"{label} must be a non-empty string")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise GovernanceValidationError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _normalize_relative_path(path: str | Path) -> str:
    raw = str(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("//"):
        raise ValueError(f"absolute or UNC paths are not allowed: {path}")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part == ".." or ":" in part for part in pure.parts)
    ):
        raise ValueError(f"path must be repository-relative and traversal-free: {path}")
    normalized = pure.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _path_identity(path: str | Path) -> str:
    """Return a platform-stable identity for duplicate authority paths."""

    return unicodedata.normalize("NFC", _normalize_relative_path(path)).casefold()


def _glob_regex(pattern: str) -> re.Pattern[str]:
    normalized = _normalize_relative_path(pattern)
    expression: list[str] = ["^"]
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if character == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                expression.append(".*")
                index += 2
                continue
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.compile("".join(expression))


def _matches(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).fullmatch(path))


class GovernanceValidator:
    """Load repository authority from disk and prove that it agrees."""

    _PROTECTED_PATTERNS = (
        "AGENTS.md",
        "constitution/**",
        "control/**",
        "phases/**",
        "schemas/**",
    )
    _GCR001_ID = "GCR001"
    _GCR001_FOUNDING_ANCHOR = "f38e767ea2dbde01eb8440198887c7895e3721d9"
    _GCR001_REPAIR_BASE = "24fffd958ddcff034efdacc20cb9978d92ddedec"
    _GCR001_PROPOSAL = "proposals/P0_GATE_CLOSURE_GCR001.md"
    _GCR001_PROPOSAL_SHA256 = (
        "ed5e606b9fc94351c9dfdd9c69e36d9a4ac44d323effa11691fb15df00dfd170"
    )
    _GCR001_AUTHORITY = "audit/P0_GATE_CLOSURE_AUTHORITY_GCR001.yaml"
    _GCR001_G_PATHS = frozenset(
        {
            "MANIFEST.sha256",
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/P0_FOUNDATION_AUDIT.yaml",
            "schemas/ironman.schema.yaml",
            "schemas/phase_gate_decision.schema.yaml",
        }
    )
    _GCR001_G_SHA256 = {
        "MANIFEST.sha256": "3662503d41d7d53fef57d9e06a1a1d7153978c895ad3062595dae1e2cd4d6935",
        "control/ACTIVE_PHASE.yaml": "8f1b8e433ab23c82ac56086543c6f8fb5060cdfb763396851f6ad7b2609ee16e",
        "control/PROGRAM_LEDGER.yaml": "76f30044eb3c3507f3cc3ee485fde1bb2ef54c36e63d0614bb5ee2f6e8182add",
        "phases/P0_FOUNDATION_AUDIT.yaml": "0c756722b6d27621b503430622e1738db8dd5b64baee8a9b72c736e3e53597bc",
        "schemas/ironman.schema.yaml": "c0b6bb7ee95ba7d91c4feaefeb8b5a6d4d0bc2d4a4c2dd9a234baefb03ae9dd6",
        "schemas/phase_gate_decision.schema.yaml": "cdcb1826c8663cfbc3a2979ba325ba6a557f9219919c2f98bcb0bbac101ea80a",
    }
    _GCR001_Z1_EXEMPTIONS = frozenset(
        {
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/P0_FOUNDATION_AUDIT.yaml",
        }
    )
    _P1_CONTROLLER_AUTHORITY_BASE = "43a73495f1eb0ed469c807dcfe9d6fee2f77bd7e"
    _P1_CONTROLLER_AUTHORITY_BASE_TREE = "51b9075ac2b74f752ef678d46699e5490b3a6c3e"
    _P1_CONTROLLER_AUTHORITY = (
        "audit/P1_COMBINED_PATH_REPAIR_AND_S0B_AUTHORITY_20260830T092259Z.yaml"
    )
    _P1_CONTROLLER_AUTHORITY_COMMIT = "8eb6bbcdf42812536de985ca8a7b7bd5c4365937"
    _P1_CONTROLLER_AUTHORITY_SHA256 = (
        "218f1e10dd0aa177442cd25fdc4a4d828599e0c492299d219c7a0c3223596d74"
    )
    _P1_CONTROLLER_AUTHORITY_CONTENT_SHA256 = (
        "478c7d1320ddfbe26e9c0fb917335ca64280375fc915223db0478322c460bd38"
    )
    _P1_CONTROLLER_TRANSACTION = "a2a7c65a91f8381f26f24fbf159ba814719aeb28"
    _P1_CONTROLLER_PATHS = frozenset(
        {
            "MANIFEST.sha256",
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/P1_DESIGNER_STRUCTURED_MIGRATION.yaml",
        }
    )
    _P1_CONTROLLER_SHA256 = {
        "MANIFEST.sha256": "69585148ac57330a7a195fb39e11cc53f18bf05507fcda58a04e25113efa396d",
        "control/ACTIVE_PHASE.yaml": "38e8d6cf10b2ed873b47bb7f45375b2633bae26af112cc122f579f24b050282b",
        "control/PROGRAM_LEDGER.yaml": "dd7128db9130ef1263f930ab4e7db040d76845cce39feb80f61711d0cce414fc",
        "phases/P1_DESIGNER_STRUCTURED_MIGRATION.yaml": (
            "bc282031975d8e324aab35270e76140cef7bdcb45876bef3ae3583c2810d25b8"
        ),
    }
    _P1_GCR_CORRECTION_AUTHORITY_BASE = "efe20954a0a6e2b13552f7897bd65a35dd619390"
    _P1_GCR_CORRECTION_AUTHORITY_BASE_TREE = (
        "237125b396b2fe3b00bf1139ef29e6b323757562"
    )
    _P1_GCR_CORRECTION_AUTHORITY = (
        "audit/P1_GCR001_VALIDATOR_CORRECTION_AUTHORITY_20260831T135539Z.yaml"
    )
    _P1_GCR_CORRECTION_AUTHORITY_COMMIT = (
        "a6b9bf9f4f30ad6226447d87e90d5511b4c0f2bf"
    )
    _P1_GCR_CORRECTION_AUTHORITY_SHA256 = (
        "fcaa9d1cbbeb17156de41b8e04095228a6c9259814212c37667381bcf96eb56d"
    )
    _P1_GCR_CORRECTION_AUTHORITY_CONTENT_SHA256 = (
        "bdc214bfe21c5715a809d7513a3e1b71ef8de031211509a01b86e7f06ad9d053"
    )
    _P1_GCR_TEST_SCOPE_EXTENSION_CONTENT_SHA256 = (
        "ee4a78df10cdacdc0ce36c12f475900f9375d22b8ebefeea6567a0d61d6f764c"
    )
    _P1_GCR_CORRECTION_RESULT = (
        "audit/P1_GCR001_VALIDATOR_CORRECTION_RESULT_20260831T141411Z.yaml"
    )
    _P1_GCR_CORRECTION_PATHS = frozenset(
        {
            "src/ironman/governance.py",
            "tests/governance/test_p1_authorized_controller_transaction.py",
            "tests/governance/test_cold_start.py",
            "tests/governance/test_governance.py",
            "tests/governance/test_phase_gate_chain.py",
            _P1_GCR_CORRECTION_RESULT,
        }
    )

    def __init__(self, repository_root: Path):
        self.root = repository_root.resolve()
        self.contracts = ContractLoader(self.root / "schemas" / "ironman.schema.yaml")
        self.active = self._load_mapping("control/ACTIVE_PHASE.yaml")
        self.ledger = self._load_mapping("control/PROGRAM_LEDGER.yaml")
        self.phase_registry = self._load_mapping("phases/PHASE_REGISTRY.yaml")
        phase_relative = _string(self.active.get("phase_file"), "active.phase_file")
        self.phase = self._load_mapping(phase_relative)
        self.write_zones = self._load_mapping("control/WRITE_ZONES.yaml")
        self.invariants = self._load_mapping("control/SYSTEM_INVARIANTS.yaml")
        authority_paths = {
            "schemas/ironman.schema.yaml",
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/PHASE_REGISTRY.yaml",
            phase_relative,
            "control/WRITE_ZONES.yaml",
            "control/SYSTEM_INVARIANTS.yaml",
            "MANIFEST.sha256",
            ".codex/agents/governance-reviewer.toml",
        }
        # A validator is an immutable authority snapshot. Include every existing
        # source that can participate in phase/Gate interpretation, not just the
        # currently active pointers; any later mutation requires a fresh loader.
        for pattern in (
            "schemas/**/*.yaml",
            "control/**/*.yaml",
            "phases/**/*.yaml",
            "audit/**/*",
            "plans/active/*.md",
            "proposals/*.md",
            ".codex/agents/*.toml",
        ):
            authority_paths.update(
                path.relative_to(self.root).as_posix()
                for path in self.root.glob(pattern)
                if path.is_file()
            )
        self._authority_paths = tuple(sorted(authority_paths))
        self._authority_file_hashes = {
            relative: hashlib.sha256(
                self._repository_file(relative).read_bytes()
            ).hexdigest()
            for relative in self._authority_paths
        }
        self._authority_object_snapshots = {
            "active": copy.deepcopy(self.active),
            "ledger": copy.deepcopy(self.ledger),
            "phase_registry": copy.deepcopy(self.phase_registry),
            "phase": copy.deepcopy(self.phase),
            "write_zones": copy.deepcopy(self.write_zones),
            "invariants": copy.deepcopy(self.invariants),
            "canonical_schema": copy.deepcopy(self.contracts.raw_schema),
        }
        self._authority_state_cache: (
            tuple[tuple[str, str], _PhaseAuthorityState] | None
        ) = None
        self._phase_gate_paths_cache: (
            tuple[tuple[str, str], tuple[str, ...]] | None
        ) = None
        # Full object IDs are content-addressed and immutable. These caches only
        # avoid repeating identical Git object queries inside one validator;
        # every HEAD-sensitive cache key includes the resolved HEAD OID.
        self._full_commit_cache: dict[str, str] = {}
        self._commit_parents_cache: dict[str, tuple[str, ...]] = {}
        self._tree_oid_cache: dict[str, str] = {}
        self._unique_add_commit_cache: dict[tuple[str, str], str] = {}
        self._changed_entries_cache: dict[
            tuple[str, str], tuple[tuple[str, str], ...]
        ] = {}
        self._blob_cache: dict[tuple[str, str], tuple[str, bytes, str]] = {}
        self._first_parent_ancestor_cache: dict[tuple[str, str], bool] = {}
        self._path_untouched_cache: dict[tuple[str, str, str], bool] = {}
        self._path_touch_cache: dict[
            tuple[str, str, str], tuple[str, ...]
        ] = {}

    @classmethod
    def from_root(cls, repository_root: str | Path) -> GovernanceValidator:
        return cls(Path(repository_root))

    def _load_mapping(self, relative_path: str) -> dict[str, Any]:
        path = self.root / _normalize_relative_path(relative_path)
        return _mapping(ContractLoader.load_yaml(path), relative_path)

    def _assert_authority_snapshot_fresh(self) -> None:
        loaded = {
            "active": self.active,
            "ledger": self.ledger,
            "phase_registry": self.phase_registry,
            "phase": self.phase,
            "write_zones": self.write_zones,
            "invariants": self.invariants,
            "canonical_schema": self.contracts.raw_schema,
        }
        for name, expected in self._authority_object_snapshots.items():
            if loaded[name] != expected:
                raise GovernanceValidationError(
                    f"loaded authority object was mutated in memory: {name}"
                )
        for relative, expected_digest in self._authority_file_hashes.items():
            actual_digest = hashlib.sha256(
                self._repository_file(relative).read_bytes()
            ).hexdigest()
            if actual_digest != expected_digest:
                raise GovernanceValidationError(
                    f"authority file changed after validator initialization: {relative}"
                )

    def _repository_state_token(self) -> tuple[str, str]:
        """Bind a reusable validation result to one exact Git/worktree state."""

        head = self._git("rev-parse", "--verify", "HEAD^{commit}").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
            raise GovernanceValidationError("HEAD is not a full Git commit OID")
        status = self._git_bytes(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        return head, hashlib.sha256(status).hexdigest()

    def _head_oid(self) -> str:
        head = self._git("rev-parse", "--verify", "HEAD^{commit}").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
            raise GovernanceValidationError("HEAD is not a full Git commit OID")
        return head

    def _repository_file(self, relative_path: str) -> Path:
        normalized = _normalize_relative_path(relative_path)
        candidate = self.root / normalized
        try:
            resolved = candidate.resolve(strict=True)
            resolved_relative = resolved.relative_to(self.root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise GovernanceValidationError(
                f"repository artifact is missing or escapes the repository: {relative_path}"
            ) from exc
        if _path_identity(resolved_relative) != _path_identity(normalized):
            raise GovernanceValidationError(
                f"repository artifact path is redirected: {relative_path}"
            )
        if not resolved.is_file():
            raise GovernanceValidationError(
                f"repository artifact is not a regular file: {relative_path}"
            )
        return resolved

    @staticmethod
    def _yaml_mapping_from_bytes(payload: bytes, label: str) -> dict[str, Any]:
        try:
            body = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise GovernanceValidationError(f"invalid UTF-8 YAML for {label}: {exc}") from exc
        return _mapping(body, label)

    @staticmethod
    def _parse_manifest_bytes(payload: bytes, label: str) -> dict[str, str]:
        try:
            lines = payload.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise GovernanceValidationError(f"{label} is not UTF-8") from exc
        entries: dict[str, str] = {}
        identities: dict[str, str] = {}
        for number, line in enumerate(lines, start=1):
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                raise GovernanceValidationError(
                    f"invalid {label} entry at line {number}"
                )
            normalized = _normalize_relative_path(parts[1])
            identity = _path_identity(normalized)
            if identity in identities:
                raise GovernanceValidationError(
                    f"duplicate {label} path alias at line {number}: "
                    f"{normalized} conflicts with {identities[identity]}"
                )
            identities[identity] = normalized
            entries[normalized] = parts[0]
        if not entries:
            raise GovernanceValidationError(f"{label} is empty")
        return entries

    def _require_full_commit(self, oid: str, label: str) -> str:
        if not isinstance(oid, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid
        ):
            raise GovernanceValidationError(f"{label} must be a full lowercase Git OID")
        cached = self._full_commit_cache.get(oid)
        if cached is not None:
            return cached
        resolved = self._git("rev-parse", "--verify", f"{oid}^{{commit}}").strip()
        if resolved != oid:
            raise GovernanceValidationError(
                f"{label} does not resolve to the declared full commit: {oid}"
            )
        self._full_commit_cache[oid] = resolved
        return resolved

    def _commit_parents(self, oid: str) -> tuple[str, ...]:
        commit = self._require_full_commit(oid, "commit")
        cached = self._commit_parents_cache.get(commit)
        if cached is not None:
            return cached
        parts = self._git("rev-list", "--parents", "-n", "1", commit).split()
        if not parts or parts[0] != commit:
            raise GovernanceValidationError(f"cannot inspect commit parents: {commit}")
        parents = tuple(parts[1:])
        self._commit_parents_cache[commit] = parents
        return parents

    def _require_single_parent(self, oid: str, expected_parent: str, label: str) -> None:
        parents = self._commit_parents(oid)
        if parents != (expected_parent,):
            raise GovernanceValidationError(
                f"{label} must be a single-parent direct child of {expected_parent}; "
                f"got {parents}"
            )

    def _tree_oid(self, oid: str) -> str:
        commit = self._require_full_commit(oid, "tree commit")
        cached = self._tree_oid_cache.get(commit)
        if cached is not None:
            return cached
        tree = self._git("show", "-s", "--format=%T", commit).strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree):
            raise GovernanceValidationError(f"invalid tree OID for commit {commit}")
        self._tree_oid_cache[commit] = tree
        return tree

    def _unique_add_commit(self, relative_path: str) -> str:
        normalized = _normalize_relative_path(relative_path)
        head = self._head_oid()
        cache_key = (head, normalized)
        cached = self._unique_add_commit_cache.get(cache_key)
        if cached is not None:
            return cached
        commits = tuple(
            line.strip()
            for line in self._git(
                "log",
                "--first-parent",
                "--diff-filter=A",
                "--format=%H",
                "--",
                normalized,
            ).splitlines()
            if line.strip()
        )
        if len(commits) != 1:
            raise GovernanceValidationError(
                f"expected one first-add commit for {normalized}, got {commits}"
            )
        commit = self._require_full_commit(
            commits[0], f"first-add commit for {normalized}"
        )
        self._unique_add_commit_cache[cache_key] = commit
        return commit

    def _changed_entries(self, parent: str, child: str) -> dict[str, str]:
        parent_commit = self._require_full_commit(parent, "diff parent")
        child_commit = self._require_full_commit(child, "diff child")
        cache_key = (parent_commit, child_commit)
        cached = self._changed_entries_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        payload = self._git_bytes(
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            "--no-renames",
            parent_commit,
            child_commit,
        )
        parts = payload.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        if len(parts) % 2:
            raise GovernanceValidationError("unexpected NUL-delimited Git diff output")
        entries: dict[str, str] = {}
        identities: set[str] = set()
        for index in range(0, len(parts), 2):
            try:
                status = parts[index].decode("ascii")
                path = _normalize_relative_path(parts[index + 1].decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GovernanceValidationError("invalid path in Git diff output") from exc
            if status not in {"A", "M", "D"}:
                raise GovernanceValidationError(
                    f"unsupported Git change status {status} for {path}"
                )
            identity = _path_identity(path)
            if identity in identities:
                raise GovernanceValidationError(f"duplicate Git diff path alias: {path}")
            identities.add(identity)
            entries[path] = status
        self._changed_entries_cache[cache_key] = tuple(entries.items())
        return entries

    def _blob_at(self, commit: str, relative_path: str) -> tuple[str, bytes, str]:
        normalized = _normalize_relative_path(relative_path)
        commit_oid = self._require_full_commit(commit, f"blob commit for {normalized}")
        cache_key = (commit_oid, normalized)
        cached = self._blob_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            blob_oid = self._git("rev-parse", f"{commit_oid}:{normalized}").strip()
            object_type = self._git("cat-file", "-t", blob_oid).strip()
            payload = self._git_bytes("show", f"{commit_oid}:{normalized}")
        except GovernanceValidationError as exc:
            raise GovernanceValidationError(
                f"cannot load Git blob {commit_oid}:{normalized}"
            ) from exc
        if object_type != "blob" or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob_oid
        ):
            raise GovernanceValidationError(
                f"Git object is not a valid blob for {commit_oid}:{normalized}"
            )
        result = (blob_oid, payload, hashlib.sha256(payload).hexdigest())
        self._blob_cache[cache_key] = result
        return result

    def _is_first_parent_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor_oid = self._require_full_commit(ancestor, "ancestor")
        descendant_oid = self._require_full_commit(descendant, "descendant")
        cache_key = (ancestor_oid, descendant_oid)
        cached = self._first_parent_ancestor_cache.get(cache_key)
        if cached is not None:
            return cached
        chain = {
            line.strip()
            for line in self._git("rev-list", "--first-parent", descendant_oid).splitlines()
            if line.strip()
        }
        result = ancestor_oid in chain
        self._first_parent_ancestor_cache[cache_key] = result
        return result

    def _path_untouched_after(
        self, commit: str, relative_path: str, *, tip: str = "HEAD"
    ) -> bool:
        commit_oid = self._require_full_commit(commit, "path freeze commit")
        normalized = _normalize_relative_path(relative_path)
        tip_oid = self._head_oid() if tip == "HEAD" else self._require_full_commit(
            tip, "path freeze tip"
        )
        cache_key = (commit_oid, normalized, tip_oid)
        cached = self._path_untouched_cache.get(cache_key)
        if cached is not None:
            return cached
        changes = self._git(
            "log",
            "--first-parent",
            "--format=%H",
            f"{commit_oid}..{tip_oid}",
            "--",
            normalized,
        ).strip()
        result = not changes
        self._path_untouched_cache[cache_key] = result
        return result

    def _path_touch_commits(
        self, commit: str, relative_path: str, *, tip: str = "HEAD"
    ) -> tuple[str, ...]:
        commit_oid = self._require_full_commit(commit, "path history start")
        normalized = _normalize_relative_path(relative_path)
        tip_oid = self._head_oid() if tip == "HEAD" else self._require_full_commit(
            tip, "path history tip"
        )
        cache_key = (commit_oid, normalized, tip_oid)
        cached = self._path_touch_cache.get(cache_key)
        if cached is not None:
            return cached
        result = tuple(
            self._require_full_commit(line.strip(), f"touch commit for {normalized}")
            for line in self._git(
                "log",
                "--first-parent",
                "--reverse",
                "--format=%H",
                f"{commit_oid}..{tip_oid}",
                "--",
                normalized,
            ).splitlines()
            if line.strip()
        )
        self._path_touch_cache[cache_key] = result
        return result

    @property
    def allowed_paths(self) -> tuple[str, ...]:
        return _string_tuple(self.phase.get("allowed_paths"), "phase.allowed_paths")

    @property
    def forbidden_paths(self) -> tuple[str, ...]:
        return _string_tuple(self.phase.get("forbidden_paths"), "phase.forbidden_paths")

    def resolve_zones(self, path: str | Path) -> tuple[tuple[str, str], ...]:
        normalized = _normalize_relative_path(path)
        zones = _mapping(self.write_zones.get("zones"), "write_zones.zones")
        matches: list[tuple[str, str]] = []
        for zone_name, zone_value in zones.items():
            zone = _mapping(zone_value, f"write zone {zone_name}")
            for pattern in _string_tuple(zone.get("paths"), f"{zone_name}.paths"):
                if _matches(normalized, pattern):
                    matches.append((str(zone_name), pattern))
        return tuple(matches)

    def classify_zone(self, path: str | Path) -> tuple[str | None, str | None]:
        matches = self.resolve_zones(path)
        if len(matches) > 1:
            raise GovernanceValidationError(
                f"path matches multiple write zones: {path}: {matches}"
            )
        return matches[0] if matches else (None, None)

    def _validate_p1_authorized_controller_transaction(self) -> str:
        """Recognize the one exact, already authorized P1 controller commit."""

        authority_base = self._require_full_commit(
            self._P1_CONTROLLER_AUTHORITY_BASE,
            "P1 controller authority base",
        )
        if self._tree_oid(authority_base) != self._P1_CONTROLLER_AUTHORITY_BASE_TREE:
            raise GovernanceValidationError("P1 controller authority base tree mismatch")

        authority_commit = self._require_full_commit(
            self._P1_CONTROLLER_AUTHORITY_COMMIT,
            "P1 controller authority commit",
        )
        if self._unique_add_commit(self._P1_CONTROLLER_AUTHORITY) != authority_commit:
            raise GovernanceValidationError(
                "P1 controller authority is not uniquely bound to its approved commit"
            )
        self._require_single_parent(
            authority_commit,
            authority_base,
            "P1 controller authority commit",
        )
        if self._changed_entries(authority_base, authority_commit) != {
            self._P1_CONTROLLER_AUTHORITY: "A"
        }:
            raise GovernanceValidationError(
                "P1 controller authority commit contains changes outside its receipt"
            )

        authority_path = self._repository_file(self._P1_CONTROLLER_AUTHORITY)
        _, authority_payload, authority_digest = self._blob_at(
            authority_commit,
            self._P1_CONTROLLER_AUTHORITY,
        )
        if authority_digest != self._P1_CONTROLLER_AUTHORITY_SHA256:
            raise GovernanceValidationError("P1 controller authority digest mismatch")
        if authority_path.read_bytes() != authority_payload:
            raise GovernanceValidationError(
                "P1 controller authority worktree bytes differ from its commit"
            )
        if not self._path_untouched_after(
            authority_commit,
            self._P1_CONTROLLER_AUTHORITY,
        ):
            raise GovernanceValidationError("P1 controller authority was modified")

        authority = _mapping(
            self._yaml_mapping_from_bytes(
                authority_payload,
                "P1 controller authority receipt",
            ),
            "P1 controller authority receipt",
        )
        self.contracts.validate_instance("DecisionRecord", authority)
        if (
            authority.get("object_type") != "DecisionRecord"
            or authority.get("decision_id")
            != "P1_COMBINED_PATH_REPAIR_AND_S0B_20260830_01"
            or authority.get("decision_scope") != "governance"
            or authority.get("lifecycle_state") != "accepted"
            or authority.get("decided_by") != "owner_controller"
            or authority.get("content_hash")
            != self._P1_CONTROLLER_AUTHORITY_CONTENT_SHA256
        ):
            raise GovernanceValidationError(
                "P1 controller authority lacks the exact approved disposition"
            )
        selected_option = _string(
            authority.get("selected_option"),
            "P1 controller authority selected option",
        )
        if (
            hashlib.sha256(selected_option.encode("utf-8")).hexdigest()
            != self._P1_CONTROLLER_AUTHORITY_CONTENT_SHA256
        ):
            raise GovernanceValidationError(
                "P1 controller authority selected option hash mismatch"
            )
        required_provenance = {
            f"git:{authority_base}",
            f"tree:{self._P1_CONTROLLER_AUTHORITY_BASE_TREE}",
            f"sha256:{self._P1_CONTROLLER_AUTHORITY_CONTENT_SHA256}",
        }
        if not required_provenance <= set(authority.get("provenance_refs", ())):
            raise GovernanceValidationError(
                "P1 controller authority lacks exact base and content provenance"
            )

        transaction_commit = self._require_full_commit(
            self._P1_CONTROLLER_TRANSACTION,
            "P1 authorized controller transaction",
        )
        self._require_single_parent(
            transaction_commit,
            authority_commit,
            "P1 authorized controller transaction",
        )
        if not self._is_first_parent_ancestor(transaction_commit, self._head_oid()):
            raise GovernanceValidationError(
                "P1 authorized controller transaction is outside HEAD ancestry"
            )
        expected_changes = {
            relative: "M" for relative in self._P1_CONTROLLER_PATHS
        }
        actual_changes = self._changed_entries(authority_commit, transaction_commit)
        if actual_changes != expected_changes:
            raise GovernanceValidationError(
                "P1 authorized controller transaction diff mismatch: "
                f"{actual_changes}"
            )
        for relative in sorted(self._P1_CONTROLLER_PATHS):
            _, _, digest = self._blob_at(transaction_commit, relative)
            if digest != self._P1_CONTROLLER_SHA256[relative]:
                raise GovernanceValidationError(
                    "P1 authorized controller postimage digest mismatch: "
                    f"{relative}"
                )
        return transaction_commit

    def _validate_p1_gcr001_correction_scope(self) -> frozenset[str]:
        """Bind the correction to its exact receipt and one direct-child commit."""

        authority_base = self._require_full_commit(
            self._P1_GCR_CORRECTION_AUTHORITY_BASE,
            "P1 GCR001 correction authority base",
        )
        if (
            self._tree_oid(authority_base)
            != self._P1_GCR_CORRECTION_AUTHORITY_BASE_TREE
        ):
            raise GovernanceValidationError(
                "P1 GCR001 correction authority base tree mismatch"
            )
        authority_commit = self._require_full_commit(
            self._P1_GCR_CORRECTION_AUTHORITY_COMMIT,
            "P1 GCR001 correction authority commit",
        )
        if self._unique_add_commit(self._P1_GCR_CORRECTION_AUTHORITY) != authority_commit:
            raise GovernanceValidationError(
                "P1 GCR001 correction authority is not uniquely bound to its commit"
            )
        self._require_single_parent(
            authority_commit,
            authority_base,
            "P1 GCR001 correction authority commit",
        )
        if self._changed_entries(authority_base, authority_commit) != {
            self._P1_GCR_CORRECTION_AUTHORITY: "A"
        }:
            raise GovernanceValidationError(
                "P1 GCR001 correction authority commit contains an extra delta"
            )

        authority_path = self._repository_file(self._P1_GCR_CORRECTION_AUTHORITY)
        _, authority_payload, authority_digest = self._blob_at(
            authority_commit,
            self._P1_GCR_CORRECTION_AUTHORITY,
        )
        if authority_digest != self._P1_GCR_CORRECTION_AUTHORITY_SHA256:
            raise GovernanceValidationError(
                "P1 GCR001 correction authority digest mismatch"
            )
        if authority_path.read_bytes() != authority_payload:
            raise GovernanceValidationError(
                "P1 GCR001 correction authority worktree bytes differ from its commit"
            )
        if not self._path_untouched_after(
            authority_commit,
            self._P1_GCR_CORRECTION_AUTHORITY,
        ):
            raise GovernanceValidationError(
                "P1 GCR001 correction authority was modified"
            )
        authority = _mapping(
            self._yaml_mapping_from_bytes(
                authority_payload,
                "P1 GCR001 correction authority receipt",
            ),
            "P1 GCR001 correction authority receipt",
        )
        self.contracts.validate_instance("DecisionRecord", authority)
        if (
            authority.get("object_type") != "DecisionRecord"
            or authority.get("decision_id")
            != "P1_GCR001_VALIDATOR_CORRECTION_20260831_01"
            or authority.get("decision_scope") != "governance"
            or authority.get("lifecycle_state") != "accepted"
            or authority.get("decided_by") != "owner_controller"
            or authority.get("content_hash")
            != self._P1_GCR_CORRECTION_AUTHORITY_CONTENT_SHA256
        ):
            raise GovernanceValidationError(
                "P1 GCR001 correction authority lacks the approved disposition"
            )
        selected_option = _string(
            authority.get("selected_option"),
            "P1 GCR001 correction authority selected option",
        )
        if (
            hashlib.sha256(selected_option.encode("utf-8")).hexdigest()
            != self._P1_GCR_CORRECTION_AUTHORITY_CONTENT_SHA256
        ):
            raise GovernanceValidationError(
                "P1 GCR001 correction authority selected option hash mismatch"
            )
        required_provenance = {
            f"git:{authority_base}",
            f"tree:{self._P1_GCR_CORRECTION_AUTHORITY_BASE_TREE}",
            f"sha256:{self._P1_GCR_CORRECTION_AUTHORITY_CONTENT_SHA256}",
        }
        if not required_provenance <= set(authority.get("provenance_refs", ())):
            raise GovernanceValidationError(
                "P1 GCR001 correction authority lacks exact provenance"
            )

        successors = tuple(
            line.strip()
            for line in self._git(
                "rev-list",
                "--first-parent",
                "--reverse",
                f"{authority_commit}..HEAD",
            ).splitlines()
            if line.strip()
        )
        if not successors:
            tracked = {
                _normalize_relative_path(path)
                for path in self._git(
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "--diff-filter=ACMDRTUXB",
                    "-z",
                    "HEAD",
                    "--",
                ).split("\0")
                if path
            }
            untracked = {
                _normalize_relative_path(path)
                for path in self._git(
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ).split("\0")
                if path and not path.startswith(".git/")
            }
            ignored = tuple(
                path
                for path in self._git(
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "-z",
                ).split("\0")
                if path and not path.startswith(".git/")
            )
            pending_paths = tracked | untracked
            if ignored:
                raise GovernanceValidationError(
                    "P1 GCR001 correction has ignored worktree projections"
                )
            if pending_paths != self._P1_GCR_CORRECTION_PATHS:
                raise GovernanceValidationError(
                    "P1 GCR001 correction pending scope mismatch: "
                    f"{sorted(pending_paths)}"
                )
            pending_result = _mapping(
                self._yaml_mapping_from_bytes(
                    self._repository_file(
                        self._P1_GCR_CORRECTION_RESULT
                    ).read_bytes(),
                    "pending P1 GCR001 correction result",
                ),
                "pending P1 GCR001 correction result",
            )
            self.contracts.validate_instance("AuditEvent", pending_result)
            if (
                pending_result.get("content_hash")
                != self._P1_GCR_TEST_SCOPE_EXTENSION_CONTENT_SHA256
                or f"sha256:{self._P1_GCR_TEST_SCOPE_EXTENSION_CONTENT_SHA256}"
                not in pending_result.get("provenance_refs", ())
            ):
                raise GovernanceValidationError(
                    "P1 GCR001 correction result lacks the test-scope extension binding"
                )
            return self._P1_GCR_CORRECTION_PATHS

        correction_commit = self._require_full_commit(
            successors[0],
            "P1 GCR001 correction transaction",
        )
        self._require_single_parent(
            correction_commit,
            authority_commit,
            "P1 GCR001 correction transaction",
        )
        expected_changes = {
            "src/ironman/governance.py": "M",
            "tests/governance/test_p1_authorized_controller_transaction.py": "A",
            "tests/governance/test_cold_start.py": "M",
            "tests/governance/test_governance.py": "M",
            "tests/governance/test_phase_gate_chain.py": "M",
            self._P1_GCR_CORRECTION_RESULT: "A",
        }
        actual_changes = self._changed_entries(authority_commit, correction_commit)
        if actual_changes != expected_changes:
            raise GovernanceValidationError(
                "P1 GCR001 correction transaction diff mismatch: "
                f"{actual_changes}"
            )
        for relative in sorted(self._P1_GCR_CORRECTION_PATHS):
            expected_payload = self._blob_at(correction_commit, relative)[1]
            if self._repository_file(relative).read_bytes() != expected_payload:
                raise GovernanceValidationError(
                    "P1 GCR001 correction worktree bytes differ from its commit: "
                    f"{relative}"
                )
            if not self._path_untouched_after(correction_commit, relative):
                raise GovernanceValidationError(
                    "P1 GCR001 correction path changed after its bounded commit: "
                    f"{relative}"
                )

        result = _mapping(
            self._yaml_mapping_from_bytes(
                self._blob_at(
                    correction_commit,
                    self._P1_GCR_CORRECTION_RESULT,
                )[1],
                "P1 GCR001 correction result",
            ),
            "P1 GCR001 correction result",
        )
        self.contracts.validate_instance("AuditEvent", result)
        if (
            result.get("object_type") != "AuditEvent"
            or result.get("event_type") != "other"
            or result.get("lifecycle_state") != "accepted"
            or result.get("actor") != "codex_p1_gcr001_executor"
            or result.get("content_hash")
            != self._P1_GCR_TEST_SCOPE_EXTENSION_CONTENT_SHA256
            or result.get("previous_state_ref")
            != "phase:P1_DESIGNER_STRUCTURED_MIGRATION:active"
            or result.get("new_state_ref")
            != "phase:P1_DESIGNER_STRUCTURED_MIGRATION:active"
        ):
            raise GovernanceValidationError(
                "P1 GCR001 correction result lacks the exact no-state-effect outcome"
            )
        required_targets = {
            "git:a2a7c65a91f8381f26f24fbf159ba814719aeb28",
            "src/ironman/governance.py",
            "tests/governance/test_p1_authorized_controller_transaction.py",
            "tests/governance/test_cold_start.py",
            "tests/governance/test_governance.py",
            "tests/governance/test_phase_gate_chain.py",
        }
        if not required_targets <= set(result.get("target_refs", ())):
            raise GovernanceValidationError(
                "P1 GCR001 correction result lacks exact transaction targets"
            )
        if (
            f"sha256:{self._P1_GCR_TEST_SCOPE_EXTENSION_CONTENT_SHA256}"
            not in result.get("provenance_refs", ())
        ):
            raise GovernanceValidationError(
                "P1 GCR001 correction result lacks test-scope extension provenance"
            )
        return self._P1_GCR_CORRECTION_PATHS

    def _validate_gcr001_controller_transaction(
        self,
        transition_commits: tuple[str, ...] = (),
        *,
        _environment_validated: bool = False,
    ) -> _ControllerTransaction:
        if not _environment_validated:
            self._validate_git_inspection_environment()
        phase_files = _mapping(
            self.phase_registry.get("phase_files"), "phase_registry.phase_files"
        )
        p0_relative = _string(
            phase_files.get("P0_FOUNDATION_AUDIT"), "P0 Phase Card path"
        )
        p0 = self.phase if self.phase.get("phase_id") == "P0_FOUNDATION_AUDIT" else self._load_mapping(p0_relative)
        baseline = _mapping(p0.get("baseline"), "P0 baseline")
        repair_base = _string(
            baseline.get("gate_closure_repair_base_commit"),
            "P0 gate closure repair base",
        )
        if repair_base != self._GCR001_REPAIR_BASE:
            raise GovernanceValidationError(
                f"GCR001 repair base mismatch: {repair_base}"
            )
        authority_ref = _string(
            baseline.get("gate_closure_authority_ref"),
            "P0 gate closure authority ref",
        )
        if authority_ref != self._GCR001_AUTHORITY:
            raise GovernanceValidationError(
                f"GCR001 authority ref mismatch: {authority_ref}"
            )

        founding = self._founding_baseline_commit()
        if founding != self._GCR001_FOUNDING_ANCHOR:
            raise GovernanceValidationError(
                f"GCR001 founding anchor mismatch: {founding}"
            )
        self._require_full_commit(repair_base, "GCR001 repair base")

        proposal_path = self._repository_file(self._GCR001_PROPOSAL)
        proposal_commit = self._unique_add_commit(self._GCR001_PROPOSAL)
        _, proposal_payload, proposal_digest = self._blob_at(
            proposal_commit, self._GCR001_PROPOSAL
        )
        if proposal_path.read_bytes() != proposal_payload:
            raise GovernanceValidationError("GCR001 proposal worktree bytes changed")
        if proposal_digest != self._GCR001_PROPOSAL_SHA256:
            raise GovernanceValidationError(
                f"GCR001 proposal content hash mismatch: {proposal_digest}"
            )
        self._require_single_parent(
            proposal_commit, repair_base, "GCR001 proposal commit"
        )
        if self._changed_entries(repair_base, proposal_commit) != {
            self._GCR001_PROPOSAL: "A"
        }:
            raise GovernanceValidationError(
                "GCR001 proposal commit contains changes outside the proposal"
            )
        if not self._path_untouched_after(proposal_commit, self._GCR001_PROPOSAL):
            raise GovernanceValidationError("GCR001 proposal changed after approval")

        authority_path = self._repository_file(authority_ref)
        authority_commit = self._unique_add_commit(authority_ref)
        _, authority_payload, _ = self._blob_at(authority_commit, authority_ref)
        if authority_path.read_bytes() != authority_payload:
            raise GovernanceValidationError(
                "GCR001 authority worktree bytes differ from its commit"
            )
        authority = _mapping(
            self._yaml_mapping_from_bytes(authority_payload, "GCR001 authority receipt"),
            "GCR001 authority receipt",
        )
        self.contracts.validate_instance("DecisionRecord", authority)
        if (
            authority.get("object_type") != "DecisionRecord"
            or authority.get("decision_id") != self._GCR001_ID
            or authority.get("decision_scope") != "governance"
            or authority.get("lifecycle_state") != "accepted"
            or authority.get("decided_by") != "owner_controller"
            or authority.get("content_ref") != self._GCR001_PROPOSAL
            or authority.get("content_hash") != self._GCR001_PROPOSAL_SHA256
        ):
            raise GovernanceValidationError(
                "GCR001 authority receipt lacks the approved canonical disposition"
            )
        self._require_single_parent(
            authority_commit, proposal_commit, "GCR001 authority commit"
        )
        if self._changed_entries(proposal_commit, authority_commit) != {
            authority_ref: "A"
        }:
            raise GovernanceValidationError(
                "GCR001 authority commit contains changes outside the receipt"
            )
        required_provenance = {
            f"git:{founding}",
            f"git:{repair_base}",
            f"git:{proposal_commit}",
            f"sha256:{self._GCR001_PROPOSAL_SHA256}",
        }
        provenance = set(authority.get("provenance_refs", ()))
        if not required_provenance <= provenance:
            raise GovernanceValidationError(
                "GCR001 authority receipt lacks required Git/content provenance"
            )
        if f"git:{authority_commit}" in provenance:
            raise GovernanceValidationError(
                "GCR001 authority receipt contains a self-referential commit"
            )
        if not self._path_untouched_after(authority_commit, authority_ref):
            raise GovernanceValidationError("GCR001 authority receipt was modified")

        successors = tuple(
            line.strip()
            for line in self._git(
                "rev-list",
                "--first-parent",
                "--reverse",
                f"{authority_commit}..HEAD",
            ).splitlines()
            if line.strip()
        )
        if not successors:
            raise GovernanceValidationError(
                "GCR001 controller transaction is absent after the authority receipt"
            )
        transaction_commit = self._require_full_commit(
            successors[0], "GCR001 controller transaction"
        )
        self._require_single_parent(
            transaction_commit, authority_commit, "GCR001 controller transaction"
        )
        expected_changes = {
            "MANIFEST.sha256": "M",
            "control/ACTIVE_PHASE.yaml": "M",
            "control/PROGRAM_LEDGER.yaml": "M",
            "phases/P0_FOUNDATION_AUDIT.yaml": "M",
            "schemas/ironman.schema.yaml": "M",
            "schemas/phase_gate_decision.schema.yaml": "A",
        }
        actual_changes = self._changed_entries(authority_commit, transaction_commit)
        if actual_changes != expected_changes:
            raise GovernanceValidationError(
                f"GCR001 controller transaction diff mismatch: {actual_changes}"
            )

        blobs: dict[str, bytes] = {}
        for relative in sorted(self._GCR001_G_PATHS):
            _, payload, digest = self._blob_at(transaction_commit, relative)
            if digest != self._GCR001_G_SHA256[relative]:
                raise GovernanceValidationError(
                    f"GCR001 approved G blob digest mismatch: {relative}"
                )
            blobs[relative] = payload

        p0_before = self._yaml_mapping_from_bytes(
            self._blob_at(authority_commit, p0_relative)[1], "pre-G P0 Phase Card"
        )
        p0_after = self._yaml_mapping_from_bytes(
            blobs[p0_relative], "post-G P0 Phase Card"
        )
        expected_p0 = copy.deepcopy(p0_before)
        expected_baseline = _mapping(expected_p0.get("baseline"), "pre-G P0 baseline")
        expected_baseline.update(
            {
                "gate_closure_repair_base_commit": self._GCR001_REPAIR_BASE,
                "gate_closure_authority_ref": self._GCR001_AUTHORITY,
            }
        )
        allowed_additions = [
            "plans/active/P0_GATE_CLOSURE_GCR001.md",
            "schemas/ironman.schema.yaml",
            "schemas/phase_gate_decision.schema.yaml",
            "MANIFEST.sha256",
        ]
        expected_p0["allowed_paths"] = list(
            _string_tuple(p0_before.get("allowed_paths"), "pre-G P0 allowed_paths")
        ) + allowed_additions
        expected_p0["required_deliverables"] = list(
            _string_tuple(
                p0_before.get("required_deliverables"),
                "pre-G P0 required_deliverables",
            )
        ) + [
            "truthful prospective P0 Gate-closure ExecPlan at "
            "plans/active/P0_GATE_CLOSURE_GCR001.md",
            "additive canonical PhaseGateDecision contract and thin wrapper",
            "exact controller-transaction recognition and protected-byte freezing "
            "derived from repair base R0",
            "artifact path, raw SHA-256, Git blob OID, containing-commit, and "
            "first-parent ancestry validation",
            "non-self-referential future transition-baseline derivation rule",
            "executor, verifier, Gate reviewer, and controller actor separation "
            "with resolvable authority references",
            "regression test preventing active Phase Card and required ExecPlan "
            "path conflicts",
        ]
        expected_p0["explicit_non_goals"] = list(
            _string_tuple(
                p0_before.get("explicit_non_goals"),
                "pre-G P0 explicit_non_goals",
            )
        ) + ["P1 activation or P1 implementation"]
        expected_p0["schema_changes"] = [
            "schemas/ironman.schema.yaml",
            "schemas/phase_gate_decision.schema.yaml",
        ]
        expected_p0["migration"] = [
            "additive v1.2 PhaseGateDecision contract; existing v1.1 audit "
            "artifacts remain valid and immutable"
        ]
        expected_p0["rollback"] = list(
            _string_tuple(p0_before.get("rollback"), "pre-G P0 rollback")
        ) + [
            "revert G atomically and restore the exact pre-G P0 Phase Card bytes, "
            "both prior phase-hash bindings, the v1.1 canonical schema, absence "
            "of the new wrapper, and the prior MANIFEST"
        ]
        expected_p0["quantitative_exit_conditions"] = list(
            _string_tuple(
                p0_before.get("quantitative_exit_conditions"),
                "pre-G P0 quantitative_exit_conditions",
            )
        ) + [
            "authorized prospective ExecPlan exists at "
            "plans/active/P0_GATE_CLOSURE_GCR001.md and truthfully marks pre-G "
            "work as historical",
            "canonical definition and wrapper parity is 36/36 including "
            "PhaseGateDecision",
            "exact controller transaction G is derived from R0, frozen, and "
            "validated with no altered, extra, merged, duplicated, or "
            "non-ancestral controller changes",
            "Gate artifacts pass repository-path, raw SHA-256, Git blob OID, "
            "containing-commit, and first-parent ancestry validation",
            "future transition baseline is derived without self-referential "
            "commit identifiers",
            "executor, verifier, Gate reviewer, and controller actors are "
            "distinct and all authority references resolve",
            "every active Phase Card requiring an ExecPlan authorizes its exact "
            "plan path",
        ]
        expected_p0["audit_requirements"] = list(
            _string_tuple(
                p0_before.get("audit_requirements"),
                "pre-G P0 audit_requirements",
            )
        ) + [
            "preserve existing partial, blocked, and Gate artifacts as immutable "
            "historical evidence",
            "bind GCR001 remediation provenance to R0, the authority receipt, and "
            "the exact controller transaction G",
            "create only superseding closeout evidence and do not apply P1 authority",
        ]
        if p0_after != expected_p0:
            raise GovernanceValidationError(
                "GCR001 P0 Phase Card differs from the exact approved semantic delta"
            )

        phase_digest = hashlib.sha256(blobs[p0_relative]).hexdigest()
        active_after = self._yaml_mapping_from_bytes(
            blobs["control/ACTIVE_PHASE.yaml"], "post-G Active Phase"
        )
        ledger_after = self._yaml_mapping_from_bytes(
            blobs["control/PROGRAM_LEDGER.yaml"], "post-G Program Ledger"
        )
        active_before = self._yaml_mapping_from_bytes(
            self._blob_at(authority_commit, "control/ACTIVE_PHASE.yaml")[1],
            "pre-G Active Phase",
        )
        expected_active = copy.deepcopy(active_before)
        expected_active["version"] = 3
        expected_active["phase_sha256"] = phase_digest
        if active_after != expected_active:
            raise GovernanceValidationError("GCR001 Active Phase binding is invalid")
        ledger_phases = _mapping(ledger_after.get("phases"), "post-G ledger phases")
        p0_ledger = _mapping(ledger_phases.get("P0_FOUNDATION_AUDIT"), "post-G P0 ledger")
        p1_ledger = _mapping(
            ledger_phases.get("P1_DESIGNER_STRUCTURED_MIGRATION"),
            "post-G P1 ledger",
        )
        ledger_before = self._yaml_mapping_from_bytes(
            self._blob_at(authority_commit, "control/PROGRAM_LEDGER.yaml")[1],
            "pre-G Program Ledger",
        )
        expected_ledger = copy.deepcopy(ledger_before)
        expected_ledger["version"] = 3
        expected_ledger["pack_version"] = "1.2.0"
        expected_ledger["active_phase_sha256"] = phase_digest
        expected_ledger["accepted_architecture_decisions"] = [authority_ref]
        expected_ledger["known_blockers"] = [
            "P0 Gate closure remains pending prospective ExecPlan completion, "
            "exact controller-transaction validation, a complete superseding "
            "ExecutionReport, independent PASS verification, independent Gate "
            "review, and an explicit controller PhaseGateDecision"
        ]
        if ledger_after != expected_ledger:
            raise GovernanceValidationError(
                "GCR001 Program Ledger state or P0/P1 lock is invalid"
            )

        schema_before = self._yaml_mapping_from_bytes(
            self._blob_at(authority_commit, "schemas/ironman.schema.yaml")[1],
            "pre-G canonical schema",
        )
        schema_after = self._yaml_mapping_from_bytes(
            blobs["schemas/ironman.schema.yaml"], "post-G canonical schema"
        )
        defs_before = _mapping(schema_before.get("$defs"), "pre-G definitions")
        defs_after = _mapping(schema_after.get("$defs"), "post-G definitions")
        if set(defs_after) != set(defs_before) | {"PhaseGateDecision"}:
            raise GovernanceValidationError(
                "GCR001 canonical schema change is not one additive definition"
            )
        if any(defs_after[name] != value for name, value in defs_before.items()):
            raise GovernanceValidationError(
                "GCR001 changed the meaning of an existing canonical definition"
            )
        if set(schema_after) != set(schema_before):
            raise GovernanceValidationError(
                "GCR001 canonical schema top-level fields changed"
            )
        for key, value in schema_before.items():
            if key not in {"$defs", "description"} and schema_after.get(key) != value:
                raise GovernanceValidationError(
                    f"GCR001 changed an unapproved canonical schema field: {key}"
                )
        if schema_after.get("description") != (
            "Canonical machine-readable contracts for IRONMAN v1.2. All per-object "
            "wrapper schemas reference this file."
        ):
            raise GovernanceValidationError("GCR001 canonical schema is not v1.2")
        wrapper_after = self._yaml_mapping_from_bytes(
            blobs["schemas/phase_gate_decision.schema.yaml"],
            "PhaseGateDecision wrapper",
        )
        if wrapper_after != {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://local.ironman/schemas/phase_gate_decision.schema.yaml",
            "$ref": "ironman.schema.yaml#/$defs/PhaseGateDecision",
        }:
            raise GovernanceValidationError(
                "GCR001 PhaseGateDecision wrapper is not thin and exact"
            )

        manifest_before = self._parse_manifest_bytes(
            self._blob_at(authority_commit, "MANIFEST.sha256")[1],
            "pre-G MANIFEST.sha256",
        )
        manifest_after = self._parse_manifest_bytes(
            blobs["MANIFEST.sha256"], "post-G MANIFEST.sha256"
        )
        wrapper_ref = "schemas/phase_gate_decision.schema.yaml"
        if set(manifest_after) != set(manifest_before) | {wrapper_ref}:
            raise GovernanceValidationError(
                "GCR001 MANIFEST path set changed beyond the new wrapper"
            )
        rebound_paths = self._GCR001_G_PATHS - {"MANIFEST.sha256"}
        for relative, digest in manifest_before.items():
            if relative not in rebound_paths and manifest_after.get(relative) != digest:
                raise GovernanceValidationError(
                    f"GCR001 changed an unrelated MANIFEST entry: {relative}"
                )
        for relative in rebound_paths:
            expected_digest = hashlib.sha256(blobs[relative]).hexdigest()
            if manifest_after.get(relative) != expected_digest:
                raise GovernanceValidationError(
                    f"GCR001 MANIFEST does not bind post-G bytes: {relative}"
                )
        if "MANIFEST.sha256" in manifest_after:
            raise GovernanceValidationError("MANIFEST.sha256 must not hash itself")
        manifest_paths = list(manifest_after)
        if manifest_paths != sorted(manifest_paths):
            raise GovernanceValidationError("GCR001 MANIFEST paths are not lexical")

        p1_controller_commit = self._validate_p1_authorized_controller_transaction()
        correction_paths = self._validate_p1_gcr001_correction_scope()

        transition_g_paths = {
            "MANIFEST.sha256",
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/P0_FOUNDATION_AUDIT.yaml",
        }
        if not transition_commits:
            # Direct structural callers do not have the phase-authority result that
            # normally supplies transition commits. Derive only the immutable
            # pre-authority segment ending at the exact approved base; this cannot
            # admit a later or future protected-path touch.
            transition_commits = self._path_touch_commits(
                transaction_commit,
                "MANIFEST.sha256",
                tip=self._P1_CONTROLLER_AUTHORITY_BASE,
            )
        normalized_transitions = tuple(
            self._require_full_commit(commit, "canonical controller transition")
            for commit in transition_commits
        )
        for transition_index, transition_commit in enumerate(normalized_transitions):
            parents = self._commit_parents(transition_commit)
            if len(parents) != 1:
                raise GovernanceValidationError(
                    "canonical controller transition must have one parent"
                )
            transition_changes = self._changed_entries(parents[0], transition_commit)
            actual_g_changes = {
                path: status
                for path, status in transition_changes.items()
                if path in self._GCR001_G_PATHS
            }
            expected_g_changes = {
                path: "M"
                for path in (
                    transition_g_paths
                    if transition_index == 0
                    else transition_g_paths - {"phases/P0_FOUNDATION_AUDIT.yaml"}
                )
            }
            if actual_g_changes != expected_g_changes:
                raise GovernanceValidationError(
                    "canonical transition changed the wrong GCR001 paths: "
                    f"{actual_g_changes}"
                )
        if not normalized_transitions:
            raise GovernanceValidationError(
                "P1 authorized controller transaction lacks the preceding transition"
            )
        if not self._is_first_parent_ancestor(
            normalized_transitions[0],
            p1_controller_commit,
        ):
            raise GovernanceValidationError(
                "P1 authorized controller transaction does not follow the P1 transition"
            )
        if len(normalized_transitions) > 1 and not self._is_first_parent_ancestor(
            p1_controller_commit,
            normalized_transitions[1],
        ):
            raise GovernanceValidationError(
                "P1 authorized controller transaction is not before the next transition"
            )

        expected_state_touches: dict[str, tuple[str, ...]] = {
            relative: () for relative in self._GCR001_G_PATHS
        }
        for relative in (
            "MANIFEST.sha256",
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/PHASE_REGISTRY.yaml",
        ):
            expected_state_touches[relative] = normalized_transitions
        phase_order = _string_tuple(
            self.phase_registry.get("phase_order"), "phase_registry.phase_order"
        )
        for phase_index, phase_id in enumerate(phase_order):
            relative = _string(
                phase_files.get(phase_id), f"PhaseCard path for {phase_id}"
            )
            touches: list[str] = []
            if phase_index > 0 and phase_index - 1 < len(normalized_transitions):
                touches.append(normalized_transitions[phase_index - 1])
            if phase_index < len(normalized_transitions):
                touches.append(normalized_transitions[phase_index])
            expected_state_touches[relative] = tuple(touches)

        for relative in sorted(self._P1_CONTROLLER_PATHS):
            expected_touches = expected_state_touches.get(relative)
            if (
                expected_touches is None
                or not expected_touches
                or expected_touches[0] != normalized_transitions[0]
            ):
                raise GovernanceValidationError(
                    "P1 authorized controller path is outside the exact phase-state set: "
                    f"{relative}"
                )
            expected_state_touches[relative] = (
                expected_touches[:1]
                + (p1_controller_commit,)
                + expected_touches[1:]
            )

        for relative, expected_touches in sorted(expected_state_touches.items()):
            actual_touches = self._path_touch_commits(
                transaction_commit, relative
            )
            if actual_touches != expected_touches:
                raise GovernanceValidationError(
                    f"GCR001 frozen path has unauthorized post-G history: {relative}: "
                    f"{actual_touches}"
                )
            expected_commit = expected_touches[-1] if expected_touches else transaction_commit
            expected_payload = self._blob_at(expected_commit, relative)[1]
            if self._repository_file(relative).read_bytes() != expected_payload:
                raise GovernanceValidationError(
                    f"GCR001 frozen worktree bytes differ from authority: {relative}"
                )

        return _ControllerTransaction(
            transaction_id=self._GCR001_ID,
            repair_base=repair_base,
            proposal_commit=proposal_commit,
            authority_commit=authority_commit,
            transaction_commit=transaction_commit,
            frozen_paths=self._GCR001_G_PATHS,
            historical_exemptions=self._GCR001_G_PATHS,
        )

    def _validate_artifact_binding(
        self,
        binding_value: Any,
        role: str,
        decision_parent: str,
    ) -> tuple[str, bytes, str]:
        binding = _mapping(binding_value, f"{role} artifact binding")
        relative = _normalize_relative_path(
            _string(binding.get("artifact_ref"), f"{role}.artifact_ref")
        )
        containing_commit = self._require_full_commit(
            _string(binding.get("containing_commit"), f"{role}.containing_commit"),
            f"{role} containing commit",
        )
        if not self._is_first_parent_ancestor(containing_commit, decision_parent):
            raise GovernanceValidationError(
                f"{role} containing commit is outside the reviewed first-parent chain"
            )
        if not self._path_untouched_after(containing_commit, relative):
            raise GovernanceValidationError(
                f"{role} artifact was touched after its bound commit: {relative}"
            )
        blob_oid, payload, digest = self._blob_at(containing_commit, relative)
        if binding.get("blob_oid") != blob_oid:
            raise GovernanceValidationError(f"{role} Git blob OID mismatch")
        if binding.get("sha256") != digest:
            raise GovernanceValidationError(f"{role} raw SHA-256 mismatch")
        if self._repository_file(relative).read_bytes() != payload:
            raise GovernanceValidationError(
                f"{role} worktree bytes differ from the bound Git blob"
            )
        if role != "exec_plan" and self._unique_add_commit(relative) != containing_commit:
            raise GovernanceValidationError(
                f"{role} containing commit is not the artifact's unique first-add commit"
            )
        return relative, payload, containing_commit

    def _validate_phase_gate_decision(self, relative_path: str) -> _ValidatedGate:
        normalized = _normalize_relative_path(relative_path)
        if not normalized.startswith("audit/") or not normalized.endswith(".yaml"):
            raise GovernanceValidationError(
                "canonical PhaseGateDecision must be an audit/**/*.yaml artifact"
            )
        decision_commit = self._unique_add_commit(normalized)
        _, decision_payload, _ = self._blob_at(decision_commit, normalized)
        decision_file = self._repository_file(normalized)
        if decision_payload != decision_file.read_bytes():
            raise GovernanceValidationError(
                "Gate decision worktree bytes differ from the decision commit"
            )
        body = self._yaml_mapping_from_bytes(
            decision_payload, f"PhaseGateDecision {normalized}"
        )
        self.contracts.validate_instance("PhaseGateDecision", body)
        if body.get("object_type") != "PhaseGateDecision":
            raise GovernanceValidationError(
                f"Gate decision has the wrong object_type: {normalized}"
            )
        if body.get("lifecycle_state") != "accepted":
            raise GovernanceValidationError(
                f"Gate decision is not accepted: {normalized}"
            )
        gate_result = _string(body.get("gate_result"), "Gate result")
        if gate_result != "pass":
            raise GovernanceValidationError(
                "non-PASS PhaseGateDecision artifacts are non-authoritative audit "
                "records; only PASS decisions enter the runtime Gate chain"
            )
        phase_id = _string(body.get("phase_id"), "Gate phase_id")

        parents = self._commit_parents(decision_commit)
        if len(parents) != 1:
            raise GovernanceValidationError("Gate decision commit must have one parent")
        decision_parent = parents[0]
        if not self._path_untouched_after(decision_commit, normalized):
            raise GovernanceValidationError("Gate decision changed after its commit")
        decision_digest = hashlib.sha256(decision_payload).hexdigest()

        bindings = _mapping(body.get("artifact_bindings"), "Gate artifact bindings")
        plan_ref, plan_payload, implementation_commit = self._validate_artifact_binding(
            bindings.get("exec_plan"), "exec_plan", decision_parent
        )
        report_ref, report_payload, report_commit = self._validate_artifact_binding(
            bindings.get("execution_report"), "execution_report", decision_parent
        )
        verification_ref, verification_payload, verification_commit = (
            self._validate_artifact_binding(
                bindings.get("verification_result"),
                "verification_result",
                decision_parent,
            )
        )
        review_ref, review_payload, review_commit = self._validate_artifact_binding(
            bindings.get("gate_review"), "gate_review", decision_parent
        )

        registry_at_review = self._yaml_mapping_from_bytes(
            self._blob_at(review_commit, "phases/PHASE_REGISTRY.yaml")[1],
            "Phase Registry at Gate review",
        )
        phase_files_at_review = _mapping(
            registry_at_review.get("phase_files"), "Gate review phase files"
        )
        phase_file_at_review = _string(
            phase_files_at_review.get(phase_id), "Gate phase file"
        )
        phase_at_review = self._yaml_mapping_from_bytes(
            self._blob_at(review_commit, phase_file_at_review)[1],
            "Phase Card at Gate review",
        )
        required_deliverables = _string_tuple(
            phase_at_review.get("required_deliverables"),
            "Gate Phase Card required_deliverables",
        )
        acceptance_commands = _string_tuple(
            phase_at_review.get("acceptance_commands"),
            "Gate Phase Card acceptance_commands",
        )
        exit_conditions = _string_tuple(
            phase_at_review.get("quantitative_exit_conditions"),
            "Gate Phase Card quantitative_exit_conditions",
        )
        invariant_objective = _string(
            phase_at_review.get("invariant_objective"),
            "Gate Phase Card invariant_objective",
        )
        declared_plan_refs = {
            match.group(0)
            for deliverable in required_deliverables
            for match in re.finditer(
                r"plans/active/[A-Za-z0-9_.\-/]+\.md", deliverable
            )
        }
        if declared_plan_refs != {plan_ref}:
            raise GovernanceValidationError(
                "Gate decision does not bind the Phase Card's one exact ExecPlan"
            )
        self._validate_execplan_payload(plan_payload, plan_ref, phase_id)
        if phase_id == "P0_FOUNDATION_AUDIT":
            plan_text = plan_payload.decode("utf-8", errors="strict").casefold()
            if (
                plan_ref != "plans/active/P0_GATE_CLOSURE_GCR001.md"
                or "prospective" not in plan_text
                or "does not retroactively" not in plan_text
            ):
                raise GovernanceValidationError(
                    "bound P0 ExecPlan does not preserve the GCR001 non-retroactive "
                    "disposition"
                )

        report = self._yaml_mapping_from_bytes(report_payload, report_ref)
        verification = self._yaml_mapping_from_bytes(
            verification_payload, verification_ref
        )
        self.contracts.validate_instance("ExecutionReport", report)
        self.contracts.validate_instance("VerificationResult", verification)
        if (
            report.get("object_type") != "ExecutionReport"
            or report.get("phase_id") != phase_id
            or report.get("completion_state") != "complete"
            or report.get("lifecycle_state") != "accepted"
            or report.get("created_by") != body.get("executor_actor")
        ):
            raise GovernanceValidationError(
                "Gate-bound ExecutionReport is not a complete report by the declared executor"
            )
        report_commands = _string_tuple(
            report.get("commands_run"), "ExecutionReport commands_run"
        )
        report_task_ids = _string_tuple(
            report.get("task_ids"), "ExecutionReport task_ids"
        )
        report_test_results = _mapping(
            report.get("test_results"), "ExecutionReport test_results"
        )
        passing_report_results = all(
            value == "pass"
            or (isinstance(value, dict) and value.get("status") == "pass")
            for value in report_test_results.values()
        )
        if (
            len(report_commands) != len(set(report_commands))
            or not set(acceptance_commands) <= set(report_commands)
            or set(report_test_results) != set(report_commands)
            or not passing_report_results
        ):
            raise GovernanceValidationError(
                "Gate-bound ExecutionReport does not record every Phase Card "
                "acceptance command exactly once as PASS"
            )
        closeout_refs = _string_tuple(
            report.get("audit_refs", []), "ExecutionReport audit_refs"
        )
        if not closeout_refs:
            raise GovernanceValidationError(
                "complete ExecutionReport must bind at least one closeout AuditEvent"
            )
        normalized_closeout_refs: list[str] = []
        closeout_identities: set[str] = set()
        for closeout_ref_value in closeout_refs:
            closeout_ref = _normalize_relative_path(closeout_ref_value)
            identity = _path_identity(closeout_ref)
            if (
                identity in closeout_identities
                or not closeout_ref.startswith("audit/")
                or not closeout_ref.endswith(".yaml")
            ):
                raise GovernanceValidationError(
                    f"invalid or duplicate closeout AuditEvent ref: {closeout_ref}"
                )
            closeout_identities.add(identity)
            if self._unique_add_commit(closeout_ref) != report_commit:
                raise GovernanceValidationError(
                    "closeout AuditEvent was not first added with the ExecutionReport"
                )
            if not self._path_untouched_after(report_commit, closeout_ref):
                raise GovernanceValidationError(
                    f"closeout AuditEvent changed after C: {closeout_ref}"
                )
            _, closeout_payload, _ = self._blob_at(report_commit, closeout_ref)
            if self._repository_file(closeout_ref).read_bytes() != closeout_payload:
                raise GovernanceValidationError(
                    f"closeout AuditEvent worktree bytes differ from C: {closeout_ref}"
                )
            closeout = self._yaml_mapping_from_bytes(
                closeout_payload, f"closeout AuditEvent {closeout_ref}"
            )
            self.contracts.validate_instance("AuditEvent", closeout)
            if (
                closeout.get("object_type") != "AuditEvent"
                or closeout.get("event_type") != "phase_closeout"
                or closeout.get("lifecycle_state") != "accepted"
                or closeout.get("actor") != body.get("executor_actor")
                or closeout.get("created_by") != body.get("executor_actor")
                or f"phase:{phase_id}" not in closeout.get("target_refs", ())
                or report_ref not in closeout.get("evidence_refs", ())
            ):
                raise GovernanceValidationError(
                    f"invalid complete closeout AuditEvent: {closeout_ref}"
                )
            normalized_closeout_refs.append(closeout_ref)
        report_refs = {
            report_ref,
            report.get("object_id"),
            report.get("report_id"),
        }
        acceptance_results = verification.get("acceptance_test_results")
        expected_verification_ids = {
            *(f"command::{command}" for command in acceptance_commands),
            *(f"exit::{condition}" for condition in exit_conditions),
        }
        report_evidence_refs = {
            reference
            for reference in (
                report_ref,
                report.get("object_id"),
                report.get("report_id"),
                *report.get("artifacts", ()),
                *normalized_closeout_refs,
            )
            if isinstance(reference, str) and reference
        }
        all_acceptance_passed = (
            isinstance(acceptance_results, list)
            and len(acceptance_results) == len(expected_verification_ids)
            and {
                result.get("test_id")
                for result in acceptance_results
                if isinstance(result, dict)
            }
            == expected_verification_ids
            and all(
                isinstance(result, dict)
                and result.get("status") == "pass"
                and result.get("evidence_ref") in report_evidence_refs
                and isinstance(result.get("notes"), str)
                and bool(result["notes"].strip())
                for result in acceptance_results
            )
        )
        if (
            verification.get("object_type") != "VerificationResult"
            or verification.get("lifecycle_state") != "accepted"
            or verification.get("overall_status") != "pass"
            or verification.get("verifier_actor") != body.get("verifier_actor")
            or verification.get("created_by") != body.get("verifier_actor")
            or verification.get("task_id") not in report_task_ids
            or not all_acceptance_passed
            or verification.get("policy_violations") != []
            or report_ref not in verification.get("provenance_refs", ())
            or report_ref not in verification.get("artifact_refs", ())
            or not report_refs.intersection(verification.get("artifact_refs", ()))
            or not str(verification.get("independence_statement", "")).strip()
        ):
            raise GovernanceValidationError(
                "Gate-bound VerificationResult is not an independent PASS over the report"
            )

        review_text = review_payload.decode("utf-8", errors="strict")
        markers = re.findall(
            r"(?im)^[ \t]*GATE_RESULT:[ \t]*(PASS|BLOCKED|FAIL)[ \t]*$",
            review_text,
        )
        expected_marker = gate_result.upper()
        if markers != [expected_marker]:
            raise GovernanceValidationError(
                "Gate review must contain exactly one unambiguous machine-readable "
                f"GATE_RESULT marker for {expected_marker}; got {markers}"
            )
        review_lines = tuple(line.strip() for line in review_text.splitlines())
        required_review_topics = (
            "invariant objective status",
            "quantitative exit conditions",
            "forbidden-path/governance findings",
            "baseline comparison",
            "actual capability delta",
            "hmvo instrumentation/result",
            "unresolved risks and technical debt",
            "rollback readiness",
            "recommendation",
            "exact controller patch proposal",
        )
        review_folded = review_text.casefold()
        missing_topics = [
            topic for topic in required_review_topics if topic not in review_folded
        ]
        next_phase_id_for_review = _string(
            body.get("next_phase_id"), "Gate next_phase_id"
        )
        expected_machine_values = {
            "PHASE_ID": phase_id,
            "INVARIANT_OBJECTIVE_SHA256": hashlib.sha256(
                invariant_objective.encode("utf-8")
            ).hexdigest(),
            "INVARIANT_OBJECTIVE_RESULT": "PASS",
            "IMPLEMENTATION_COMMIT": implementation_commit,
            "IMPLEMENTATION_TREE": self._tree_oid(implementation_commit),
            "EXECUTION_REPORT_REF": report_ref,
            "EXECUTION_REPORT_COMMIT": report_commit,
            "EXECUTION_REPORT_TREE": self._tree_oid(report_commit),
            "EXECUTION_REPORT_SHA256": hashlib.sha256(report_payload).hexdigest(),
            "VERIFICATION_RESULT_REF": verification_ref,
            "VERIFICATION_RESULT_COMMIT": verification_commit,
            "VERIFICATION_RESULT_TREE": self._tree_oid(verification_commit),
            "VERIFICATION_RESULT_SHA256": hashlib.sha256(
                verification_payload
            ).hexdigest(),
            "QUANTITATIVE_EXIT_CONDITIONS_RESULT": "PASS",
            "FORBIDDEN_PATH_GOVERNANCE_RESULT": "PASS",
            "BASELINE_COMPARISON_RESULT": "PASS",
            "ROLLBACK_READINESS_RESULT": "PASS",
            "NEXT_PHASE_ID": next_phase_id_for_review,
            "NEXT_PHASE_STATE": "LOCKED",
            "TRANSITION_APPLIED": "NO",
            "RECOMMENDATION": "APPROVE_GATE_ONLY_KEEP_NEXT_PHASE_LOCKED",
        }
        expected_exit_values = tuple(
            hashlib.sha256(condition.encode("utf-8")).hexdigest()
            + f" | RESULT: PASS | EVIDENCE_REF: {report_ref}"
            for condition in exit_conditions
        )
        machine_values: dict[str, list[str]] = {}
        for line in review_lines:
            match = re.fullmatch(r"([A-Z][A-Z0-9_]+):[ \t]*(.+)", line)
            if match:
                machine_values.setdefault(match.group(1), []).append(match.group(2))
        permitted_machine_keys = {
            *expected_machine_values,
            "EXIT_CONDITION_SHA256",
            "GATE_RESULT",
        }
        unknown_machine_keys = sorted(set(machine_values) - permitted_machine_keys)
        invalid_machine_keys = [
            key
            for key, value in sorted(expected_machine_values.items())
            if machine_values.get(key) != [value]
        ]
        if machine_values.get("EXIT_CONDITION_SHA256") != list(expected_exit_values):
            invalid_machine_keys.append("EXIT_CONDITION_SHA256")
        if machine_values.get("GATE_RESULT") != [expected_marker]:
            invalid_machine_keys.append("GATE_RESULT")
        if missing_topics or invalid_machine_keys or unknown_machine_keys:
            raise GovernanceValidationError(
                "Gate review lacks required closeout coverage: "
                f"topics={missing_topics}, machine_keys={invalid_machine_keys}, "
                f"unknown_machine_keys={unknown_machine_keys}"
            )

        reviewer_ref = _normalize_relative_path(
            _string(
                body.get("gate_reviewer_authority_ref"),
                "Gate reviewer authority ref",
            )
        )
        if reviewer_ref != ".codex/agents/governance-reviewer.toml":
            raise GovernanceValidationError(
                "Gate reviewer authority must use the canonical read-only reviewer"
            )
        reviewer_path = self._repository_file(reviewer_ref)
        _, reviewer_payload, _ = self._blob_at(review_commit, reviewer_ref)
        if reviewer_path.read_bytes() != reviewer_payload:
            raise GovernanceValidationError(
                "Gate reviewer authority worktree bytes differ from the reviewed commit"
            )
        if not self._path_untouched_after(review_commit, reviewer_ref):
            raise GovernanceValidationError(
                "Gate reviewer authority changed after the independent review"
            )
        try:
            reviewer_authority = tomllib.loads(reviewer_payload.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise GovernanceValidationError(
                "Gate reviewer authority ref is not valid TOML"
            ) from exc
        if (
            reviewer_authority.get("name") != body.get("gate_reviewer_actor")
            or reviewer_authority.get("sandbox_mode") != "read-only"
        ):
            raise GovernanceValidationError(
                "Gate reviewer authority does not identify a read-only reviewer"
            )

        decision_authority_ref = _normalize_relative_path(
            _string(body.get("decision_authority_ref"), "decision authority ref")
        )
        if decision_authority_ref == self._GCR001_AUTHORITY:
            raise GovernanceValidationError(
                "GCR001 repair authority does not authorize a final Gate decision"
            )
        authority_path = self._repository_file(decision_authority_ref)
        authority_commit = self._unique_add_commit(decision_authority_ref)
        _, authority_payload, _ = self._blob_at(
            authority_commit, decision_authority_ref
        )
        if authority_path.read_bytes() != authority_payload:
            raise GovernanceValidationError(
                "Gate authority worktree bytes differ from A2"
            )
        authority = _mapping(
            self._yaml_mapping_from_bytes(authority_payload, "Gate decision authority"),
            "Gate decision authority",
        )
        self.contracts.validate_instance("DecisionRecord", authority)
        if decision_parent != authority_commit:
            raise GovernanceValidationError(
                "Gate decision must directly follow its controller authority receipt"
            )
        self._require_single_parent(
            authority_commit, review_commit, "Gate controller authority receipt"
        )
        if not self._path_untouched_after(authority_commit, decision_authority_ref):
            raise GovernanceValidationError(
                "Gate controller authority receipt was modified"
            )
        next_phase_id = _string(body.get("next_phase_id"), "Gate next_phase_id")
        phase_label = phase_id.split("_", 1)[0]
        next_phase_label = next_phase_id.split("_", 1)[0]
        expected_selection = (
            f"Approve the {phase_label} Gate for transition eligibility while "
            f"{next_phase_label} remains locked."
        )
        if (
            authority.get("object_type") != "DecisionRecord"
            or authority.get("decision_scope") != "governance"
            or authority.get("lifecycle_state") != "accepted"
            or authority.get("decided_by") != body.get("decided_by")
            or authority.get("content_ref") != normalized
            or authority.get("content_hash") != decision_digest
            or review_ref not in authority.get("evidence_refs", ())
            or authority.get("selected_option") != expected_selection
        ):
            raise GovernanceValidationError(
                "Gate decision authority does not hash-bind and exactly approve this "
                "Gate while keeping P1 locked"
            )

        actors = {
            body.get("executor_actor"),
            body.get("verifier_actor"),
            body.get("gate_reviewer_actor"),
            body.get("decided_by"),
        }
        if None in actors or len(actors) != 4:
            raise GovernanceValidationError(
                "executor, verifier, Gate reviewer and controller must be distinct"
            )
        if not str(body.get("independence_statement", "")).strip():
            raise GovernanceValidationError("Gate independence statement is empty")

        chain = _mapping(body.get("git_chain"), "Gate Git chain")
        founding = self._founding_baseline_commit()
        if chain.get("founding_anchor_commit") != founding:
            raise GovernanceValidationError("Gate founding anchor commit mismatch")
        if chain.get("founding_anchor_tree") != self._tree_oid(founding):
            raise GovernanceValidationError("Gate founding anchor tree mismatch")
        phase_baseline = self._require_full_commit(
            _string(chain.get("phase_baseline_commit"), "Gate phase baseline"),
            "Gate phase baseline",
        )
        if phase_id == "P0_FOUNDATION_AUDIT" and phase_baseline != founding:
            raise GovernanceValidationError("P0 Gate baseline must be the founding commit")
        if chain.get("phase_baseline_tree") != self._tree_oid(phase_baseline):
            raise GovernanceValidationError("Gate phase baseline tree mismatch")
        if chain.get("reviewed_implementation_commit") != implementation_commit:
            raise GovernanceValidationError("Gate reviewed implementation commit mismatch")
        if chain.get("reviewed_implementation_tree") != self._tree_oid(
            implementation_commit
        ):
            raise GovernanceValidationError("Gate reviewed implementation tree mismatch")
        if chain.get("reviewed_closeout_commit") != review_commit:
            raise GovernanceValidationError("Gate reviewed closeout commit mismatch")
        if chain.get("reviewed_closeout_tree") != self._tree_oid(review_commit):
            raise GovernanceValidationError("Gate reviewed closeout tree mismatch")

        self._require_single_parent(
            report_commit, implementation_commit, "ExecutionReport commit"
        )
        self._require_single_parent(
            verification_commit, report_commit, "VerificationResult commit"
        )
        self._require_single_parent(review_commit, verification_commit, "Gate review commit")
        self._require_single_parent(decision_commit, authority_commit, "Gate decision commit")
        expected_c_changes = {report_ref: "A"} | {
            closeout_ref: "A" for closeout_ref in normalized_closeout_refs
        }
        exact_role_commits = (
            (
                implementation_commit,
                report_commit,
                expected_c_changes,
                "ExecutionReport/closeout C",
            ),
            (
                report_commit,
                verification_commit,
                {verification_ref: "A"},
                "VerificationResult V",
            ),
            (
                verification_commit,
                review_commit,
                {review_ref: "A"},
                "Gate review R",
            ),
            (
                review_commit,
                authority_commit,
                {decision_authority_ref: "A"},
                "Gate authority A2",
            ),
            (
                authority_commit,
                decision_commit,
                {normalized: "A"},
                "PhaseGateDecision D",
            ),
        )
        for parent, child, expected_changes, label in exact_role_commits:
            actual_changes = self._changed_entries(parent, child)
            if actual_changes != expected_changes:
                raise GovernanceValidationError(
                    f"{label} commit contains an unauthorized delta: {actual_changes}"
                )
        if not self._is_first_parent_ancestor(phase_baseline, implementation_commit):
            raise GovernanceValidationError(
                "Gate implementation is outside the phase baseline chain"
            )

        reviewed_state = _mapping(
            body.get("reviewed_phase_state"), "Gate reviewed phase state"
        )
        if (
            reviewed_state.get("observed_at_commit") != review_commit
            or reviewed_state.get("observed_tree") != self._tree_oid(review_commit)
        ):
            raise GovernanceValidationError(
                "Gate reviewed phase state does not bind the Gate review commit"
            )
        ledger_at_review = self._yaml_mapping_from_bytes(
            self._blob_at(review_commit, "control/PROGRAM_LEDGER.yaml")[1],
            "Program Ledger at Gate review",
        )
        ledger_phases = _mapping(
            ledger_at_review.get("phases"), "Gate review ledger phases"
        )
        phase_order_at_review = _string_tuple(
            registry_at_review.get("phase_order"), "Gate review phase_order"
        )
        if phase_id not in phase_order_at_review:
            raise GovernanceValidationError("Gate phase is absent from phase_order")
        phase_index = phase_order_at_review.index(phase_id)
        if (
            phase_index + 1 >= len(phase_order_at_review)
            or phase_order_at_review[phase_index + 1] != next_phase_id
            or registry_at_review.get("active_phase") != phase_id
        ):
            raise GovernanceValidationError(
                "PASS Gate next phase is not the Registry-adjacent locked phase"
            )
        current_entry = _mapping(ledger_phases.get(phase_id), "Gate current phase")
        next_entry = _mapping(ledger_phases.get(next_phase_id), "Gate next phase")
        next_phase_file = _string(
            phase_files_at_review.get(next_phase_id), "Gate next phase file"
        )
        next_phase_at_review = self._yaml_mapping_from_bytes(
            self._blob_at(review_commit, next_phase_file)[1],
            "next Phase Card at Gate review",
        )
        if (
            reviewed_state.get("current_phase_status") != "active"
            or reviewed_state.get("next_phase_status") != "locked"
            or reviewed_state.get("next_phase_delta_empty") is not True
            or current_entry.get("status") != "active"
            or next_entry.get("status") != "locked"
            or phase_at_review.get("status") != "active"
            or next_phase_at_review.get("status") != "locked"
        ):
            raise GovernanceValidationError(
                "PASS Gate did not review the current phase active with its adjacent "
                "next phase locked"
            )
        if self._blob_at(phase_baseline, next_phase_file)[1] != self._blob_at(
            review_commit, next_phase_file
        )[1]:
            raise GovernanceValidationError(
                "next Phase Card changed before the PASS Gate"
            )

        baseline_binding = _mapping(
            body.get("next_phase_baseline"), "Gate next phase baseline"
        )
        manifest_value = baseline_binding.get("transition_manifest", [])
        if not isinstance(manifest_value, list):
            raise GovernanceValidationError("Gate transition manifest must be a list")
        identities: set[str] = set()
        transition_manifest: list[Mapping[str, Any]] = []
        required_transition_state_paths = {
            "MANIFEST.sha256",
            "control/ACTIVE_PHASE.yaml",
            "control/PROGRAM_LEDGER.yaml",
            "phases/PHASE_REGISTRY.yaml",
            phase_file_at_review,
            next_phase_file,
        }
        next_phase_plan_ref = f"plans/active/{next_phase_id}.md"
        audit_transition_paths: list[str] = []
        for index, entry_value in enumerate(manifest_value):
            entry = _mapping(entry_value, f"transition manifest entry {index}")
            path = _normalize_relative_path(
                _string(entry.get("path"), f"transition manifest path {index}")
            )
            identity = _path_identity(path)
            if identity in identities:
                raise GovernanceValidationError(
                    f"duplicate transition manifest path alias: {path}"
                )
            identities.add(identity)
            if path in required_transition_state_paths:
                if entry.get("change_type") != "modify":
                    raise GovernanceValidationError(
                        f"transition state path must be modified, not replaced: {path}"
                    )
            elif path.startswith("audit/") and path.endswith(".yaml"):
                if entry.get("change_type") != "add":
                    raise GovernanceValidationError(
                        f"transition AuditEvent must be newly added: {path}"
                )
                audit_transition_paths.append(path)
            elif path == next_phase_plan_ref:
                if entry.get("change_type") != "add":
                    raise GovernanceValidationError(
                        "next-phase ExecPlan must be newly added by the controller "
                        "transition"
                    )
            else:
                raise GovernanceValidationError(
                    f"transition manifest contains a non-governance path: {path}"
                )
            normalized_entry = dict(entry)
            normalized_entry["path"] = path
            transition_manifest.append(normalized_entry)
        manifest_paths = {
            _string(entry.get("path"), "transition manifest path")
            for entry in transition_manifest
        }
        if (
            not required_transition_state_paths <= manifest_paths
            or len(audit_transition_paths) != 1
            or manifest_paths
            != required_transition_state_paths
            | set(audit_transition_paths)
            | {next_phase_plan_ref}
        ):
            raise GovernanceValidationError(
                "transition manifest must contain exactly six state modifications and "
                "one new closeout AuditEvent plus the next-phase ExecPlan"
            )

        return _ValidatedGate(
            decision_path=normalized,
            decision_commit=decision_commit,
            gate_result=gate_result,
            phase_id=phase_id,
            next_phase_id=next_phase_id,
            phase_baseline_commit=phase_baseline,
            next_phase_plan_ref=next_phase_plan_ref,
            completion_evidence=(report_ref, verification_ref, normalized),
            decision_authority_ref=decision_authority_ref,
            controller_actor=_string(body.get("decided_by"), "Gate decided_by"),
            decided_at=_string(body.get("decided_at"), "Gate decided_at"),
            transition_manifest=tuple(transition_manifest),
        )

    def _current_phase_gate_paths(self) -> tuple[str, ...]:
        head = self._git("rev-parse", "--verify", "HEAD^{commit}").strip()
        active_id = _string(self.active.get("phase_id"), "active.phase_id")
        cache_key = (head, active_id)
        if (
            self._phase_gate_paths_cache is not None
            and self._phase_gate_paths_cache[0] == cache_key
        ):
            return self._phase_gate_paths_cache[1]

        decision_paths: dict[str, str] = {}
        commits = tuple(
            line.strip()
            for line in self._git(
                "rev-list", "--first-parent", "--reverse", head
            ).splitlines()
            if line.strip()
        )
        for commit in commits:
            parents = self._commit_parents(commit)
            if not parents:
                continue
            changes = self._changed_entries(parents[0], commit)
            for path, status in changes.items():
                if (
                    status != "A"
                    or not path.startswith("audit/")
                    or Path(path).suffix.casefold() not in {".yaml", ".yml"}
                ):
                    continue
                try:
                    body = self._yaml_mapping_from_bytes(
                        self._blob_at(commit, path)[1],
                        f"historical Gate candidate {path}",
                    )
                except GovernanceValidationError:
                    continue
                if (
                    body.get("object_type") == "PhaseGateDecision"
                    and body.get("phase_id") == self.active.get("phase_id")
                    and body.get("gate_result") == "pass"
                ):
                    identity = _path_identity(path)
                    if identity in decision_paths:
                        raise GovernanceValidationError(
                            f"duplicate historical PASS Gate path alias: {path}"
                        )
                    decision_paths[identity] = path
        result = tuple(sorted(decision_paths.values()))
        self._phase_gate_paths_cache = (cache_key, result)
        return result

    def _current_phase_gates(self) -> tuple[_ValidatedGate, ...]:
        return tuple(
            self._validate_phase_gate_decision(path)
            for path in self._current_phase_gate_paths()
        )

    def _validate_pending_phase_gate(self) -> str:
        decision_paths = self._current_phase_gate_paths()
        if not decision_paths:
            return "no pending PASS PhaseGateDecision"
        gates = self._current_phase_gates()
        passing = tuple(gate for gate in gates if gate.gate_result == "pass")
        if len(passing) > 1:
            raise GovernanceValidationError(
                "multiple unconsumed PASS PhaseGateDecision artifacts"
            )
        if passing:
            gate = passing[0]
            head = self._require_full_commit(
                self._git("rev-parse", "HEAD").strip(), "pending Gate HEAD"
            )
            if head != gate.decision_commit:
                raise GovernanceValidationError(
                    "ordinary commits exist after an unconsumed PASS Gate decision"
                )
            status = self._git(
                "status", "--porcelain=v1", "--untracked-files=all"
            ).strip()
            ignored = tuple(
                path
                for path in self._git(
                    "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
                ).split("\0")
                if path and not path.startswith(".git/")
            )
            if status or ignored:
                raise GovernanceValidationError(
                    "worktree, including ignored projections, is not clean after an "
                    "unconsumed PASS Gate decision"
                )
            p0 = _mapping(
                _mapping(self.ledger.get("phases"), "ledger phases").get(gate.phase_id),
                "pending Gate current phase",
            )
            next_entry = _mapping(
                _mapping(self.ledger.get("phases"), "ledger phases").get(
                    gate.next_phase_id
                ),
                "pending Gate next phase",
            )
            if p0.get("status") != "active" or next_entry.get("status") != "locked":
                raise GovernanceValidationError(
                    "PASS Gate changed phase state before the controller transition"
                )
        return f"{len(gates)} PhaseGateDecision artifact(s) valid"

    def _derive_transition_baseline(self, gate: _ValidatedGate) -> str:
        if gate.gate_result != "pass" or not gate.transition_manifest:
            raise GovernanceValidationError(
                "canonical closeout contracts do not bind an independently approved "
                "Gate decision to a Git baseline commit"
            )
        successors = tuple(
            line.strip()
            for line in self._git(
                "rev-list",
                "--first-parent",
                "--reverse",
                f"{gate.decision_commit}..HEAD",
            ).splitlines()
            if line.strip()
        )
        if not successors:
            raise GovernanceValidationError(
                "PASS Gate has no controller transition commit"
            )
        transition_commit = self._require_full_commit(
            successors[0], "controller transition commit"
        )
        self._require_single_parent(
            transition_commit, gate.decision_commit, "controller transition commit"
        )
        actual = self._changed_entries(gate.decision_commit, transition_commit)
        expected_status = {"add": "A", "modify": "M", "delete": "D"}
        expected: dict[str, str] = {}
        transition_payloads: dict[str, bytes] = {}
        for entry in gate.transition_manifest:
            path = _normalize_relative_path(_string(entry.get("path"), "transition path"))
            change_type = _string(entry.get("change_type"), "transition change type")
            expected[path] = expected_status[change_type]
            if change_type != "delete":
                _, payload, digest = self._blob_at(transition_commit, path)
                transition_payloads[path] = payload
                if digest != entry.get("sha256_after"):
                    raise GovernanceValidationError(
                        f"controller transition digest mismatch: {path}"
                    )
        if actual != expected:
            raise GovernanceValidationError(
                f"controller transition does not match the bound manifest: {actual}"
            )

        registry_ref = "phases/PHASE_REGISTRY.yaml"
        active_ref = "control/ACTIVE_PHASE.yaml"
        ledger_ref = "control/PROGRAM_LEDGER.yaml"
        manifest_ref = "MANIFEST.sha256"
        registry_before = self._yaml_mapping_from_bytes(
            self._blob_at(gate.decision_commit, registry_ref)[1],
            "pre-transition Phase Registry",
        )
        registry_after = self._yaml_mapping_from_bytes(
            transition_payloads[registry_ref], "transition Phase Registry"
        )
        phase_order = _string_tuple(
            registry_before.get("phase_order"), "pre-transition phase_order"
        )
        if gate.phase_id not in phase_order:
            raise GovernanceValidationError("transition phase is absent from phase_order")
        phase_index = phase_order.index(gate.phase_id)
        if (
            gate.next_phase_id is None
            or phase_index + 1 >= len(phase_order)
            or phase_order[phase_index + 1] != gate.next_phase_id
        ):
            raise GovernanceValidationError(
                "controller transition does not target the adjacent next phase"
            )
        phase_files = _mapping(
            registry_before.get("phase_files"), "pre-transition phase_files"
        )
        current_phase_ref = _string(
            phase_files.get(gate.phase_id), "transition current Phase Card"
        )
        next_phase_ref = _string(
            phase_files.get(gate.next_phase_id), "transition next Phase Card"
        )
        expected_registry = copy.deepcopy(registry_before)
        expected_registry["active_phase"] = gate.next_phase_id
        if registry_after != expected_registry:
            raise GovernanceValidationError(
                "transition Phase Registry changed beyond the adjacent active phase"
            )

        current_before = self._yaml_mapping_from_bytes(
            self._blob_at(gate.decision_commit, current_phase_ref)[1],
            "pre-transition current Phase Card",
        )
        next_before = self._yaml_mapping_from_bytes(
            self._blob_at(gate.decision_commit, next_phase_ref)[1],
            "pre-transition next Phase Card",
        )
        current_after = self._yaml_mapping_from_bytes(
            transition_payloads[current_phase_ref],
            "transition current Phase Card",
        )
        next_after = self._yaml_mapping_from_bytes(
            transition_payloads[next_phase_ref], "transition next Phase Card"
        )
        self.contracts.validate_instance("PhaseCard", current_after)
        self.contracts.validate_instance("PhaseCard", next_after)
        expected_current = copy.deepcopy(current_before)
        expected_current["status"] = "complete"
        expected_next = copy.deepcopy(next_before)
        expected_next["status"] = "active"
        expected_next["allowed_paths"] = list(
            _string_tuple(
                next_before.get("allowed_paths"),
                "pre-transition next Phase Card allowed_paths",
            )
        ) + [gate.next_phase_plan_ref]
        expected_next["required_deliverables"] = list(
            _string_tuple(
                next_before.get("required_deliverables"),
                "pre-transition next Phase Card required_deliverables",
            )
        ) + [f"prospective ExecPlan at {gate.next_phase_plan_ref}"]
        if current_after != expected_current or next_after != expected_next:
            raise GovernanceValidationError(
                "controller transition changed Phase Cards beyond status promotion and "
                "the exact next-phase ExecPlan binding"
            )
        self._validate_execplan_payload(
            transition_payloads[gate.next_phase_plan_ref],
            gate.next_phase_plan_ref,
            gate.next_phase_id,
        )
        next_phase_digest = hashlib.sha256(
            transition_payloads[next_phase_ref]
        ).hexdigest()

        audit_paths = [
            path
            for path, status in expected.items()
            if status == "A" and path.startswith("audit/") and path.endswith(".yaml")
        ]
        if len(audit_paths) != 1:
            raise GovernanceValidationError(
                "controller transition requires exactly one new closeout AuditEvent"
            )
        transition_audit_ref = audit_paths[0]
        if not self._path_untouched_after(
            transition_commit, transition_audit_ref
        ):
            raise GovernanceValidationError(
                "transition closeout AuditEvent changed after T"
            )
        if (
            self._repository_file(transition_audit_ref).read_bytes()
            != transition_payloads[transition_audit_ref]
        ):
            raise GovernanceValidationError(
                "transition closeout AuditEvent worktree bytes differ from T"
            )
        transition_audit = self._yaml_mapping_from_bytes(
            transition_payloads[transition_audit_ref], "transition closeout AuditEvent"
        )
        self.contracts.validate_instance("AuditEvent", transition_audit)
        required_targets = {
            f"phase:{gate.phase_id}",
            f"phase:{gate.next_phase_id}",
            manifest_ref,
            active_ref,
            ledger_ref,
            registry_ref,
            current_phase_ref,
            next_phase_ref,
            gate.next_phase_plan_ref,
        }
        if (
            transition_audit.get("object_type") != "AuditEvent"
            or transition_audit.get("event_type") != "phase_closeout"
            or transition_audit.get("lifecycle_state") != "accepted"
            or transition_audit.get("actor") != gate.controller_actor
            or transition_audit.get("created_by") != gate.controller_actor
            or gate.decision_path not in transition_audit.get("evidence_refs", ())
            or not required_targets
            <= set(transition_audit.get("target_refs", ()))
        ):
            raise GovernanceValidationError(
                "transition AuditEvent does not bind the controller Gate and full state delta"
            )
        occurred_at = _string(
            transition_audit.get("occurred_at"), "transition AuditEvent occurred_at"
        )

        active_before = self._yaml_mapping_from_bytes(
            self._blob_at(gate.decision_commit, active_ref)[1],
            "pre-transition Active Phase",
        )
        active_after = self._yaml_mapping_from_bytes(
            transition_payloads[active_ref], "transition Active Phase"
        )
        expected_active = copy.deepcopy(active_before)
        expected_active.update(
            {
                "phase_id": gate.next_phase_id,
                "phase_file": next_phase_ref,
                "phase_sha256": next_phase_digest,
                "activated_at": occurred_at,
                "activation_authority": gate.decision_path,
                "status": "active",
            }
        )
        if active_after != expected_active:
            raise GovernanceValidationError(
                "transition Active Phase does not exactly bind the promoted Phase Card"
            )

        ledger_before = self._yaml_mapping_from_bytes(
            self._blob_at(gate.decision_commit, ledger_ref)[1],
            "pre-transition Program Ledger",
        )
        ledger_after = self._yaml_mapping_from_bytes(
            transition_payloads[ledger_ref], "transition Program Ledger"
        )
        expected_ledger = copy.deepcopy(ledger_before)
        expected_ledger["active_phase"] = gate.next_phase_id
        expected_ledger["active_phase_file"] = next_phase_ref
        expected_ledger["active_phase_sha256"] = next_phase_digest
        expected_ledger["last_verified_at"] = occurred_at
        ledger_phases = _mapping(expected_ledger.get("phases"), "transition ledger phases")
        current_ledger = _mapping(
            ledger_phases.get(gate.phase_id), "transition current ledger phase"
        )
        current_ledger["status"] = "complete"
        current_ledger["completion_evidence"] = [
            *gate.completion_evidence,
            transition_audit_ref,
        ]
        next_ledger = _mapping(
            ledger_phases.get(gate.next_phase_id), "transition next ledger phase"
        )
        next_ledger["status"] = "active"
        next_ledger["completion_evidence"] = []
        accepted_decisions = list(
            _string_tuple(
                expected_ledger.get("accepted_architecture_decisions", []),
                "pre-transition accepted decisions",
            )
        )
        if gate.decision_authority_ref in accepted_decisions:
            raise GovernanceValidationError(
                "Gate authority was already recorded before the atomic transition"
            )
        accepted_decisions.append(gate.decision_authority_ref)
        expected_ledger["accepted_architecture_decisions"] = accepted_decisions
        p0_blocker = (
            "P0 Gate closure remains pending prospective ExecPlan completion, exact "
            "controller-transaction validation, a complete superseding ExecutionReport, "
            "independent PASS verification, independent Gate review, and an explicit "
            "controller PhaseGateDecision"
        )
        expected_ledger["known_blockers"] = [
            blocker
            for blocker in _string_tuple(
                expected_ledger.get("known_blockers", []),
                "pre-transition known_blockers",
            )
            if blocker != p0_blocker
        ]
        if ledger_after != expected_ledger:
            raise GovernanceValidationError(
                "transition Program Ledger is not the exact Gate-authorized state delta"
            )

        manifest_before = self._parse_manifest_bytes(
            self._blob_at(gate.decision_commit, manifest_ref)[1],
            "pre-transition MANIFEST.sha256",
        )
        manifest_after = self._parse_manifest_bytes(
            transition_payloads[manifest_ref], "transition MANIFEST.sha256"
        )
        if set(manifest_after) != set(manifest_before):
            raise GovernanceValidationError(
                "transition MANIFEST path set differs from the reviewed state"
            )
        rebound_paths = {
            active_ref,
            ledger_ref,
            registry_ref,
            current_phase_ref,
            next_phase_ref,
        }
        for path, digest in manifest_before.items():
            expected_digest = (
                hashlib.sha256(transition_payloads[path]).hexdigest()
                if path in rebound_paths
                else digest
            )
            if manifest_after.get(path) != expected_digest:
                raise GovernanceValidationError(
                    f"transition MANIFEST has an unauthorized or stale binding: {path}"
                )
        if manifest_ref in manifest_after or list(manifest_after) != sorted(manifest_after):
            raise GovernanceValidationError(
                "transition MANIFEST must remain lexical and must not hash itself"
            )
        return transition_commit

    def _validate_completed_phase_evidence(
        self,
        phase_id: str,
        evidence_refs: list[str],
    ) -> _ValidatedGate:
        """Validate canonical completion evidence and return its Gate binding."""

        supported_types = {
            "ExecutionReport",
            "VerificationResult",
            "AuditEvent",
            "PhaseGateDecision",
        }
        artifacts: dict[str, list[tuple[str, dict[str, Any]]]] = {
            object_type: [] for object_type in supported_types
        }
        wrappers = {
            "ExecutionReport": "execution_report.schema.yaml",
            "VerificationResult": "verification_result.schema.yaml",
            "AuditEvent": "audit_event.schema.yaml",
            "PhaseGateDecision": "phase_gate_decision.schema.yaml",
        }

        for evidence_ref in evidence_refs:
            normalized = _normalize_relative_path(evidence_ref)
            if not normalized.startswith("audit/") or not normalized.endswith(".yaml"):
                raise GovernanceValidationError(
                    f"phase completion evidence is not a canonical audit YAML: {normalized}"
                )
            resolved = self._repository_file(normalized)
            body = _mapping(ContractLoader.load_yaml(resolved), evidence_ref)
            object_type = body.get("object_type")
            if object_type not in supported_types:
                raise GovernanceValidationError(
                    f"unsupported phase completion evidence type: {object_type}"
                )
            wrapper_path = self.root / "schemas" / wrappers[str(object_type)]
            self.contracts.validate_wrapper_instance(wrapper_path, body)
            artifacts[str(object_type)].append((normalized, body))

        required_types = {
            "ExecutionReport",
            "VerificationResult",
            "PhaseGateDecision",
        }
        missing_types = sorted(
            object_type
            for object_type in required_types
            if not artifacts[object_type]
        )
        if missing_types:
            raise GovernanceValidationError(
                "completed phase lacks canonical evidence types: "
                + ", ".join(missing_types)
            )

        reports = [
            (path, body)
            for path, body in artifacts["ExecutionReport"]
            if body.get("phase_id") == phase_id
            and body.get("completion_state") == "complete"
        ]
        if not reports:
            raise GovernanceValidationError(
                f"completed phase lacks a complete ExecutionReport: {phase_id}"
            )
        if not any(
            body.get("overall_status") == "pass"
            and isinstance(body.get("independence_statement"), str)
            and bool(body["independence_statement"].strip())
            for _, body in artifacts["VerificationResult"]
        ):
            raise GovernanceValidationError(
                f"completed phase lacks independent PASS VerificationResult: {phase_id}"
            )

        valid_gates = [
            self._validate_phase_gate_decision(path)
            for path, body in artifacts["PhaseGateDecision"]
            if body.get("phase_id") == phase_id and body.get("gate_result") == "pass"
        ]
        if len(valid_gates) != 1:
            raise GovernanceValidationError(
                f"completed phase requires exactly one canonical PASS "
                f"PhaseGateDecision: {phase_id}"
            )
        return valid_gates[0]

    def _validate_authority_state(self) -> _PhaseAuthorityState:
        """Validate the complete phase authority state used for every decision."""

        self._assert_authority_snapshot_fresh()
        self._validate_git_inspection_environment()
        cache_key = self._repository_state_token()
        if (
            self._authority_state_cache is not None
            and self._authority_state_cache[0] == cache_key
        ):
            return self._authority_state_cache[1]

        active_id = _string(self.active.get("phase_id"), "active.phase_id")
        active_file = _string(self.active.get("phase_file"), "active.phase_file")
        if self.active.get("status") != "active":
            raise GovernanceValidationError("ACTIVE_PHASE.status must be active")

        phase_order = _string_tuple(
            self.phase_registry.get("phase_order"), "phase_registry.phase_order"
        )
        if len(phase_order) != len(set(phase_order)):
            raise GovernanceValidationError("Phase Registry phase_order contains duplicates")
        registry_files = _mapping(
            self.phase_registry.get("phase_files"), "phase_registry.phase_files"
        )
        if set(phase_order) != set(registry_files):
            raise GovernanceValidationError(
                "Phase Registry phase_order and phase_files disagree"
            )
        file_identities: set[str] = set()
        for phase_id in phase_order:
            relative = _string(registry_files.get(phase_id), f"phase file for {phase_id}")
            identity = _path_identity(relative)
            if identity in file_identities:
                raise GovernanceValidationError(
                    "Phase Registry phase_files contains duplicate path aliases"
                )
            file_identities.add(identity)

        ledger_phases = _mapping(self.ledger.get("phases"), "ledger.phases")
        if set(ledger_phases) != set(phase_order):
            raise GovernanceValidationError(
                "Program Ledger and Phase Registry phase sets disagree"
            )
        if active_id not in phase_order:
            raise GovernanceValidationError(
                f"active phase is absent from Phase Registry order: {active_id}"
            )
        active_index = phase_order.index(active_id)

        if self.ledger.get("active_phase") != active_id:
            raise GovernanceValidationError("Active Phase and Program Ledger disagree")
        if self.phase_registry.get("active_phase") != active_id:
            raise GovernanceValidationError("Active Phase and Phase Registry disagree")
        if self.phase.get("phase_id") != active_id:
            raise GovernanceValidationError("Active Phase and phase body disagree")
        if self.ledger.get("active_phase_file") != active_file:
            raise GovernanceValidationError(
                "Active Phase and Program Ledger phase paths disagree"
            )
        if registry_files.get(active_id) != active_file:
            raise GovernanceValidationError(
                "Active Phase and Phase Registry phase paths disagree"
            )

        active_ledger_ids: list[str] = []
        next_phase_baseline = self._founding_baseline_commit()
        completed_transition: str | None = None
        completed_transitions: list[str] = []
        for index, phase_id in enumerate(phase_order):
            ledger_entry = _mapping(ledger_phases.get(phase_id), f"ledger.{phase_id}")
            relative = _string(registry_files.get(phase_id), f"phase file {phase_id}")
            body = self.phase if phase_id == active_id else self._load_mapping(relative)
            if body.get("phase_id") != phase_id:
                raise GovernanceValidationError(
                    f"phase ID mismatch for {relative}: {body.get('phase_id')}"
                )

            ledger_status = ledger_entry.get("status")
            body_status = body.get("status")
            if ledger_status == "active":
                active_ledger_ids.append(phase_id)
            if index < active_index:
                if ledger_status != "complete" or body_status != "complete":
                    raise GovernanceValidationError(
                        f"phase before active phase is not complete: {phase_id}"
                    )
                evidence_refs = ledger_entry.get("completion_evidence")
                if not isinstance(evidence_refs, list) or not evidence_refs or not all(
                    isinstance(reference, str) and reference for reference in evidence_refs
                ):
                    raise GovernanceValidationError(
                        f"completed phase lacks completion evidence: {phase_id}"
                    )
                completed_gate = self._validate_completed_phase_evidence(
                    phase_id, evidence_refs
                )
                if completed_gate.phase_baseline_commit != next_phase_baseline:
                    raise GovernanceValidationError(
                        f"completed phase Gate baseline is not adjacent: {phase_id}"
                    )
                completed_transition = self._derive_transition_baseline(completed_gate)
                completed_transitions.append(completed_transition)
                next_phase_baseline = completed_transition
            elif index == active_index:
                if ledger_status != "active" or body_status != "active":
                    raise GovernanceValidationError(
                        "active phase is not marked active everywhere"
                    )
            elif ledger_status != "locked" or body_status != "locked":
                raise GovernanceValidationError(
                    f"phase after active phase is not locked: {phase_id}"
                )

        if active_ledger_ids != [active_id]:
            raise GovernanceValidationError(
                f"expected exactly one active ledger phase, got {active_ledger_ids}"
            )

        phase_path = self.root / _normalize_relative_path(active_file)
        digest = hashlib.sha256(phase_path.read_bytes()).hexdigest()
        if digest != self.active.get("phase_sha256"):
            raise GovernanceValidationError(
                "Active Phase hash does not match the phase body"
            )
        if digest != self.ledger.get("active_phase_sha256"):
            raise GovernanceValidationError(
                "Program Ledger hash does not match the phase body"
            )

        baseline_commit: str | None = None
        if active_index > 0:
            if completed_transition is None:
                raise GovernanceValidationError(
                    "canonical closeout contracts do not bind an independently approved "
                    "Gate decision to a Git baseline commit; non-founding phases fail closed"
                )
            baseline_commit = completed_transition
        controller_transaction = self._validate_gcr001_controller_transaction(
            tuple(completed_transitions),
            _environment_validated=True,
        )
        self._assert_authority_snapshot_fresh()
        self._validate_git_inspection_environment()
        if self._repository_state_token() != cache_key:
            raise GovernanceValidationError(
                "Git HEAD or worktree changed during authority validation"
            )
        state = _PhaseAuthorityState(
            active_id,
            phase_order,
            active_index,
            baseline_commit,
            tuple(completed_transitions),
            controller_transaction,
        )
        self._authority_state_cache = (cache_key, state)
        return state

    def _authority_binding_problem(self) -> str | None:
        try:
            self._validate_authority_state()
        except Exception as exc:
            return str(exc)
        return None

    def _authorize_lexical_path(self, normalized: str) -> PathDecision:
        zone_matches = self.resolve_zones(normalized)
        if len(zone_matches) > 1:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=None,
                matched_rule=None,
                reason=f"path ambiguously matches multiple write zones: {zone_matches}",
            )
        zone_name, zone_pattern = zone_matches[0] if zone_matches else (None, None)

        for pattern in self.forbidden_paths:
            if _matches(normalized, pattern):
                return PathDecision(
                    path=normalized,
                    allowed=False,
                    zone=zone_name,
                    matched_rule=pattern,
                    reason="active Phase Card explicitly forbids this path",
                )

        if zone_name in {"Z0_constitution", "Z1_governance"}:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=zone_name,
                matched_rule=zone_pattern,
                reason="owner/controller authority cannot be granted by a phase wildcard",
            )

        if zone_name == "Z2_contracts":
            schema_changes = self.phase.get("schema_changes", [])
            explicitly_named = isinstance(schema_changes, list) and normalized in schema_changes
            if not explicitly_named:
                return PathDecision(
                    path=normalized,
                    allowed=False,
                    zone=zone_name,
                    matched_rule=zone_pattern,
                    reason="contract write lacks exact active-phase permission",
                )

        for pattern in self.allowed_paths:
            if _matches(normalized, pattern):
                return PathDecision(
                    path=normalized,
                    allowed=True,
                    zone=zone_name,
                    matched_rule=pattern,
                    reason="path is explicitly permitted by the active Phase Card",
                )

        return PathDecision(
            path=normalized,
            allowed=False,
            zone=zone_name,
            matched_rule=zone_pattern,
            reason="path is outside the active Phase Card allow-list",
        )

    def authorize_path(self, path: str | Path) -> PathDecision:
        normalized = _normalize_relative_path(path)
        binding_problem = self._authority_binding_problem()
        if binding_problem:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=None,
                matched_rule=None,
                reason=f"authority state is invalid; fail closed: {binding_problem}",
            )

        try:
            passing_gates = tuple(
                gate for gate in self._current_phase_gates() if gate.gate_result == "pass"
            )
        except Exception as exc:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=None,
                matched_rule=None,
                reason=f"current-phase Gate evidence is invalid; fail closed: {exc}",
            )
        if passing_gates:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=None,
                matched_rule=None,
                reason=(
                    "an unconsumed PASS Gate freezes ordinary writes until the "
                    "separate controller transition"
                ),
            )

        return self._authorize_path_with_valid_state(normalized)

    def _authorize_path_with_valid_state(self, normalized: str) -> PathDecision:
        """Authorize one normalized path after authority/Gate validation."""

        if normalized in self._GCR001_G_PATHS:
            zone_name, zone_pattern = self.classify_zone(normalized)
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=zone_name,
                matched_rule=zone_pattern,
                reason="GCR001 controller transaction bytes are frozen after authority",
            )

        lexical_decision = self._authorize_lexical_path(normalized)
        if not lexical_decision.allowed:
            return lexical_decision

        candidate = self.root
        try:
            for part in PurePosixPath(normalized).parts:
                candidate /= part
                candidate.resolve(strict=False).relative_to(self.root)
            resolved = candidate.resolve(strict=False)
            resolved_relative = resolved.relative_to(self.root).as_posix()
            resolved_normalized = _normalize_relative_path(resolved_relative)
        except (OSError, RuntimeError, ValueError) as exc:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=lexical_decision.zone,
                matched_rule=lexical_decision.matched_rule,
                reason=f"resolved path escapes or cannot be proven inside the repository: {exc}",
            )

        resolved_decision = self._authorize_lexical_path(resolved_normalized)
        if not resolved_decision.allowed:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=lexical_decision.zone,
                matched_rule=lexical_decision.matched_rule,
                reason=(
                    f"resolved target {resolved_normalized} is not authorized: "
                    f"{resolved_decision.reason}"
                ),
            )
        if lexical_decision.zone != resolved_decision.zone:
            return PathDecision(
                path=normalized,
                allowed=False,
                zone=lexical_decision.zone,
                matched_rule=lexical_decision.matched_rule,
                reason=(
                    "lexical and resolved paths cross write-zone boundaries: "
                    f"{lexical_decision.zone} -> {resolved_decision.zone}"
                ),
            )
        return lexical_decision

    def _founding_baseline_commit(self) -> str:
        roots = tuple(
            line.strip()
            for line in self._git("rev-list", "--max-parents=0", "HEAD").splitlines()
            if line.strip()
        )
        if len(roots) != 1 or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", roots[0]):
            raise GovernanceValidationError(
                f"Git repository must have exactly one founding baseline commit, got {roots}"
            )
        return roots[0]

    def _current_phase_baseline_commit(self) -> str:
        state = self._validate_authority_state()
        if state.active_index == 0:
            return self._founding_baseline_commit()
        if state.baseline_commit is None:
            raise GovernanceValidationError(
                "no canonical Gate-bound Git baseline is available for the active phase"
            )
        return state.baseline_commit

    def changed_paths_since_baseline(self) -> tuple[str, ...]:
        baseline = self._current_phase_baseline_commit()
        tracked = self._git(
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACMDRTUXB",
            "-z",
            baseline,
            "--",
        ).split("\0")
        untracked = self._git(
            "ls-files", "--others", "--exclude-standard", "-z"
        ).split("\0")
        ignored = self._git(
            "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
        ).split("\0")
        paths = {
            _normalize_relative_path(path)
            for path in (*tracked, *untracked, *ignored)
            if path and not path.startswith(".git/")
        }
        return tuple(sorted(paths))

    def unauthorized_changed_paths(
        self, transaction: _ControllerTransaction | None = None
    ) -> dict[str, PathDecision]:
        if transaction is None:
            state = self._validate_authority_state()
            transaction = state.controller_transaction
        exact_historical_exemptions = (
            transaction.historical_exemptions
            | self._P1_CONTROLLER_PATHS
            | self._P1_GCR_CORRECTION_PATHS
        )
        denied: dict[str, PathDecision] = {}
        for path in self.changed_paths_since_baseline():
            if path in exact_historical_exemptions:
                continue
            decision = self._authorize_path_with_valid_state(path)
            if decision.allowed:
                continue
            denied[path] = decision
        return denied

    def _validate_git_inspection_environment(self) -> None:
        replacement_refs = self._git(
            "for-each-ref", "--format=%(refname)", "refs/replace"
        ).strip()
        if replacement_refs:
            raise GovernanceValidationError(
                "Git replacement refs are not allowed for governance evidence"
            )

        config_records = self._git("config", "--null", "--list").split("\0")
        for record in config_records:
            if not record or "\n" not in record:
                continue
            key, value = record.split("\n", 1)
            if key.casefold() == "core.sparsecheckout" and value.casefold() in {
                "true",
                "yes",
                "on",
                "1",
            }:
                raise GovernanceValidationError(
                    "sparse checkout is not allowed for governance validation"
                )

        git_directories = {
            Path(self._git("rev-parse", "--absolute-git-dir").strip()).resolve(),
        }
        common_raw = self._git("rev-parse", "--git-common-dir").strip()
        common_path = Path(common_raw)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        git_directories.add(common_path.resolve())
        for git_directory in git_directories:
            for relative in ("info/grafts", "info/sparse-checkout"):
                control_file = git_directory / relative
                if control_file.is_file() and control_file.stat().st_size:
                    raise GovernanceValidationError(
                        f"unsupported Git history/worktree projection is active: {control_file}"
                    )

        unusual_index_entries = [
            entry
            for entry in self._git("ls-files", "-v", "-z").split("\0")
            if entry and not entry.startswith("H ")
        ]
        if unusual_index_entries:
            raise GovernanceValidationError(
                "Git index contains skip-worktree, assume-unchanged, or non-stage-zero "
                f"entries: {unusual_index_entries[:5]}"
            )

        # Successful semantic caches must not conceal a deleted or corrupt
        # content-addressed object while HEAD appears unchanged.
        self._git("fsck", "--strict", "--no-dangling")

    def _git(self, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "--no-replace-objects", *arguments],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GovernanceValidationError(f"Git inspection failed: {exc}") from exc
        return completed.stdout

    def _git_bytes(self, *arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "--no-replace-objects", *arguments],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GovernanceValidationError(f"Git inspection failed: {exc}") from exc
        return completed.stdout

    def _manifest_entries(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        identities: dict[str, str] = {}
        manifest = self.root / "MANIFEST.sha256"
        for number, line in enumerate(
            manifest.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                raise GovernanceValidationError(
                    f"invalid MANIFEST.sha256 entry at line {number}"
                )
            normalized = _normalize_relative_path(parts[1])
            identity = _path_identity(normalized)
            if identity in identities:
                raise GovernanceValidationError(
                    "duplicate MANIFEST.sha256 path alias at line "
                    f"{number}: {normalized} conflicts with {identities[identity]}"
                )
            identities[identity] = normalized
            entries[normalized] = parts[0]
        if not entries:
            raise GovernanceValidationError("MANIFEST.sha256 is empty")
        return entries

    def protected_path_drift(
        self, transaction: _ControllerTransaction | None = None
    ) -> tuple[str, ...]:
        if transaction is None:
            state = self._validate_authority_state()
            transaction = state.controller_transaction
        manifest = self._manifest_entries()
        baseline = self._current_phase_baseline_commit()
        changed_since_phase_baseline = set(self.changed_paths_since_baseline())
        exact_frozen_paths = (
            transaction.frozen_paths
            | self._P1_CONTROLLER_PATHS
            | self._P1_GCR_CORRECTION_PATHS
        )
        drift: list[str] = []
        for relative in sorted(manifest):
            # Exact G/validated-transition paths are already compared with the
            # last authorized Git blob by the controller-transaction proof.
            if relative in exact_frozen_paths:
                continue
            if self._authorize_path_with_valid_state(relative).allowed:
                continue
            try:
                expected_payload = self._blob_at(baseline, relative)[1]
            except GovernanceValidationError:
                if (self.root / relative).exists():
                    drift.append(f"added:{relative}")
                continue
            try:
                current_payload = self._repository_file(relative).read_bytes()
            except GovernanceValidationError:
                drift.append(f"missing-or-redirected:{relative}")
                continue
            if current_payload != expected_payload:
                drift.append(f"modified:{relative}")

        for path in self.root.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(self.root).as_posix()
            # A checksum manifest cannot include its own digest. Its integrity is
            # independently covered by the founding Git baseline/change-scope check.
            if relative == "MANIFEST.sha256":
                continue
            if relative in manifest:
                continue
            if relative in exact_frozen_paths:
                continue
            # Files accepted before the current phase baseline retain their
            # historical authority. Only a current-phase delta is judged by
            # the current Phase Card's allow-list.
            if relative not in changed_since_phase_baseline:
                continue
            if not self._authorize_path_with_valid_state(relative).allowed:
                drift.append(f"added:{relative}")
        return tuple(sorted(drift))

    @staticmethod
    def _validate_execplan_payload(
        payload: bytes,
        relative: str,
        phase_id: str,
    ) -> str:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GovernanceValidationError(f"ExecPlan is not UTF-8: {relative}") from exc
        folded = text.casefold()
        if (
            phase_id not in text
            or "execplan" not in folded
            or "prospective" not in folded
        ):
            raise GovernanceValidationError(
                f"ExecPlan does not identify its phase and prospective scope: {relative}"
            )

        matches = tuple(
            re.finditer(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$", text)
        )
        sections: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            body_start = match.end()
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections.append(
                (match.group(1).strip().casefold(), text[body_start:body_end].strip())
            )

        topics = {
            "purpose and invariant objective": lambda heading: (
                "purpose" in heading and "invariant" in heading
            ),
            "current behavior": lambda heading: "behavior" in heading,
            "baseline and measurable target": lambda heading: (
                "baseline" in heading and "target" in heading
            ),
            "scope": lambda heading: "scope" in heading,
            "non-goals": lambda heading: "non-goal" in heading,
            "authority/write zones": lambda heading: (
                "authority" in heading and "write" in heading
            ),
            "design": lambda heading: "design" in heading,
            "alternatives": lambda heading: "alternative" in heading,
            "implementation milestones": lambda heading: "milestone" in heading,
            "schema/data migration": lambda heading: (
                "schema" in heading and "migration" in heading
            ),
            "tests/evals": lambda heading: (
                "test" in heading and "eval" in heading
            ),
            "rollback": lambda heading: "rollback" in heading,
            "progress checklist": lambda heading: "progress" in heading,
            "decisions": lambda heading: "decision" in heading,
            "surprises/discoveries": lambda heading: (
                "surprise" in heading or "discover" in heading
            ),
            "risks": lambda heading: "risk" in heading,
            "final outcomes and evidence": lambda heading: (
                "final" in heading and "evidence" in heading
            ),
        }
        missing_topics = [
            topic
            for topic, matches_topic in topics.items()
            if not any(matches_topic(heading) and body for heading, body in sections)
        ]
        if missing_topics:
            raise GovernanceValidationError(
                f"ExecPlan lacks required non-empty sections for {relative}: "
                f"{missing_topics}"
            )
        return text

    def _validate_active_execplan_binding(self) -> str:
        deliverables = _string_tuple(
            self.phase.get("required_deliverables"), "phase.required_deliverables"
        )
        plan_paths = {
            match.group(0)
            for deliverable in deliverables
            for match in re.finditer(
                r"plans/active/[A-Za-z0-9_.\-/]+\.md", deliverable
            )
        }
        if any("execplan" in value.casefold() for value in deliverables) and not plan_paths:
            raise GovernanceValidationError(
                "active Phase Card requires an ExecPlan without naming its exact path"
            )
        for relative in sorted(plan_paths):
            if relative not in self.allowed_paths:
                raise GovernanceValidationError(
                    f"active Phase Card does not exactly authorize its ExecPlan: {relative}"
                )
            payload = self._validate_execplan_payload(
                self._repository_file(relative).read_bytes(),
                relative,
                _string(self.phase.get("phase_id"), "phase.phase_id"),
            )
            if self.phase.get("phase_id") == "P0_FOUNDATION_AUDIT":
                folded = payload.casefold()
                if (
                    self._GCR001_REPAIR_BASE not in payload
                    or "prospective" not in folded
                    or "does not retroactively" not in folded
                ):
                    raise GovernanceValidationError(
                        "P0 Gate-closure ExecPlan is not truthfully prospective"
                    )
        return f"{len(plan_paths)} active ExecPlan binding(s) valid"

    def cold_start_snapshot(self) -> ColdStartSnapshot:
        purpose = "IRONMAN purpose is defined by the Founding Constitution."
        constitution = self.root / "constitution" / "FOUNDING_CONSTITUTION.md"
        for line in constitution.read_text(encoding="utf-8").splitlines():
            if line.startswith("IRONMAN exists "):
                purpose = line.strip()
                break

        ledger_proposals = _string_tuple(
            self.ledger.get("open_governance_proposals", []),
            "ledger.open_governance_proposals",
        )
        discovered_proposals: set[str] = set()
        proposal_directory = self.root / "proposals"
        if proposal_directory.is_dir():
            discovered_proposals.update(
                path.relative_to(self.root).as_posix()
                for path in proposal_directory.glob("*.md")
                if path.name != "README.md"
            )

        return ColdStartSnapshot(
            program_id=_string(self.ledger.get("program_id"), "ledger.program_id"),
            purpose=purpose,
            purpose_ref="constitution/FOUNDING_CONSTITUTION.md",
            root_metric=_string(self.ledger.get("root_metric"), "ledger.root_metric"),
            active_phase=_string(self.active.get("phase_id"), "active.phase_id"),
            phase_file=_string(self.active.get("phase_file"), "active.phase_file"),
            invariant_objective=_string(
                self.phase.get("invariant_objective"), "phase.invariant_objective"
            ),
            allowed_paths=self.allowed_paths,
            forbidden_paths=self.forbidden_paths,
            acceptance_commands=_string_tuple(
                self.phase.get("acceptance_commands"), "phase.acceptance_commands"
            ),
            exit_conditions=_string_tuple(
                self.phase.get("quantitative_exit_conditions"),
                "phase.quantitative_exit_conditions",
            ),
            audit_requirements=_string_tuple(
                self.phase.get("audit_requirements"), "phase.audit_requirements"
            ),
            ledger_open_proposals=ledger_proposals,
            discovered_proposals=tuple(sorted(discovered_proposals)),
            known_blockers=_string_tuple(
                self.ledger.get("known_blockers", []), "ledger.known_blockers"
            ),
            truth_refs=(
                "constitution/FOUNDING_CONSTITUTION.md",
                "control/SYSTEM_INVARIANTS.yaml",
                "control/WRITE_ZONES.yaml",
                "control/PROGRAM_LEDGER.yaml",
                "control/ACTIVE_PHASE.yaml",
                "phases/PHASE_REGISTRY.yaml",
                _string(self.active.get("phase_file"), "active.phase_file"),
                "schemas/ironman.schema.yaml",
            ),
            projection_warning=(
                "Chat history, generated UI, search indexes, embeddings, and provider "
                "sessions are projections or context, never durable authority."
            ),
        )

    def validate_repository(self) -> GovernanceReport:
        checks: list[GovernanceCheck] = []
        controller_transaction: _ControllerTransaction | None = None
        authority_state: _PhaseAuthorityState | None = None

        def check(name: str, operation: Any) -> None:
            try:
                detail = operation()
                checks.append(GovernanceCheck(name, True, str(detail or "ok")))
            except Exception as exc:  # diagnostic aggregation is intentional
                checks.append(GovernanceCheck(name, False, str(exc)))

        def parse_all_yaml() -> str:
            paths = [
                path
                for path in self.root.rglob("*.yaml")
                if ".git" not in path.parts
            ]
            for path in paths:
                ContractLoader.load_yaml(path)
            return f"{len(paths)} YAML files parsed"

        def parse_all_toml() -> str:
            paths = [
                path
                for path in self.root.rglob("*.toml")
                if ".git" not in path.parts
            ]
            for path in paths:
                tomllib.loads(path.read_text(encoding="utf-8"))
            return f"{len(paths)} TOML files parsed"

        def validate_phases() -> str:
            phase_files = _mapping(
                self.phase_registry.get("phase_files"), "phase_registry.phase_files"
            )
            for phase_id, relative in phase_files.items():
                body = self._load_mapping(_string(relative, f"phase file for {phase_id}"))
                self.contracts.validate_instance("PhaseCard", body)
                if body.get("phase_id") != phase_id:
                    raise GovernanceValidationError(
                        f"phase ID mismatch for {relative}: {body.get('phase_id')}"
                    )
            return f"{len(phase_files)} phase cards valid"

        def validate_phase_state() -> str:
            nonlocal authority_state
            authority_state = self._validate_authority_state()
            return authority_state.active_id

        def validate_phase_hash() -> str:
            relative = _string(self.active.get("phase_file"), "active.phase_file")
            digest = hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
            active_hash = self.active.get("phase_sha256")
            ledger_hash = self.ledger.get("active_phase_sha256")
            if digest != active_hash or digest != ledger_hash:
                raise GovernanceValidationError("active phase SHA-256 binding mismatch")
            if self.ledger.get("active_phase_file") != relative:
                raise GovernanceValidationError("ledger active phase path mismatch")
            return digest

        def validate_invariants() -> str:
            values = self.invariants.get("invariants")
            if not isinstance(values, list) or not values:
                raise GovernanceValidationError("invariants list is empty or invalid")
            seen: set[str] = set()
            valid_severities = {"critical", "high", "medium", "low"}
            for value in values:
                invariant = _mapping(value, "invariant")
                identifier = _string(invariant.get("id"), "invariant.id")
                if not re.fullmatch(r"INV-\d{3}", identifier) or identifier in seen:
                    raise GovernanceValidationError(
                        f"invalid or duplicate invariant ID: {identifier}"
                    )
                seen.add(identifier)
                _string(invariant.get("rule"), f"{identifier}.rule")
                if invariant.get("severity") not in valid_severities:
                    raise GovernanceValidationError(
                        f"invalid invariant severity: {identifier}"
                    )
            return f"{len(values)} unique invariants loaded"

        def validate_typed_audit_artifacts() -> str:
            audit_directory = self.root / "audit"
            paths = (
                sorted(
                    path
                    for path in audit_directory.rglob("*")
                    if path.is_file()
                    and path.suffix.casefold() in {".yaml", ".yml"}
                )
                if audit_directory.is_dir()
                else []
            )
            validated = 0
            for path in paths:
                body = ContractLoader.load_yaml(path)
                if not isinstance(body, dict):
                    raise GovernanceValidationError(
                        f"audit artifact must be a mapping: {path.name}"
                    )
                object_type = body.get("object_type")
                if not isinstance(object_type, str):
                    raise GovernanceValidationError(
                        f"audit artifact lacks object_type: {path.name}"
                    )
                self.contracts.validate_instance(object_type, body)
                validated += 1
            return f"{validated} typed audit artifacts valid"

        def validate_controller_transaction() -> str:
            nonlocal controller_transaction
            state = authority_state or self._validate_authority_state()
            controller_transaction = state.controller_transaction
            return (
                f"{controller_transaction.transaction_id} exact transaction at "
                f"{controller_transaction.transaction_commit}"
            )

        def validate_protected_paths() -> str:
            drift = self.protected_path_drift(controller_transaction)
            if drift:
                raise GovernanceValidationError(
                    "protected/forbidden path drift: " + ", ".join(drift)
                )
            return "zero protected or forbidden path modifications"

        def validate_changed_paths() -> str:
            denied = self.unauthorized_changed_paths(controller_transaction)
            if denied:
                details = ", ".join(
                    f"{path} ({decision.reason})" for path, decision in denied.items()
                )
                raise GovernanceValidationError(f"unauthorized changed paths: {details}")
            changed = self.changed_paths_since_baseline()
            return f"{len(changed)} changed paths, all active-phase authorized"

        check("all_yaml_parses", parse_all_yaml)
        check("all_toml_parses", parse_all_toml)
        check("canonical_meta_schema", lambda: (self.contracts.validate_meta_schema(), "valid")[1])
        check(
            "wrapper_schemas",
            lambda: f"{len(self.contracts.validate_wrappers(self.root / 'schemas'))} wrappers valid",
        )
        check("phase_cards", validate_phases)
        check("active_phase_agreement", validate_phase_state)
        check("active_phase_hash", validate_phase_hash)
        check("system_invariants", validate_invariants)
        check("typed_audit_artifacts", validate_typed_audit_artifacts)
        check("controller_transaction_chain", validate_controller_transaction)
        check("active_execplan_binding", self._validate_active_execplan_binding)
        check("phase_gate_artifact_chain", self._validate_pending_phase_gate)
        check("protected_path_integrity", validate_protected_paths)
        check("active_phase_write_scope", validate_changed_paths)
        return GovernanceReport(tuple(checks))
