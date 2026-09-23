"""Conservative lifecycle promotion validation from machine-readable policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import ContractLoader


class LifecycleTransitionError(ValueError):
    """Raised when a lifecycle transition is undeclared or lacks evidence."""

    def __init__(self, decision: TransitionDecision):
        self.decision = decision
        super().__init__(decision.reason)


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    from_state: str
    to_state: str
    structurally_eligible: bool
    authorized: bool
    rule: str | None
    missing_requirements: tuple[str, ...]
    reason: str
    independent_evidence_required: bool

    @property
    def allowed(self) -> bool:
        """Whether the transition may be committed.

        P0 deliberately never grants promotion authority.  A structurally
        eligible edge is only a proposal until a later authority component
        validates durable evidence, verifier independence, and (where
        applicable) owner approval.
        """

        return self.authorized


class LifecycleValidator:
    """Deny-by-default validator for explicitly declared promotion rules.

    Requirement tokens are checked for structural completeness only. Their
    truth, provenance, verifier independence, and actor authority must be
    established by an authority component that is intentionally absent from
    P0. Structural eligibility is never returned as commit authorization.
    """

    def __init__(
        self,
        states: Iterable[str],
        promotion_requirements: Mapping[tuple[str, str], tuple[str, ...]],
    ):
        normalized_states = tuple(dict.fromkeys(states))
        if not normalized_states or any(not state for state in normalized_states):
            raise ValueError("lifecycle states must be non-empty")
        self.states = normalized_states
        self._state_set = frozenset(normalized_states)
        self.promotion_requirements = MappingProxyType(dict(promotion_requirements))

    @classmethod
    def from_policy_file(cls, policy_path: Path) -> LifecycleValidator:
        policy = ContractLoader.load_yaml(policy_path)
        if not isinstance(policy, dict):
            raise ValueError("lifecycle policy must be a mapping")
        lifecycle = policy.get("asset_lifecycle")
        if not isinstance(lifecycle, dict) or not lifecycle:
            raise ValueError("lifecycle policy has no asset_lifecycle states")
        states = tuple(str(state) for state in lifecycle)
        rules = policy.get("promotion_rules")
        if not isinstance(rules, dict) or not rules:
            raise ValueError("lifecycle policy has no promotion_rules")

        parsed: dict[tuple[str, str], tuple[str, ...]] = {}
        for name, value in rules.items():
            if not isinstance(name, str) or "_to_" not in name:
                raise ValueError(f"invalid promotion rule name: {name}")
            from_state, separator, to_state = name.partition("_to_")
            if not separator or from_state not in lifecycle or to_state not in lifecycle:
                raise ValueError(f"promotion rule references unknown state: {name}")
            if not isinstance(value, dict):
                raise ValueError(f"promotion rule must be a mapping: {name}")
            requires = value.get("requires")
            if not isinstance(requires, list) or not requires or not all(
                isinstance(requirement, str) and requirement for requirement in requires
            ):
                raise ValueError(f"promotion rule has invalid requirements: {name}")
            if len(requires) != len(set(requires)):
                raise ValueError(f"promotion rule has duplicate requirements: {name}")
            key = (from_state, to_state)
            if key in parsed:
                raise ValueError(f"duplicate lifecycle promotion: {name}")
            parsed[key] = tuple(requires)
        return cls(states, parsed)

    @classmethod
    def from_root(cls, repository_root: str | Path) -> LifecycleValidator:
        root = Path(repository_root).resolve()
        validator = cls.from_policy_file(root / "control" / "LIFECYCLE_POLICY.yaml")
        contracts = ContractLoader(root / "schemas" / "ironman.schema.yaml")
        envelope = contracts.raw_schema["$defs"]["ObjectEnvelope"]
        canonical_states = tuple(envelope["properties"]["lifecycle_state"]["enum"])
        if set(validator.states) != set(canonical_states):
            raise ValueError("lifecycle policy states disagree with canonical schema")
        return validator

    @staticmethod
    def _evidence_tokens(
        evidence: Mapping[str, Any] | Iterable[str] | None,
    ) -> frozenset[str]:
        if evidence is None:
            return frozenset()
        if isinstance(evidence, Mapping):
            return frozenset(
                str(token) for token, present in evidence.items() if present is True
            )
        if isinstance(evidence, str):
            return frozenset((evidence,))
        return frozenset(str(token) for token in evidence)

    def evaluate(
        self,
        from_state: str,
        to_state: str,
        evidence: Mapping[str, Any] | Iterable[str] | None = None,
    ) -> TransitionDecision:
        if from_state not in self._state_set or to_state not in self._state_set:
            unknown = from_state if from_state not in self._state_set else to_state
            return TransitionDecision(
                from_state,
                to_state,
                False,
                False,
                None,
                (),
                f"unknown lifecycle state: {unknown}",
                True,
            )
        if from_state == to_state:
            return TransitionDecision(
                from_state,
                to_state,
                False,
                False,
                None,
                (),
                "lifecycle transition must change state",
                True,
            )
        requirements = self.promotion_requirements.get((from_state, to_state))
        rule = f"{from_state}_to_{to_state}"
        if requirements is None:
            return TransitionDecision(
                from_state,
                to_state,
                False,
                False,
                None,
                (),
                f"transition {rule} is not declared by promotion_rules",
                True,
            )
        supplied = self._evidence_tokens(evidence)
        missing = tuple(requirement for requirement in requirements if requirement not in supplied)
        if missing:
            return TransitionDecision(
                from_state,
                to_state,
                False,
                False,
                rule,
                missing,
                f"transition {rule} lacks evidence: {', '.join(missing)}",
                True,
            )
        return TransitionDecision(
            from_state,
            to_state,
            True,
            False,
            rule,
            (),
            f"transition {rule} is structurally eligible but not authorized; durable evidence, independent verification, and actor authority are required",
            True,
        )

    def require_structural_eligibility(
        self,
        from_state: str,
        to_state: str,
        evidence: Mapping[str, Any] | Iterable[str] | None = None,
    ) -> TransitionDecision:
        """Return a proposal decision only when the declared rule is complete."""

        decision = self.evaluate(from_state, to_state, evidence)
        if not decision.structurally_eligible:
            raise LifecycleTransitionError(decision)
        return decision

    def require_transition(
        self,
        from_state: str,
        to_state: str,
        evidence: Mapping[str, Any] | Iterable[str] | None = None,
    ) -> TransitionDecision:
        decision = self.require_structural_eligibility(from_state, to_state, evidence)
        # P0 has no controller/owner authority resolver and therefore cannot
        # turn caller assertions into a durable lifecycle mutation.
        raise LifecycleTransitionError(decision)
