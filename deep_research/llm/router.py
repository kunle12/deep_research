"""LLM routing — primary (vision-capable) + optional secondary text endpoint.

By default everything resolves to the primary endpoint (single-endpoint mode,
identical to the historical behavior). When `llm.secondary` is configured,
text-role calls (planner, researcher, critic, writer, analysis, ...) route to
it, while any call carrying images always stays on the primary vision model —
the secondary does not need vision capability.

Secondary calls are wrapped in a `FallbackClient` so a failing secondary
endpoint transparently retries once on the primary (with a warning), keeping
long unattended runs alive when the secondary is down. A **circuit breaker**
per run (shared across parallel researchers) means a dead secondary is only
attempted once — subsequent calls go straight to the primary, so a hung
endpoint cannot repeatedly eat the researcher's wall-clock budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, BadRequestError, UnprocessableEntityError

from deep_research.config import LLMConfig, LLMRole

logger = logging.getLogger(__name__)


class _SecondaryBreaker:
    """Per-run secondary-endpoint health, shared by every `FallbackClient`.

    `failed` opens the circuit (skip the secondary on all later calls);
    `warned` ensures the endpoint-down warning is logged once per run, not
    once per FallbackClient (parallel researchers each get their own client).
    """

    __slots__ = ("failed", "warned")

    def __init__(self) -> None:
        self.failed = False
        self.warned = False


# Request-level rejections mean the endpoint is alive but refused this specific
# call (e.g. the payload overflowed this model's context). Those retry on the
# primary WITHOUT opening the circuit, so a merely-undersized model stays in
# play for future, smaller calls. Anything else (connection, timeout, 5xx,
# auth, …) is treated as endpoint-level and opens the circuit.
_REQUEST_LEVEL_ERRORS = (BadRequestError, UnprocessableEntityError)


class FallbackClient:
    """Duck-typed stand-in for `AsyncOpenAI` exposing `chat.completions.create`.

    Tries the secondary endpoint first; on failure retries once on the primary
    with the model name swapped to the primary's. Once an *endpoint-level*
    failure has been seen, the shared circuit breaker opens and all later calls
    go straight to the primary (no re-wait). `breaker=None` gives each instance
    its own private breaker (used by tests and standalone callers).
    """

    def __init__(
        self,
        secondary: AsyncOpenAI,
        primary: AsyncOpenAI,
        primary_model: str,
        breaker: _SecondaryBreaker | None = None,
    ) -> None:
        self._secondary = secondary
        self._primary = primary
        self._primary_model = primary_model
        self._breaker = breaker if breaker is not None else _SecondaryBreaker()
        self.chat = _ChatNamespace(self)

    async def _create(self, **kwargs: Any) -> Any:
        breaker = self._breaker
        if breaker.failed:
            # Circuit open — the secondary already proved unreachable this run.
            # Go straight to the primary instead of waiting out the secondary's
            # timeout again on every call.
            kwargs = dict(kwargs)
            kwargs["model"] = self._primary_model
            return await self._primary.chat.completions.create(**kwargs)

        try:
            return await self._secondary.chat.completions.create(**kwargs)
        except Exception as e:
            if isinstance(e, _REQUEST_LEVEL_ERRORS):
                logger.debug(
                    "secondary rejected request (%s: %s); retrying on primary",
                    type(e).__name__,
                    e,
                )
            else:
                breaker.failed = True
                if not breaker.warned:
                    breaker.warned = True
                    logger.warning(
                        "secondary LLM call failed (%s: %s); falling back to "
                        "primary — further calls this run go straight to primary",
                        type(e).__name__,
                        e,
                    )
                else:
                    logger.debug(
                        "secondary LLM call failed again; falling back to primary",
                        exc_info=e,
                    )
            kwargs = dict(kwargs)
            kwargs["model"] = self._primary_model
            return await self._primary.chat.completions.create(**kwargs)


class _Completions:
    def __init__(self, owner: FallbackClient) -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> Any:
        return await self._owner._create(**kwargs)


class _ChatNamespace:
    def __init__(self, owner: FallbackClient) -> None:
        self.completions = _Completions(owner)


# Anything that serves the `client.chat.completions.create(...)` surface the
# nodes use — either a real AsyncOpenAI or the secondary->primary fallback
# wrapper.
LLMClientLike = AsyncOpenAI | FallbackClient


@dataclass(frozen=True)
class ResolvedLLM:
    """A routing decision: which client, model, and context window to use."""

    client: LLMClientLike
    model: str
    max_context_tokens: int
    endpoint: str  # "primary" | "secondary" — for logging/diagnostics


class LLMRouter:
    """Routes task roles to the primary or optional secondary LLM endpoint.

    Use as an async context manager so the caller controls lifetime:

        async with LLMRouter(config.llm) as router:
            r = router.resolve(LLMRole.PLANNER)
            resp = await r.client.chat.completions.create(model=r.model, ...)
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._primary: AsyncOpenAI | None = None
        self._secondary: AsyncOpenAI | None = None
        self._breaker = _SecondaryBreaker()

    async def __aenter__(self) -> LLMRouter:
        self._primary = AsyncOpenAI(
            base_url=self._cfg.base_url,
            api_key=self._cfg.api_key,
            timeout=self._cfg.timeout_s,
            max_retries=2,
        )
        sec = self._cfg.secondary
        if self._cfg.secondary_enabled and sec is not None:
            # Fresh breaker per session; the secondary client uses max_retries=0
            # so a hung endpoint fails after ONE timeout (the FallbackClient's
            # primary retry is the only retry). SDK-level retries would otherwise
            # multiply the burn: 2 retries x timeout_s per call.
            self._breaker = _SecondaryBreaker()
            self._secondary = AsyncOpenAI(
                base_url=sec.base_url,
                api_key=sec.api_key,
                timeout=sec.timeout_s,
                max_retries=0,
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._primary is not None:
            await self._primary.close()
            self._primary = None
        if self._secondary is not None:
            await self._secondary.close()
            self._secondary = None
        self._breaker = _SecondaryBreaker()

    @property
    def primary(self) -> AsyncOpenAI:
        if self._primary is None:
            raise RuntimeError("LLMRouter used outside of `async with` context")
        return self._primary

    @property
    def has_secondary(self) -> bool:
        return self._cfg.secondary_enabled and self._secondary is not None

    def resolve(self, role: LLMRole, *, has_images: bool = False) -> ResolvedLLM:
        """Resolve a task role to (client, model, max_context_tokens).

        `has_images=True` ALWAYS resolves to the primary vision model — the
        secondary endpoint may lack vision capability. Text roles resolve to
        the secondary when it is configured and the role is covered by its
        `roles` list (None = all text roles), else to the primary text model.
        """
        primary = self.primary
        if has_images:
            return ResolvedLLM(
                primary, self._cfg.vision_model, self._cfg.max_context_tokens, "primary"
            )
        sec = self._cfg.secondary
        if (
            self.has_secondary
            and sec is not None
            and self._secondary is not None
            and (sec.roles is None or role in sec.roles)
        ):
            client: LLMClientLike = FallbackClient(
                self._secondary,
                primary,
                self._cfg.text_model,
                breaker=self._breaker,
            )
            return ResolvedLLM(client, sec.model, sec.max_context_tokens, "secondary")
        return ResolvedLLM(
            primary, self._cfg.text_model, self._cfg.max_context_tokens, "primary"
        )


__all__ = ["FallbackClient", "LLMClientLike", "LLMRouter", "ResolvedLLM"]
