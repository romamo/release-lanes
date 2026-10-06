"""The config file, .github/shipmill.toml, and the strict reader every section parser uses:
one file, one section per layer (D-4)"""

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from shipmill.errors import ReleaseError

CONFIG_PATH = Path(".github") / "shipmill.toml"


def config_path(root: Path) -> Path:
    """The file shipmill reads its settings from: .github/shipmill.toml"""
    path = root / CONFIG_PATH
    if not path.is_file():
        raise ReleaseError(f"no release policy at {path}")
    return path


def read(path: Path) -> Mapping[str, Any]:
    """The file's TOML; a syntax error names the file"""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{path}: {exc}") from None


class Table:
    """A TOML table read strictly: unknown keys and wrong types fail with the key's path"""

    def __init__(self, raw: Mapping[str, Any], where: str) -> None:
        self.raw = raw
        self.where = where

    def keys(self) -> list[str]:
        return list(self.raw)

    def allow(self, *keys: str) -> None:
        """Refuse keys outside these; a StrEnum's members name their plain values (#59)"""
        names = sorted(str(k) for k in keys)
        if unknown := sorted(set(self.raw) - set(names)):
            raise ReleaseError(f"{self.where}: unknown keys {unknown}; allowed: {names}")

    def _get(self, key: str, default: object, kind: type | tuple[type, ...], label: str) -> Any:
        if key not in self.raw:
            if default is _REQUIRED:
                raise ReleaseError(f"{self.where}: {key} is required")
            return default
        value = self.raw[key]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ReleaseError(f"{self.where}: {key} must be {label}, got {value!r}")
        return value

    def string(self, key: str, default: object = None) -> str:
        value: str = self._get(key, _REQUIRED if default is None else default, str, "a string")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value: bool = self._get(key, default, bool, "true or false")
        return value

    def integer(self, key: str, default: int | None, low: int, high: int | None) -> int | None:
        """high None: no upper bound"""
        value: int | None = self._get(key, default, int, "an integer")
        if value is not None and high is None and value < low:
            raise ReleaseError(f"{self.where}: {key} must be {low} or more, got {value}")
        if value is not None and high is not None and not low <= value <= high:
            raise ReleaseError(f"{self.where}: {key} must be in {low}..{high}, got {value}")
        return value

    def strings(self, key: str, default: tuple[str, ...] | None = None) -> tuple[str, ...]:
        value = self._get(key, _REQUIRED if default is None else default, (list, tuple), "a list of strings")
        if not all(isinstance(v, str) and v for v in value):
            raise ReleaseError(f"{self.where}: {key} must be a list of non-empty strings")
        return tuple(value)

    def enum[E: StrEnum](self, key: str, kind: type[E]) -> E:
        value = self.string(key)
        try:
            return kind(value)
        except ValueError:
            raise ReleaseError(f"{self.where}: {key} must be one of {[e.value for e in kind]}, got {value!r}") from None

    def enums[E: StrEnum](self, key: str, kind: type[E], default: tuple[E, ...]) -> tuple[E, ...]:
        values = self.strings(key, default=tuple(default))
        try:
            return tuple(kind(v) for v in values)
        except ValueError:
            raise ReleaseError(f"{self.where}: {key} holds values outside {[e.value for e in kind]}") from None

    def table(self, key: str, optional: bool = False) -> Table:
        value = self._get(key, {} if optional else _REQUIRED, dict, "a table")
        return Table(value, f"{self.where} [{key}]")

    def tables(self, key: str) -> list[Table]:
        value = self._get(key, [], list, "an array of tables")
        if not all(isinstance(v, dict) for v in value):
            raise ReleaseError(f"{self.where}: {key} must be an array of tables, [[{key}]]")
        return [Table(v, f"{self.where} [[{key}]][{i}]") for i, v in enumerate(value)]


_REQUIRED = object()
