# Copyright (c) 2026

"""Bounded sibling metadata observation with joined directory descriptor loans."""

from __future__ import annotations

import os
import stat
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path
    from types import TracebackType
    from typing import Self

WORKERS = 8
CHUNK_SIZE = 16

_Outcome = tuple[str, os.stat_result | OSError]


def _stat_chunk(descriptor: int, names: tuple[str, ...]) -> tuple[_Outcome, ...]:
    outcomes: list[_Outcome] = []
    for name in names:
        try:
            value: os.stat_result | OSError = os.stat(
                name, dir_fd=descriptor, follow_symlinks=False
            )
        except OSError as error:
            value = error
        outcomes.append((name, value))
    return tuple(outcomes)


class Walker:
    """Own one observation's executor; workers only borrow live parent descriptors."""

    def __init__(self) -> None:
        """Sample the finite scheduling budget once for this observation."""
        if WORKERS < 1 or CHUNK_SIZE < 1:
            msg = "Metadata scheduling bounds must be positive."
            raise ValueError(msg)
        self._workers = WORKERS
        self._chunk_size = CHUNK_SIZE
        self._executor = ThreadPoolExecutor(max_workers=self._workers)

    def __enter__(self) -> Self:
        """Keep the observation's executor alive across namespace roots."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Settle workers even when observation ends unexpectedly."""
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _batch(self, descriptor: int, names: list[str]) -> tuple[_Outcome, ...]:
        futures: list[Future[tuple[_Outcome, ...]]] = []
        try:
            for offset in range(0, len(names), self._chunk_size):
                futures.append(  # noqa: PERF401
                    self._executor.submit(
                        _stat_chunk, descriptor, tuple(names[offset : offset + self._chunk_size])
                    )
                )
            return tuple(outcome for future in futures for outcome in future.result())
        finally:
            # A submission or worker failure must not end a borrowed FD's lifetime.
            for future in futures:
                future.cancel()
            for future in futures:
                with suppress(BaseException):
                    future.result()

    @contextmanager
    def files(
        self, descriptor: int, path: Path, diagnostics: list[str]
    ) -> Iterator[Iterator[tuple[Path, os.stat_result]]]:
        """Consume an owned directory descriptor with explicit iterator cleanup."""
        try:
            with closing(self._files(descriptor, path, diagnostics)) as entries:
                yield entries
        finally:
            os.close(descriptor)

    def _files(  # noqa: C901, PLR0912
        self, descriptor: int, path: Path, diagnostics: list[str]
    ) -> Generator[tuple[Path, os.stat_result]]:
        try:
            with os.scandir(descriptor) as entries:
                finished = unavailable = False
                while not finished and not unavailable:
                    names: list[str] = []
                    for _ in range(self._workers * self._chunk_size):
                        try:
                            names.append(next(entries).name)
                        except StopIteration:
                            finished = True
                            break
                        except OSError:
                            unavailable = True
                            break
                    for name, value in self._batch(descriptor, names):
                        if isinstance(value, OSError):
                            diagnostics.append("physical_namespace_unavailable")
                            continue
                        child = path / name
                        if stat.S_ISDIR(value.st_mode):
                            try:
                                nested = os.open(
                                    name,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor,
                                )
                            except OSError:
                                diagnostics.append("physical_namespace_unavailable")
                            else:
                                with self.files(nested, child, diagnostics) as descendants:
                                    yield from descendants
                        elif stat.S_ISREG(value.st_mode):
                            yield child, value
                        else:
                            diagnostics.append("physical_namespace_unsupported_object")
                    if unavailable:
                        diagnostics.append("physical_namespace_unavailable")
        except OSError:
            diagnostics.append("physical_namespace_unavailable")
