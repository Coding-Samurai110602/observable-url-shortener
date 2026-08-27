"""Short-code generation.

Two strategies live here, each for a different job:

* :func:`encode_base62` / :func:`decode_base62` — a deterministic, reversible
  integer<->string codec. Useful for id-derived codes and for tests that need
  to reason about exact outputs.
* :func:`generate_short_code` + :func:`generate_unique_short_code` — the
  production path: cryptographically-random codes with collision handling
  against a caller-supplied existence check.

Why random rather than "just Base62 the primary key"? Sequential id-based codes
leak volume ("how many URLs exist") and are trivially enumerable, letting anyone
walk every short link. Random codes at length 7 over a 62-symbol alphabet give
62**7 ≈ 3.5e12 possibilities, so collisions are rare and cheaply retried.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

# Base62 alphabet: digits, then upper, then lower. Order is fixed and part of
# the contract for the reversible codec below — do not reorder.
BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(BASE62_ALPHABET)
_CHAR_TO_VALUE = {char: index for index, char in enumerate(BASE62_ALPHABET)}

# Cap retries so a pathologically saturated keyspace surfaces a real error
# instead of spinning forever.
DEFAULT_MAX_COLLISION_RETRIES = 5


class ShortCodeGenerationError(RuntimeError):
    """Raised when a unique short code could not be generated within the retry budget."""


def encode_base62(number: int) -> str:
    """Encode a non-negative integer as a Base62 string.

    ``0`` maps to the first alphabet symbol (``"0"``) rather than an empty
    string, so every input yields at least one character.

    Args:
        number: The non-negative integer to encode.

    Returns:
        The Base62 representation using :data:`BASE62_ALPHABET`.

    Raises:
        ValueError: If ``number`` is negative.
    """
    if number < 0:
        raise ValueError("Cannot Base62-encode a negative integer.")
    if number == 0:
        return BASE62_ALPHABET[0]

    digits: list[str] = []
    while number > 0:
        number, remainder = divmod(number, _BASE)
        digits.append(BASE62_ALPHABET[remainder])
    # Remainders are produced least-significant first; reverse for normal order.
    return "".join(reversed(digits))


def decode_base62(code: str) -> int:
    """Decode a Base62 string back into its integer value.

    Args:
        code: A non-empty string over :data:`BASE62_ALPHABET`.

    Returns:
        The decoded non-negative integer.

    Raises:
        ValueError: If ``code`` is empty or contains a non-Base62 character.
    """
    if not code:
        raise ValueError("Cannot decode an empty string.")

    number = 0
    for char in code:
        try:
            number = number * _BASE + _CHAR_TO_VALUE[char]
        except KeyError as exc:
            raise ValueError(f"Invalid Base62 character: {char!r}") from exc
    return number


def generate_short_code(length: int = 7) -> str:
    """Generate a single cryptographically-random Base62 code.

    Uses :mod:`secrets` (not :mod:`random`) because these codes are user-facing
    identifiers; predictable codes would make links enumerable.

    Args:
        length: Number of characters in the code. Must be positive.

    Returns:
        A random Base62 string of the requested length.

    Raises:
        ValueError: If ``length`` is not positive.
    """
    if length <= 0:
        raise ValueError("Short code length must be positive.")
    return "".join(secrets.choice(BASE62_ALPHABET) for _ in range(length))


async def generate_unique_short_code(
    exists: Callable[[str], Awaitable[bool]],
    length: int = 7,
    max_retries: int = DEFAULT_MAX_COLLISION_RETRIES,
) -> str:
    """Generate a random code guaranteed unique against ``exists``.

    The uniqueness source of truth stays with the caller: ``exists`` is an async
    predicate (typically a DB lookup) returning ``True`` if a code is already
    taken. This keeps the generator free of any storage dependency and trivially
    testable with a fake predicate.

    Note the check-then-insert here is best-effort against races; the database's
    unique constraint on ``urls.short_code`` remains the ultimate guarantee, and
    the caller should still handle an ``IntegrityError`` on insert.

    Args:
        exists: Async predicate; returns ``True`` if the code already exists.
        length: Length of generated codes.
        max_retries: Maximum number of fresh codes to try before giving up.

    Returns:
        A code for which ``exists`` returned ``False``.

    Raises:
        ShortCodeGenerationError: If no free code was found within ``max_retries``.
    """
    for _ in range(max_retries):
        candidate = generate_short_code(length)
        if not await exists(candidate):
            return candidate
    raise ShortCodeGenerationError(
        f"Failed to generate a unique short code after {max_retries} attempts "
        f"(length={length}); consider increasing the code length."
    )
