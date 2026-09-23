"""Typed views and deterministic validation for canonical IRONMAN contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry as ReferenceRegistry
from referencing import Resource
from referencing.exceptions import Unresolvable
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver


class ContractError(ValueError):
    """Base error for contract loading or validation."""


class UnknownContractError(ContractError):
    """Raised when a requested canonical definition does not exist."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses silent duplicate-key replacement."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Stable, serializable view of a JSON Schema validation failure."""

    path: str
    message: str
    validator: str | None


class ContractValidationError(ContractError):
    """Raised when a document does not satisfy a canonical definition."""

    def __init__(self, definition: str, issues: tuple[ValidationIssue, ...]):
        self.definition = definition
        self.issues = issues
        detail = "; ".join(
            f"{issue.path or '<root>'}: {issue.message}" for issue in issues
        )
        super().__init__(f"invalid {definition}: {detail}")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CanonicalContract:
    """Typed, read-only metadata view over the canonical JSON Schema."""

    schema_uri: str
    schema_id: str
    title: str
    definitions: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> CanonicalContract:
        schema_uri = document.get("$schema")
        schema_id = document.get("$id")
        title = document.get("title")
        definitions = document.get("$defs")
        if not isinstance(schema_uri, str) or not schema_uri:
            raise ContractError("canonical schema is missing a non-empty $schema")
        if not isinstance(schema_id, str) or not schema_id:
            raise ContractError("canonical schema is missing a non-empty $id")
        if not isinstance(title, str) or not title:
            raise ContractError("canonical schema is missing a non-empty title")
        if not isinstance(definitions, dict) or not definitions:
            raise ContractError("canonical schema is missing non-empty $defs")
        typed_definitions: dict[str, Mapping[str, Any]] = {}
        for name, definition in definitions.items():
            if not isinstance(name, str) or not name:
                raise ContractError("canonical definition names must be non-empty strings")
            if not isinstance(definition, dict):
                raise ContractError(f"canonical definition {name} must be an object")
            typed_definitions[name] = _deep_freeze(deepcopy(definition))
        return cls(
            schema_uri=schema_uri,
            schema_id=schema_id,
            title=title,
            definitions=MappingProxyType(typed_definitions),
        )


class ContractLoader:
    """Load the canonical schema once and validate named contract instances."""

    DRAFT_2020_12_URI = "https://json-schema.org/draft/2020-12/schema"
    WRAPPER_FIELDS = frozenset({"$schema", "$id", "$ref"})
    WRAPPER_ID_PREFIX = "https://local.ironman/schemas/"
    WRAPPER_PREFIX = "ironman.schema.yaml#/$defs/"

    def __init__(self, canonical_path: Path):
        self.canonical_path = canonical_path.resolve()
        document = self.load_yaml(self.canonical_path)
        if not isinstance(document, dict):
            raise ContractError("canonical schema document must be a mapping")
        self._document: dict[str, Any] = document
        self.contract = CanonicalContract.from_mapping(document)
        self._validators: dict[str, Draft202012Validator] = {}
        self._reference_registry = ReferenceRegistry().with_resource(
            self.contract.schema_id,
            Resource.from_contents(self._document),
        )

    @staticmethod
    def load_yaml(path: Path) -> Any:
        """Load one UTF-8 YAML document without constructing arbitrary objects."""

        try:
            return yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=_UniqueKeySafeLoader,
            )
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ContractError(f"cannot parse YAML {path}: {exc}") from exc

    @property
    def definition_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.contract.definitions))

    @property
    def raw_schema(self) -> Mapping[str, Any]:
        """Return a defensive copy so callers cannot mutate loaded authority."""

        return deepcopy(self._document)

    def validate_meta_schema(self) -> None:
        try:
            Draft202012Validator.check_schema(self._document)
        except SchemaError as exc:
            raise ContractError(f"canonical JSON Schema is invalid: {exc.message}") from exc

    def definition(self, name: str) -> Mapping[str, Any]:
        try:
            return self.contract.definitions[name]
        except KeyError as exc:
            raise UnknownContractError(f"unknown canonical contract: {name}") from exc

    def validator(self, name: str) -> Draft202012Validator:
        self.definition(name)
        if name not in self._validators:
            entrypoint = {
                "$schema": self.contract.schema_uri,
                "$ref": f"{self.contract.schema_id}#/$defs/{name}",
            }
            self._validators[name] = Draft202012Validator(
                entrypoint,
                format_checker=FormatChecker(),
                registry=self._reference_registry,
            )
        return self._validators[name]

    def issues(self, name: str, instance: Any) -> tuple[ValidationIssue, ...]:
        errors = sorted(
            self.validator(name).iter_errors(instance),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            ValidationIssue(
                path="/".join(str(part) for part in error.absolute_path),
                message=error.message,
                validator=str(error.validator) if error.validator is not None else None,
            )
            for error in errors
        )

    def validate_instance(self, name: str, instance: Any) -> None:
        issues = self.issues(name, instance)
        if issues:
            raise ContractValidationError(name, issues)

    def _validated_wrapper(
        self,
        wrapper_path: Path,
        *,
        known_ids: dict[str, Path] | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        """Load a thin wrapper and eagerly prove its canonical reference resolves."""

        wrapper = self.load_yaml(wrapper_path)
        if not isinstance(wrapper, dict):
            raise ContractError(f"wrapper schema must be a mapping: {wrapper_path}")

        fields = set(wrapper)
        if fields != self.WRAPPER_FIELDS:
            missing = sorted(self.WRAPPER_FIELDS - fields)
            unexpected = sorted(str(field) for field in fields - self.WRAPPER_FIELDS)
            detail: list[str] = []
            if missing:
                detail.append(f"missing fields {missing}")
            if unexpected:
                detail.append(f"unexpected fields {unexpected}")
            raise ContractError(
                f"wrapper must contain only $schema, $id, and $ref: "
                f"{wrapper_path} ({'; '.join(detail)})"
            )

        if wrapper["$schema"] != self.DRAFT_2020_12_URI:
            raise ContractError(
                f"wrapper must use Draft 2020-12 $schema: {wrapper_path}"
            )

        wrapper_id = wrapper["$id"]
        if not isinstance(wrapper_id, str):
            raise ContractError(f"wrapper $id must be a string: {wrapper_path}")
        if wrapper_id == self.contract.schema_id:
            raise ContractError(
                f"wrapper $id collides with canonical schema $id: {wrapper_path}"
            )
        if known_ids is not None:
            if previous := known_ids.get(wrapper_id):
                raise ContractError(
                    f"duplicate wrapper $id {wrapper_id}: {previous} and {wrapper_path}"
                )
            known_ids[wrapper_id] = wrapper_path
        expected_id = f"{self.WRAPPER_ID_PREFIX}{wrapper_path.name}"
        if wrapper_id != expected_id:
            raise ContractError(
                f"wrapper $id must match its filename; expected {expected_id}: "
                f"{wrapper_path}"
            )

        try:
            Draft202012Validator.check_schema(wrapper)
        except SchemaError as exc:
            raise ContractError(
                f"invalid wrapper JSON Schema {wrapper_path}: {exc.message}"
            ) from exc
        reference = wrapper["$ref"]
        if not isinstance(reference, str) or not reference.startswith(self.WRAPPER_PREFIX):
            raise ContractError(f"wrapper has non-canonical $ref: {wrapper_path}")
        name = reference[len(self.WRAPPER_PREFIX) :]
        self.definition(name)
        try:
            resolved = self._reference_registry.resolver(wrapper_id).lookup(reference)
        except Unresolvable as exc:
            raise ContractError(
                f"wrapper $ref cannot be resolved against the canonical schema: "
                f"{wrapper_path}: {reference}"
            ) from exc
        if resolved.contents != self._document["$defs"][name]:
            raise ContractError(
                f"wrapper $ref did not resolve to canonical $defs/{name}: {wrapper_path}"
            )
        return wrapper, name, wrapper_id

    def wrapper_definition(self, wrapper_path: Path) -> str:
        _, name, _ = self._validated_wrapper(wrapper_path)
        return name

    def wrapper_validator(self, wrapper_path: Path) -> Draft202012Validator:
        wrapper, _, _ = self._validated_wrapper(wrapper_path)
        return Draft202012Validator(
            wrapper,
            format_checker=FormatChecker(),
            registry=self._reference_registry,
        )

    def validate_wrapper_instance(self, wrapper_path: Path, instance: Any) -> None:
        wrapper, name, _ = self._validated_wrapper(wrapper_path)
        validator = Draft202012Validator(
            wrapper,
            format_checker=FormatChecker(),
            registry=self._reference_registry,
        )
        errors = sorted(
            validator.iter_errors(instance),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            issues = tuple(
                ValidationIssue(
                    path="/".join(str(part) for part in error.absolute_path),
                    message=error.message,
                    validator=(
                        str(error.validator) if error.validator is not None else None
                    ),
                )
                for error in errors
            )
            raise ContractValidationError(name, issues)

    def validate_wrappers(self, schema_directory: Path) -> dict[str, str]:
        resolved: dict[str, str] = {}
        wrapper_ids: dict[str, Path] = {}
        for path in sorted(schema_directory.glob("*.schema.yaml")):
            if path.resolve() == self.canonical_path:
                continue
            wrapper, name, _ = self._validated_wrapper(path, known_ids=wrapper_ids)
            resolved[path.name] = name
            Draft202012Validator(
                wrapper,
                format_checker=FormatChecker(),
                registry=self._reference_registry,
            )
        return resolved
