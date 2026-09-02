"""A typed outcome for chains of independent verification steps.

Used wherever code re-verifies something proposed by a language model, or
runs several checks that should all be attempted rather than stopping at
the first failure — a `Result` is either a success carrying its value, or a
failure carrying every reason it failed, never both and never neither.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

T = TypeVar("T")
U = TypeVar("U")


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    """The outcome of one step (or a chain of steps) in a verification flow."""

    _value: T | None
    errors: tuple[str, ...]

    @classmethod
    def success(cls, value: T) -> Result[T]:
        return cls(_value=value, errors=())

    @classmethod
    def failure(cls, error: str, *more_errors: str) -> Result[T]:
        return cls(_value=None, errors=(error, *more_errors))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def value(self) -> T:
        """The success value. Raise if this result is a failure."""
        if not self.ok:
            raise ValueError(f"Called .value on a failed Result: {'; '.join(self.errors)}")
        return cast(T, self._value)

    def unwrap_or(self, default: T) -> T:
        """The success value, or `default` if this result is a failure."""
        return cast(T, self._value) if self.ok else default

    def map(self, transform: Callable[[T], U]) -> Result[U]:
        """Transform the success value; a failure passes its errors through."""
        if not self.ok:
            return Result(_value=None, errors=self.errors)
        return Result.success(transform(cast(T, self._value)))

    def and_then(self, step: Callable[[T], Result[U]]) -> Result[U]:
        """Chain another verification step that itself can fail."""
        if not self.ok:
            return Result(_value=None, errors=self.errors)
        return step(cast(T, self._value))

    @staticmethod
    def combine(results: Iterable[Result[T]]) -> Result[tuple[T, ...]]:
        """Run every result to completion and collect every failure, rather
        than stopping at the first one — for verifying several independent
        fields at once and reporting all of the failing ones together."""
        materialised = list(results)
        errors: list[str] = []
        for result in materialised:
            errors.extend(result.errors)
        if errors:
            return Result(_value=None, errors=tuple(errors))
        return Result.success(tuple(cast(T, result._value) for result in materialised))
