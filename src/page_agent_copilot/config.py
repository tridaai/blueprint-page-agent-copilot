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

The deployment profile (round 16) is the second governed decision
an FDE makes on site: HOW the library reaches the page (the
committed bundle, or a pinned CDN script) and WHERE the model lives
(a hosted endpoint, or one inside the customer's own network). The
profile is named configuration, validated loud at load — a CDN pin
that does not match the package this repo bundles is refused, never
silently served — and recorded on every execution attempt, so the
audit can answer which deployment ran a task.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlparse


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def store_profile() -> tuple[str, str]:
    """(kind, target): ("postgres", dsn) or ("sqlite", path)."""
    dsn = _env("DATABASE_URL")
    if dsn:
        return "postgres", dsn
    return "sqlite", _env("SQLITE_PATH", "data/copilot.db")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Policy:
    """The governed limits. Every number a rule depends on is named
    here (and overridable by env), never buried in a prompt.

    The credit cap is a plan-time refusal, not a warning: a credit
    memo above it never becomes a plan, so no confirmation can ever
    approve one.

    The budget ceilings are the same kind of rule, at their own
    moments: a plan longer than `max_plan_steps` is refused at plan
    time; an execution attempt that would run more than
    `max_execution_steps` steps, or run past `max_execution_seconds`
    of wall clock, is stopped by the shell and lands as a failed
    execution naming the budget — never a silent overrun. The
    attempt is the budget unit: a resume or restart is a new
    attempt with a fresh budget, because what it may still do is
    bounded afresh, and the steps already standing are history the
    earlier attempts record, not spend the new one re-pays."""

    # The largest credit memo the copilot may plan. Above this the
    # plan is refused outright at plan time.
    credit_cap: Decimal = Decimal("500.00")
    # The longest plan the copilot may derive, in steps. A task
    # whose plan would run longer is refused at plan time.
    max_plan_steps: int = 25
    # The most steps one execution attempt may run. This is also
    # the value served to the console as PageAgent's `maxSteps` —
    # one source of truth, here.
    max_execution_steps: int = 40
    # The wall clock one execution attempt may span, in seconds,
    # measured from the attempt's start to each step's landing.
    max_execution_seconds: int = 1800
    # The clarification patience (round 14): the most DISTINCT
    # questions derivation may ask one task before a further
    # ambiguity refuses the task outright, history attached. Two
    # means: the first question, its answer, one more question —
    # a third distinct ambiguity lands REFUSED. Being asked again
    # about a question already asked (the records moved under a
    # pinned answer, so the choice must be re-made) does not
    # spend new patience.
    max_clarifications: int = 2

    @classmethod
    def from_env(cls) -> "Policy":
        base = cls()
        raw = _env("COPILOT_CREDIT_CAP")
        return cls(
            credit_cap=Decimal(raw) if raw else base.credit_cap,
            max_plan_steps=_env_int(
                "COPILOT_MAX_PLAN_STEPS", base.max_plan_steps),
            max_execution_steps=_env_int(
                "COPILOT_MAX_EXECUTION_STEPS",
                base.max_execution_steps),
            max_execution_seconds=_env_int(
                "COPILOT_MAX_EXECUTION_SECONDS",
                base.max_execution_seconds),
            max_clarifications=_env_int(
                "COPILOT_MAX_CLARIFICATIONS",
                base.max_clarifications))


# The page-agent version this repo pins (frontend/package.json) and
# bundles. A CDN deployment must serve exactly this version: the
# evals ran against it, so a different agent in the page would be a
# different deployment than the one that was graded. A test pins
# this constant to the package.json dependency.
PINNED_PAGE_AGENT_VERSION = "1.12.4"

# Where the committed bundle is served from — the local delivery.
LOCAL_BUNDLE_PATH = "/static/vendor/page-agent.bundle.js"


class ConfigError(ValueError):
    """A deployment configuration that does not validate. Raised
    at config load (and again at app startup): a misconfigured
    deployment fails loud, with a named code, before it serves
    anything — it never degrades into a quietly different agent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _url_origin(url: str) -> str:
    """scheme://host[:port] for a URL — the origin a CSP names."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _url_well_formed(url: str) -> bool:
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# The named profiles: name -> (bundle delivery, endpoint class).
# Delivery says how the library reaches the page; the endpoint
# class says where the model lives. The four combinations are the
# deployment decisions; anything else is not a profile.
PROFILES: dict[str, tuple[str, str]] = {
    "local-hosted": ("local", "hosted"),
    "local-self-hosted": ("local", "self_hosted"),
    "cdn-hosted": ("cdn", "hosted"),
    "cdn-self-hosted": ("cdn", "self_hosted"),
}

DEFAULT_PROFILE = "local-hosted"

_CDN_VERSION_RE = re.compile(r"page-agent@(\d+\.\d+\.\d+)")


@dataclass(frozen=True)
class DeploymentProfile:
    """How the agent ships and where its model lives, as governed
    configuration.

    `delivery` is `local` (the committed bundle, built from the
    pinned npm package) or `cdn` (a script URL the deployment
    names in `COPILOT_CDN_URL`, served into the page instead —
    its `page-agent@<version>` pin must equal
    `PINNED_PAGE_AGENT_VERSION`, checked at load).

    `endpoint_class` is `hosted` (an OpenAI-compatible endpoint
    outside the customer's network) or `self_hosted` (an endpoint
    inside it — a NIM container, a vLLM service). The key rule
    follows the class: a hosted endpoint, once one is configured,
    REQUIRES a key; a self-hosted endpoint often runs keyless on a
    trusted network, so its key is optional. The key itself is
    never part of the profile, never recorded, never echoed — the
    profile names the class, not the secret.
    """

    name: str = DEFAULT_PROFILE
    delivery: str = "local"
    endpoint_class: str = "hosted"
    cdn_url: str = ""

    @classmethod
    def default(cls) -> "DeploymentProfile":
        delivery, endpoint_class = PROFILES[DEFAULT_PROFILE]
        return cls(name=DEFAULT_PROFILE, delivery=delivery,
                   endpoint_class=endpoint_class)

    @property
    def bundle_url(self) -> str:
        """The script the console page loads the agent from."""
        if self.delivery == "cdn":
            return self.cdn_url
        return LOCAL_BUNDLE_PATH

    def validate(self, page_agent: "PageAgentConfig") -> None:
        """Refuse a deployment that does not hold together, with
        the reason named. Called at config load and at app
        startup, so a bad profile fails before anything serves."""
        if self.delivery == "cdn":
            if not self.cdn_url:
                raise ConfigError(
                    "cdn_url_missing",
                    f"profile '{self.name}' delivers the agent "
                    f"from a CDN but COPILOT_CDN_URL is not set: "
                    f"name the pinned script URL the page should "
                    f"load (page-agent@{PINNED_PAGE_AGENT_VERSION})")
            if not _url_well_formed(self.cdn_url):
                raise ConfigError(
                    "cdn_url_invalid",
                    f"COPILOT_CDN_URL '{self.cdn_url}' is not an "
                    f"http(s) URL the page can load")
            match = _CDN_VERSION_RE.search(self.cdn_url)
            if match is None:
                raise ConfigError(
                    "cdn_url_unpinned",
                    f"COPILOT_CDN_URL '{self.cdn_url}' carries no "
                    f"page-agent@<version> pin: the shell cannot "
                    f"prove the page would load the agent the "
                    f"evals ran against "
                    f"(page-agent@{PINNED_PAGE_AGENT_VERSION})")
            if match.group(1) != PINNED_PAGE_AGENT_VERSION:
                raise ConfigError(
                    "cdn_version_mismatch",
                    f"COPILOT_CDN_URL pins page-agent@"
                    f"{match.group(1)} but this blueprint bundles "
                    f"and grades page-agent@"
                    f"{PINNED_PAGE_AGENT_VERSION}: refusing to "
                    f"serve a different agent than the one the "
                    f"evals ran against")
        if page_agent.base_url and not _url_well_formed(
                page_agent.base_url):
            raise ConfigError(
                "base_url_invalid",
                f"PAGEAGENT_BASE_URL '{page_agent.base_url}' is "
                f"not a parseable http(s) URL: the in-page agent "
                f"could not call the model at that address")
        if (self.endpoint_class == "hosted"
                and (page_agent.model or page_agent.base_url)
                and not page_agent.api_key):
            raise ConfigError(
                "hosted_key_missing",
                f"profile '{self.name}' runs the model at a "
                f"hosted endpoint but PAGEAGENT_API_KEY is not "
                f"set: a hosted endpoint requires a key (a "
                f"self-hosted endpoint inside the customer's "
                f"network may run keyless — see the "
                f"*-self-hosted profiles)")

    def content_security_policy(
            self, page_agent: "PageAgentConfig") -> str:
        """The Content-Security-Policy the app serves, computed
        from the profile in code.

        `script-src 'self'` covers the committed bundle; a CDN
        profile adds exactly the pinned CDN's origin — no other
        script origin is ever named. `connect-src` names 'self'
        plus the model endpoint's origin, deliberately: the agent
        runs IN the page and calls the model from the browser, so
        the hole is stated, scoped to the one configured endpoint,
        and moves when the profile does. The console's own styles
        are inline (style-src 'unsafe-inline') and its footer
        wordmark is served from trida.ai (img-src) — both stated
        rather than pretended away."""
        script_src = ["'self'"]
        if self.delivery == "cdn" and self.cdn_url:
            script_src.append(_url_origin(self.cdn_url))
        connect_src = ["'self'"]
        if page_agent.base_url and _url_well_formed(
                page_agent.base_url):
            connect_src.append(_url_origin(page_agent.base_url))
        return "; ".join([
            "default-src 'self'",
            f"script-src {' '.join(script_src)}",
            f"connect-src {' '.join(connect_src)}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' https://trida.ai data:",
        ])

    @classmethod
    def from_env(cls, page_agent: "PageAgentConfig"
                 ) -> "DeploymentProfile":
        name = _env("COPILOT_PROFILE", DEFAULT_PROFILE)
        if name not in PROFILES:
            raise ConfigError(
                "profile_unknown",
                f"COPILOT_PROFILE names '{name}', which is not a "
                f"deployment profile; the profiles are: "
                f"{', '.join(sorted(PROFILES))}")
        delivery, endpoint_class = PROFILES[name]
        profile = cls(name=name, delivery=delivery,
                      endpoint_class=endpoint_class,
                      cdn_url=_env("COPILOT_CDN_URL"))
        profile.validate(page_agent)
        return profile


@dataclass(frozen=True)
class PageAgentConfig:
    """What the console page needs to construct `new PageAgent(...)`.

    Mirrors the package's own LLMConfig (verified against
    page-agent@1.12.4's types): `baseURL` and `model` are required by
    the library; `apiKey` is optional (some endpoints need none). The
    key, when set server-side, is served to the operator's own page
    for the demo posture — a deployment should instead let the
    operator supply it in the page. See README "Honest status".

    The library's `maxSteps` is deliberately NOT here: the step
    ceiling is the shell's governed budget (`Policy.max_execution_
    steps`), and `/api/agent-config` serves that value as
    `maxSteps` — one source of truth for the number, in Policy.
    """

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    language: str = "en-US"

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url)

    @classmethod
    def from_env(cls) -> "PageAgentConfig":
        base = cls()
        return cls(
            model=_env("PAGEAGENT_MODEL", base.model),
            base_url=_env("PAGEAGENT_BASE_URL", base.base_url),
            api_key=_env("PAGEAGENT_API_KEY", base.api_key),
            language=_env("PAGEAGENT_LANGUAGE", base.language),
        )


@dataclass(frozen=True)
class Settings:
    store_kind: str
    store_target: str
    policy: Policy
    page_agent: PageAgentConfig
    # The deployment profile (round 16). The default keeps every
    # construction that predates profiles valid; `from_env`
    # resolves the named profile and validates it against the
    # PageAgent settings, and the app validates again at startup.
    profile: DeploymentProfile = field(
        default_factory=DeploymentProfile.default)

    def validate(self) -> None:
        """The startup gate: the profile must hold against the
        PageAgent settings it will run with."""
        self.profile.validate(self.page_agent)

    @classmethod
    def from_env(cls) -> "Settings":
        kind, target = store_profile()
        page_agent = PageAgentConfig.from_env()
        return cls(
            store_kind=kind,
            store_target=target,
            policy=Policy.from_env(),
            page_agent=page_agent,
            profile=DeploymentProfile.from_env(page_agent),
        )
