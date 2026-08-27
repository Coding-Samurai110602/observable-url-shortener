"""Unit tests for app.core.shortener.

Covers the reversible Base62 codec, random-code properties, and — most
importantly — the collision-retry behavior of ``generate_unique_short_code``,
which is the piece most likely to hide a bug.
"""

from __future__ import annotations

import pytest

from app.core.shortener import (
    BASE62_ALPHABET,
    ShortCodeGenerationError,
    decode_base62,
    encode_base62,
    generate_short_code,
    generate_unique_short_code,
)

# --------------------------------------------------------------------------- #
# Base62 codec
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (10, "A"),
        (61, "z"),
        (62, "10"),
        (3843, "zz"),  # 62*62 - 1
    ],
)
def test_encode_base62_known_values(number: int, expected: str) -> None:
    assert encode_base62(number) == expected


@pytest.mark.parametrize("number", [0, 1, 61, 62, 12345, 2**63])
def test_encode_decode_round_trip(number: int) -> None:
    """Decoding an encoded value must return the original integer."""
    assert decode_base62(encode_base62(number)) == number


def test_encode_base62_rejects_negative() -> None:
    with pytest.raises(ValueError):
        encode_base62(-1)


def test_decode_base62_rejects_empty() -> None:
    with pytest.raises(ValueError):
        decode_base62("")


def test_decode_base62_rejects_invalid_character() -> None:
    with pytest.raises(ValueError):
        decode_base62("abc$")  # '$' is not in the alphabet


# --------------------------------------------------------------------------- #
# Random code generation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("length", [4, 7, 10])
def test_generate_short_code_length_and_alphabet(length: int) -> None:
    code = generate_short_code(length)
    assert len(code) == length
    assert all(char in BASE62_ALPHABET for char in code)


def test_generate_short_code_rejects_non_positive_length() -> None:
    with pytest.raises(ValueError):
        generate_short_code(0)


def test_generate_short_code_is_reasonably_unique() -> None:
    """Sanity check: many draws should almost never collide at length 7."""
    codes = {generate_short_code(7) for _ in range(1000)}
    assert len(codes) == 1000


# --------------------------------------------------------------------------- #
# Unique code generation with collision handling
# --------------------------------------------------------------------------- #


async def test_generate_unique_returns_when_no_collision() -> None:
    """If nothing exists, the first generated code is returned."""

    async def never_exists(_code: str) -> bool:
        return False

    code = await generate_unique_short_code(never_exists, length=7)
    assert len(code) == 7


async def test_generate_unique_retries_past_collisions() -> None:
    """The generator must retry while ``exists`` reports a collision.

    We make the first two candidates "taken" and assert the third is returned,
    and that ``exists`` was consulted exactly three times.
    """
    call_count = 0

    async def exists_twice(_code: str) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count <= 2  # first two calls collide, third is free

    code = await generate_unique_short_code(exists_twice, length=7, max_retries=5)
    assert len(code) == 7
    assert call_count == 3


async def test_generate_unique_raises_after_exhausting_retries() -> None:
    """If every candidate collides, a domain-specific error is raised."""
    attempts = 0

    async def always_exists(_code: str) -> bool:
        nonlocal attempts
        attempts += 1
        return True

    with pytest.raises(ShortCodeGenerationError):
        await generate_unique_short_code(always_exists, length=7, max_retries=3)

    # Exactly max_retries candidates should have been tried.
    assert attempts == 3
