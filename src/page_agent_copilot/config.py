"""Environment-driven configuration.

One decision lives here and nowhere else: which store profile is
running (`store_profile`). `DATABASE_URL` set means PostgreSQL +
pgvector (production); otherwise a single SQLite file is the local
default.

The PageAgent LLM settings are the browser agent's own configuration
(`new PageAgent({ model, baseURL, apiKey, language })` in the page).
The shell never calls that LLM itself; it serves the non-secret parts
to the console so the operator's page can construct the agent, and
the offline driver remains the default everywhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def store_profile() -> tuple[str, str]:
    """(kind, target): ("postgres", dsn) or ("sqlite", path)."""
    dsn = _env("DATABASE_URL")
    if dsn:
        return "postgres", dsn
    return "sqlite", _env("SQLITE_PATH", "data/copilot.db")


@dataclass(frozen=True)
class Policy:
    """The governed limits. Every number a rule depends on is named
    here (and overridable by env), never buried in a prompt.

    The credit cap is a plan-time refusal, not a warning: a credit
    memo above it never becomes a plan, so no confirmation can ever
    approve one."""

    # The largest credit memo the copilot may plan. Above this the
    # plan is refused outright at plan time.
    credit_cap: Decimal = Decimal("500.00")

    @classmethod
    def from_env(cls) -> "Policy":
        raw = _env("COPILOT_CREDIT_CAP")
        return cls(credit_cap=Decimal(raw) if raw else cls().credit_cap)


@dataclass(frozen=True)
class PageAgentConfig:
    """What the console page needs to construct `new PageAgent(...)`.

    Mirrors the package's own LLMConfig (verified against
    page-agent@1.12.4's types): `baseURL` and `model` are required by
    the library; `apiKey` is optional (some endpoints need none). The
    key, when set server-side, is served to the operator's own page
    for the demo posture — a deployment should instead let the
    operator supply it in the page. See README "Honest status".
    """

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    language: str = "en-US"
    max_steps: int = 40

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url)

    @classmethod
    def from_env(cls) -> "PageAgentConfig":
        base = cls()
        raw_steps = _env("PAGEAGENT_MAX_STEPS")
        return cls(
            model=_env("PAGEAGENT_MODEL", base.model),
            base_url=_env("PAGEAGENT_BASE_URL", base.base_url),
            api_key=_env("PAGEAGENT_API_KEY", base.api_key),
            language=_env("PAGEAGENT_LANGUAGE", base.language),
            max_steps=int(raw_steps) if raw_steps else base.max_steps,
        )


@dataclass(frozen=True)
class Settings:
    store_kind: str
    store_target: str
    policy: Policy
    page_agent: PageAgentConfig

    @classmethod
    def from_env(cls) -> "Settings":
        kind, target = store_profile()
        return cls(
            store_kind=kind,
            store_target=target,
            policy=Policy.from_env(),
            page_agent=PageAgentConfig.from_env(),
        )
