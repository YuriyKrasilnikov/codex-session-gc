# Copyright (c) 2026

"""The complete command and environment for one owned child."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_FIRST_INHERITED_DESCRIPTOR: Final = 3


@dataclass(frozen=True, slots=True)
class ChildCommand:
    """The complete command and environment for one owned child."""

    executable: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    inherited_descriptors: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """Require an explicit executable, string environment and unique inherited descriptors."""
        if not self.executable.is_absolute():
            msg = "The executable path must be absolute."
            raise ValueError(msg)
        if not all(type(argument) is str for argument in self.arguments):
            msg = "Command arguments must be strings."
            raise TypeError(msg)
        if any(
            type(value) is not int or value < _FIRST_INHERITED_DESCRIPTOR
            for value in self.inherited_descriptors
        ):
            msg = "Inherited descriptors must be open non-standard file descriptors."
            raise ValueError(msg)
        if len(set(self.inherited_descriptors)) != len(self.inherited_descriptors):
            msg = "Inherited descriptors must be unique."
            raise ValueError(msg)
        if not all(
            type(key) is str and type(value) is str for key, value in self.environment.items()
        ):
            msg = "Command environment entries must be strings."
            raise TypeError(msg)
