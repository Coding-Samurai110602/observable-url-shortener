"""Integration tests for ``POST /api/urls``."""

from __future__ import annotations

from httpx import AsyncClient


async def test_create_url_success_returns_expected_shape(client: AsyncClient) -> None:
    response = await client.post("/api/urls", json={"long_url": "https://example.com/page"})

    assert response.status_code == 201
    body = response.json()
    assert body["short_code"]
    # short_url must be BASE_URL + short_code.
    assert body["short_url"].endswith(body["short_code"])
    assert body["long_url"].startswith("https://example.com")
    assert body["expires_at"] is None


async def test_create_url_with_custom_code(client: AsyncClient) -> None:
    response = await client.post(
        "/api/urls",
        json={"long_url": "https://example.com/x", "custom_code": "mycode1"},
    )

    assert response.status_code == 201
    assert response.json()["short_code"] == "mycode1"


async def test_create_url_with_expiry_sets_expires_at(client: AsyncClient) -> None:
    response = await client.post(
        "/api/urls",
        json={"long_url": "https://example.com/x", "expires_in_days": 7},
    )

    assert response.status_code == 201
    assert response.json()["expires_at"] is not None


async def test_duplicate_custom_code_returns_409(client: AsyncClient) -> None:
    payload = {"long_url": "https://example.com/x", "custom_code": "dupe12"}

    first = await client.post("/api/urls", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/urls", json=payload)
    assert second.status_code == 409


async def test_invalid_long_url_returns_422(client: AsyncClient) -> None:
    response = await client.post("/api/urls", json={"long_url": "not-a-valid-url"})
    assert response.status_code == 422


async def test_invalid_custom_code_returns_422(client: AsyncClient) -> None:
    # Contains a space and punctuation — not Base62.
    response = await client.post(
        "/api/urls",
        json={"long_url": "https://example.com/x", "custom_code": "bad code!"},
    )
    assert response.status_code == 422
