"""Atomic settings, including a current-Windows-user DPAPI envelope for env."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
from typing import Protocol
import uuid
from pydantic import ValidationError

from zhimeng_runtime.mcp_models import McpServerConfig, McpServerUpdate
from zhimeng_runtime.settings_versions import SettingsConflictError, check_settings_revision, settings_lock, settings_revision


class SecretProtector(Protocol):
    def encrypt(self, data: bytes) -> bytes: ...
    def decrypt(self, data: bytes) -> bytes: ...


class WindowsSecretProtector:
    """DPAPI binds ciphertext to this Windows account; never falls back to plaintext."""

    def _crypt(self, data: bytes, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise RuntimeError("本地工具的凭据存储需要 Windows DPAPI。")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = Blob(len(data), buffer)
        target = Blob()
        api = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        method = api.CryptUnprotectData if decrypt else api.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        method.restype = wintypes.BOOL
        if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise RuntimeError("无法读取本机保存的工具凭据。" if decrypt else "无法安全保存工具凭据。")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel.LocalFree(target.pbData)

    def encrypt(self, data: bytes) -> bytes:
        return self._crypt(data, False)

    def decrypt(self, data: bytes) -> bytes:
        return self._crypt(data, True)


class McpSettingsService:
    def __init__(self, path: Path, *, protector: SecretProtector | None = None):
        self.path = Path(path)
        self.protector = protector or WindowsSecretProtector()
        self._lock = settings_lock(self.path)

    def _read_document(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "connections": []}
        if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
            raise ValueError("工具连接配置格式不正确，原文件已保留。")
        if any(not isinstance(item, dict) or not isinstance(item.get("connection_id"), str) for item in data["connections"]):
            raise ValueError("工具连接目录格式不正确，原文件已保留。")
        if len({item["connection_id"] for item in data["connections"]}) != len(data["connections"]):
            raise ValueError("工具连接目录含重复编号，原文件已保留。")
        return data

    def _load(self, document: dict | None = None) -> list[McpServerConfig]:
        data = document if document is not None else self._read_document()
        result = []
        for item in data["connections"]:
            item = dict(item)
            encrypted = item.pop("env_encrypted", None)
            item["env"] = json.loads(self.protector.decrypt(base64.b64decode(encrypted)).decode("utf-8")) if encrypted else {}
            encrypted_headers = item.pop("headers_encrypted", None)
            item["headers"] = json.loads(self.protector.decrypt(base64.b64decode(encrypted_headers)).decode("utf-8")) if encrypted_headers else {}
            try:
                config = McpServerConfig.model_validate(item)
                config.revision = settings_revision(item)
                result.append(config)
            except ValidationError:
                # Pydantic's default message includes input_value. This object
                # already contains decrypted env, before the manager knows the
                # values to redact. Preserve a useful diagnosis without inputs.
                raise ValueError("工具连接配置格式不正确，请检查本机保存的连接设置。") from None
        return result

    def _save(self, configs: list[McpServerConfig], *, original: dict) -> None:
        items = []
        raw_configs = {item["connection_id"]: item for item in original["connections"]}
        known = set(McpServerConfig.model_fields) | {"env_encrypted", "headers_encrypted"}
        for config in configs:
            item = {**{key: value for key, value in raw_configs.get(config.connection_id, {}).items() if key not in known},
                    **config.model_dump()}
            if config.env:
                item["env_encrypted"] = base64.b64encode(self.protector.encrypt(json.dumps(config.env, ensure_ascii=False).encode("utf-8"))).decode("ascii")
            if config.headers:
                item["headers_encrypted"] = base64.b64encode(self.protector.encrypt(json.dumps(config.headers, ensure_ascii=False).encode("utf-8"))).decode("ascii")
            items.append(item)
        data = (json.dumps({**original, "version": 1, "connections": items}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if self._read_document() != original:
                raise SettingsConflictError()
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def list_configs(self) -> list[McpServerConfig]:
        with self._lock:
            return self._load()

    def get(self, connection_id: str) -> McpServerConfig:
        with self._lock:
            for config in self._load():
                if config.connection_id == connection_id:
                    return config
        raise KeyError(connection_id)

    def upsert(self, update: McpServerUpdate, connection_id: str | None = None) -> McpServerConfig:
        with self._lock:
            original = self._read_document()
            configs = self._load(original)
            target_id = connection_id or update.creation_id
            if connection_id and update.creation_id:
                raise ValueError("新建编号只能用于添加工具连接。")
            old = next((item for item in configs if item.connection_id == target_id), None)
            expected = update.expected_revision if connection_id else ("missing" if update.creation_id else update.expected_revision)
            check_settings_revision(expected, old.revision if old else "missing")
            if connection_id is not None and old is None:
                raise KeyError(connection_id)
            env = dict(old.env) if old else {}
            for name in update.remove_env:
                for existing in list(env):
                    if existing == name or (os.name == "nt" and existing.casefold() == name.casefold()):
                        env.pop(existing)
            for name, value in update.env.items():
                if value == "":
                    continue
                for existing in list(env):
                    if existing == name or (os.name == "nt" and existing.casefold() == name.casefold()):
                        env.pop(existing)
                env[name] = value
            headers = dict(old.headers) if old else {}
            for name in update.remove_headers:
                for existing in list(headers):
                    if existing.casefold() == name.casefold():
                        headers.pop(existing)
            for name, value in update.headers.items():
                if value == "":
                    continue
                for existing in list(headers):
                    if existing.casefold() == name.casefold():
                        headers.pop(existing)
                headers[name] = value
            cwd = Path(update.cwd).expanduser() if update.cwd else Path.home()
            if update.transport == "stdio" and (not cwd.is_absolute() or not cwd.is_dir()):
                raise ValueError("工作目录需要是存在的完整文件夹路径。")
            config = McpServerConfig(connection_id=target_id or uuid.uuid4().hex, name=update.name,
                transport=update.transport, command=update.command, args=update.args, cwd=str(cwd.resolve()),
                enabled=update.enabled, env=env, url=update.url, headers=headers)
            configs = [config if item.connection_id == connection_id else item for item in configs]
            if old is None:
                configs.append(config)
            self._save(configs, original=original)
            return self.get(config.connection_id)

    def remove(self, connection_id: str, *, expected_revision: str | None = None) -> None:
        with self._lock:
            original = self._read_document()
            configs = self._load(original)
            old = next((item for item in configs if item.connection_id == connection_id), None)
            check_settings_revision(expected_revision, old.revision if old else "missing")
            remaining = [item for item in configs if item.connection_id != connection_id]
            if len(remaining) == len(configs):
                raise KeyError(connection_id)
            self._save(remaining, original=original)
