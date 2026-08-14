"""Tests for the LLM router (deep_research/llm/router.py).

Covers the routing table, the images-never-to-secondary rule, the
secondary->primary fallback wrapper, and the single-endpoint parity path.
"""

from __future__ import annotations

import pytest

from deep_research.config import LLMConfig, LLMRole
from deep_research.llm.router import FallbackClient, LLMRouter, _SecondaryBreaker


# Fake OpenAI client doubles: we only exercise `resolve` (no real network
# calls), so a plain object with a `close` stub is enough.
class _FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _enter_router(cfg: LLMConfig) -> LLMRouter:
    """Enter the router context with fake primary/secondary clients injected."""
    router = LLMRouter(cfg)
    router._primary = _FakeClient("primary")
    router._secondary = _FakeClient("secondary")
    return router


def _cfg(secondary: bool = False, roles: list[str] | None = None) -> LLMConfig:
    if not secondary:
        return LLMConfig()
    return LLMConfig(
        secondary={
            "model": "sec-model",
            "roles": roles,
        }
    )


@pytest.mark.asyncio
async def test_no_secondary_resolves_everything_to_primary() -> None:
    router = await _enter_router(_cfg())
    for role in LLMRole:
        r = router.resolve(role)
        assert r.endpoint == "primary"
        assert r.client.name == "primary"
        assert r.model == "qwen3.5-122b"
        assert r.max_context_tokens == 131072
    # Vision always primary too
    r = router.resolve(LLMRole.ANALYSIS, has_images=True)
    assert r.endpoint == "primary"
    assert r.client.name == "primary"
    assert r.model == "qwen3.5-122b"


@pytest.mark.asyncio
async def test_secondary_default_routes_all_text_roles() -> None:
    router = await _enter_router(_cfg(secondary=True))
    for role in LLMRole:
        r = router.resolve(role)
        assert r.endpoint == "secondary"
        assert isinstance(r.client, FallbackClient)
        assert r.client._secondary.name == "secondary"
        assert r.model == "sec-model"
        assert r.max_context_tokens == 131072


@pytest.mark.asyncio
async def test_roles_list_narrows_routing() -> None:
    router = await _enter_router(_cfg(secondary=True, roles=["planner", "critic"]))
    assert router.resolve(LLMRole.PLANNER).endpoint == "secondary"
    assert router.resolve(LLMRole.CRITIC).endpoint == "secondary"
    assert router.resolve(LLMRole.WRITER).endpoint == "primary"
    assert router.resolve(LLMRole.RESEARCHER).endpoint == "primary"


@pytest.mark.asyncio
async def test_images_never_route_to_secondary() -> None:
    router = await _enter_router(_cfg(secondary=True))
    # Even a role covered by the secondary must stay on primary when images exist.
    r = router.resolve(LLMRole.ANALYSIS, has_images=True)
    assert r.endpoint == "primary"
    assert r.client.name == "primary"
    assert r.model == "qwen3.5-122b"
    # And the text-only version of the same role does route to secondary.
    r = router.resolve(LLMRole.ANALYSIS, has_images=False)
    assert r.endpoint == "secondary"


@pytest.mark.asyncio
async def test_disabled_secondary_acts_like_primary_only() -> None:
    router = await _enter_router(LLMConfig(secondary={"enabled": False}))
    assert router.has_secondary is False
    r = router.resolve(LLMRole.PLANNER)
    assert r.endpoint == "primary"


@pytest.mark.asyncio
async def test_fallback_client_retries_on_primary() -> None:
    from unittest.mock import AsyncMock, MagicMock

    sec = MagicMock()
    sec.chat.completions.create = AsyncMock(side_effect=RuntimeError("sec down"))
    prim = MagicMock()
    prim.chat.completions.create = AsyncMock(return_value="primary-ok")

    fb = FallbackClient(sec, prim, "prim-model")
    out = await fb.chat.completions.create(model="sec-model", messages=[])
    assert out == "primary-ok"
    # The fallback swapped the model to the primary's and called primary.
    prim.chat.completions.create.assert_awaited_once_with(
        model="prim-model", messages=[]
    )


@pytest.mark.asyncio
async def test_fallback_client_passes_through_on_secondary_success() -> None:
    from unittest.mock import AsyncMock, MagicMock

    sec = MagicMock()
    sec.chat.completions.create = AsyncMock(return_value="sec-ok")
    prim = MagicMock()

    fb = FallbackClient(sec, prim, "prim-model")
    out = await fb.chat.completions.create(model="sec-model", messages=[])
    assert out == "sec-ok"
    prim.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
async def test_circuit_breaker_shared_across_fallback_clients() -> None:
    """After one endpoint-level failure, all FallbackClients sharing a breaker
    skip the secondary entirely — no re-wait on a dead endpoint."""
    from unittest.mock import AsyncMock, MagicMock

    sec = MagicMock()
    sec.chat.completions.create = AsyncMock(side_effect=RuntimeError("sec down"))
    prim = MagicMock()
    prim.chat.completions.create = AsyncMock(return_value="primary-ok")

    breaker = _SecondaryBreaker()
    fb1 = FallbackClient(sec, prim, "prim-model", breaker=breaker)
    fb2 = FallbackClient(sec, prim, "prim-model", breaker=breaker)

    assert await fb1.chat.completions.create(model="sec-model", messages=[]) == "primary-ok"
    assert breaker.failed is True
    # Second client (e.g. another parallel researcher) goes straight to primary.
    assert await fb2.chat.completions.create(model="sec-model", messages=[]) == "primary-ok"
    assert sec.chat.completions.create.await_count == 1  # secondary only attempted once
    assert prim.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_request_level_error_does_not_open_circuit() -> None:
    """A 400-class rejection (e.g. payload too large for the secondary) retries
    on the primary but leaves the secondary in play for smaller calls."""
    from unittest.mock import AsyncMock, MagicMock

    from openai import BadRequestError

    sec = MagicMock()
    sec.chat.completions.create = AsyncMock(
        side_effect=[
            BadRequestError(
                "payload too large", response=MagicMock(status_code=400), body={}
            ),
            "sec-ok",
        ]
    )
    prim = MagicMock()
    prim.chat.completions.create = AsyncMock(return_value="primary-ok")

    breaker = _SecondaryBreaker()
    fb = FallbackClient(sec, prim, "prim-model", breaker=breaker)

    assert await fb.chat.completions.create(model="sec-model", messages=[]) == "primary-ok"
    assert breaker.failed is False, "request-level error must not open the circuit"
    # Next call still tries the secondary (which now succeeds).
    assert await fb.chat.completions.create(model="sec-model", messages=[]) == "sec-ok"
    assert sec.chat.completions.create.await_count == 2
    assert prim.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_router_enters_and_closes_clients() -> None:
    with pytest.MonkeyPatch.context() as mp:
        import deep_research.llm.router as router_mod

        created: list[dict] = []
        closed: list[str] = []

        class _FakeOpenAI:
            def __init__(self, **kwargs) -> None:
                self.base_url = kwargs.get("base_url", "")
                created.append(
                    {"base_url": self.base_url, "max_retries": kwargs.get("max_retries")}
                )

            async def close(self) -> None:
                self.closed = True
                closed.append(self.base_url)

        mp.setattr(router_mod, "AsyncOpenAI", _FakeOpenAI)

        cfg = LLMConfig(
            secondary={"base_url": "http://sec.test/v1", "model": "sec"},
        )
        async with LLMRouter(cfg) as router:
            assert created == [
                {"base_url": "http://localhost:8000/v1", "max_retries": 2},
                {"base_url": "http://sec.test/v1", "max_retries": 0},
            ]
            assert router.has_secondary is True
        assert closed == ["http://localhost:8000/v1", "http://sec.test/v1"]
