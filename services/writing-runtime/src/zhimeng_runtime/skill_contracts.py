from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError


CONTRACT_URI_PREFIX = "https://zhimeng.local/contracts/"
CONTRACT_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
REPARSE_POINT_ATTRIBUTE = 0x400


class SkillContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SkillContractIdentity:
    uri: str
    fingerprint: str
    validator: Draft202012Validator


@dataclass(frozen=True, slots=True)
class SkillContractPair:
    input_contract: SkillContractIdentity
    output_contract: SkillContractIdentity
    bundle_fingerprint: str


class SkillContractRegistry:
    def __init__(self, root: Path | None = None) -> None:
        candidate = (
            Path(__file__).resolve().parents[4] / "packages" / "contracts"
            if root is None
            else Path(root)
        )
        if not candidate.is_absolute():
            raise SkillContractError("contracts_root must be an absolute directory")
        try:
            value = candidate.lstat()
        except OSError as exc:
            raise SkillContractError(
                "contracts_root must be an accessible directory"
            ) from exc
        if (
            not stat.S_ISDIR(value.st_mode)
            or _is_link_or_reparse(value)
            or _is_junction(candidate)
        ):
            raise SkillContractError("contracts_root must be a regular directory")
        self.root = candidate.resolve(strict=True)
        # 校验器缓存：内容指纹不变就复用已构建的 validator（构建是这套里最贵的
        # 一步）。每次 load 仍然重读文件、重算指纹、重查路径安全——文件变了
        # 指纹就对不上，会重建，不牺牲完整性。
        self._validator_cache: dict[str, tuple[str, SkillContractIdentity]] = {}

    def load(self, contract_uri: str) -> SkillContractIdentity:
        relative_path = self._relative_contract_path(contract_uri)
        candidate = self.root.joinpath(*relative_path.parts)
        try:
            _reject_linked_ancestors(candidate, self.root)
            resolved = candidate.resolve(strict=True)
            if not _is_within(resolved, self.root):
                raise SkillContractError("Skill contract path escapes the contract root")
            scanned = resolved.lstat()
            if (
                not stat.S_ISREG(scanned.st_mode)
                or _is_link_or_reparse(scanned)
                or int(getattr(scanned, "st_nlink", 1)) > 1
            ):
                raise SkillContractError("Skill contract must be a regular local file")
            content = _read_stable_file(resolved, scanned)
            schema = json.loads(content.decode("utf-8"))
        except SkillContractError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkillContractError("Skill contract cannot be loaded") from exc

        if not isinstance(schema, dict):
            raise SkillContractError("Skill contract must be a JSON object")
        if schema.get("$schema") != CONTRACT_SCHEMA_URI:
            raise SkillContractError("Skill contract must declare Draft 2020-12")
        if schema.get("$id") != contract_uri:
            raise SkillContractError(
                "Skill contract $id does not match its manifest URI"
            )
        if _has_external_schema_reference(schema):
            raise SkillContractError("Skill contract contains an external $ref")
        fingerprint = hashlib.sha256(content).hexdigest()
        cached = self._validator_cache.get(contract_uri)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        try:
            Draft202012Validator.check_schema(schema)
        except JsonSchemaError as exc:
            raise SkillContractError("Skill contract schema is invalid") from exc
        identity = SkillContractIdentity(
            uri=contract_uri,
            fingerprint=fingerprint,
            validator=Draft202012Validator(schema),
        )
        self._validator_cache[contract_uri] = (fingerprint, identity)
        return identity

    def load_pair(self, input_uri: str, output_uri: str) -> SkillContractPair:
        input_contract = self.load(input_uri)
        output_contract = self.load(output_uri)
        encoded = json.dumps(
            {
                "input": {
                    "uri": input_contract.uri,
                    "fingerprint": input_contract.fingerprint,
                },
                "output": {
                    "uri": output_contract.uri,
                    "fingerprint": output_contract.fingerprint,
                },
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return SkillContractPair(
            input_contract=input_contract,
            output_contract=output_contract,
            bundle_fingerprint=hashlib.sha256(encoded).hexdigest(),
        )

    @staticmethod
    def _relative_contract_path(contract_uri: str) -> PurePosixPath:
        if not isinstance(contract_uri, str) or not contract_uri.startswith(
            CONTRACT_URI_PREFIX
        ):
            raise SkillContractError(
                "Skill contract URI is not a local Zhimeng contract"
            )
        relative = contract_uri.removeprefix(CONTRACT_URI_PREFIX)
        relative_path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or relative_path.is_absolute()
            or relative_path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or not relative_path.name.endswith(".schema.json")
        ):
            raise SkillContractError("Skill contract URI contains an unsafe path")
        return relative_path


def _read_stable_file(path: Path, scanned: os.stat_result) -> bytes:
    with path.open("rb") as handle:
        opened_before = os.fstat(handle.fileno())
        if _stat_key(opened_before) != _stat_key(scanned):
            raise SkillContractError("Skill contract changed before it was read")
        content = handle.read()
        opened_after = os.fstat(handle.fileno())
    final = path.lstat()
    if (
        _stat_key(opened_before) != _stat_key(opened_after)
        or _stat_key(opened_after) != _stat_key(final)
        or len(content) != int(final.st_size)
        or _is_link_or_reparse(final)
        or not stat.S_ISREG(final.st_mode)
        or int(getattr(final, "st_nlink", 1)) > 1
    ):
        raise SkillContractError("Skill contract changed while it was read")
    return content


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE
    )


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))


def _reject_linked_ancestors(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        value = current.lstat()
        if _is_link_or_reparse(value) or _is_junction(current):
            raise SkillContractError("Skill contract path contains a linked component")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_external_schema_reference(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str) and not child.startswith("#"):
                return True
            if _has_external_schema_reference(child):
                return True
    elif isinstance(value, list):
        return any(_has_external_schema_reference(child) for child in value)
    return False


__all__ = [
    "SkillContractError",
    "SkillContractIdentity",
    "SkillContractPair",
    "SkillContractRegistry",
]
