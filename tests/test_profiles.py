"""Deployment profiles: how the agent ships, where the model lives.

A named profile (`COPILOT_PROFILE`) selects two things an FDE
decides on site: the bundle delivery — the committed bundle built
from the pinned npm package, or a CDN script the deployment names —
and the model endpoint class, hosted or self-hosted. The profile
is governed configuration: a CDN pin that is not the bundled
version, a hosted endpoint with no key, an unparseable model
address, or an unknown profile name all refuse at config load,
loudly and by name. The app serves a Content-Security-Policy
computed from the profile, the console page loads the profile's
bundle source, and every execution attempt records the profile
it ran under (delivery, endpoint class, model name — never the
key), so the audit can answer which deployment ran a task.
Attempts from before profiles existed carry empty fields: their
deployment is unknown, not backfilled.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import (
    LOCAL_BUNDLE_PATH,
    PINNED_PAGE_AGENT_VERSION,
    PROFILES,
    ConfigError,
    DeploymentProfile,
    PageAgentConfig,
    Policy,
    Settings,
)
from page_agent_copilot.models import TaskStatus
from page_agent_copilot.service import CopilotService
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
CDN_URL = ("https://cdn.jsdelivr.net/npm/"
           f"page-agent@{PINNED_PAGE_AGENT_VERSION}"
           "/dist/iife/page-agent.demo.js")
CDN_ORIGIN = "https://cdn.jsdelivr.net"
HOSTED = PageAgentConfig(
    model="qwen-plus",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key="sk-test-key")
SELF_HOSTED = PageAgentConfig(
    model="llama3.1", base_url="http://localhost:8000/v1")


def profile(name: str, cdn_url: str = "") -> DeploymentProfile:
    delivery, endpoint_class = PROFILES[name]
    return DeploymentProfile(name=name, delivery=delivery,
                             endpoint_class=endpoint_class,
                             cdn_url=cdn_url)


def settings_for(prof: DeploymentProfile,
                 page_agent: PageAgentConfig) -> Settings:
    return Settings(store_kind="memory", store_target="",
                    policy=Policy(), page_agent=page_agent,
                    profile=prof)


def client_for(prof: DeploymentProfile,
               page_agent: PageAgentConfig) -> TestClient:
    return TestClient(create_app(
        settings_for(prof, page_agent), InMemoryStore(), seed=True))


# -- the pin ----------------------------------------------------------------

def test_pin_constant_matches_the_frontend_package():
    package = json.loads(
        (Path(__file__).resolve().parents[1] / "frontend"
         / "package.json").read_text())
    assert package["dependencies"]["page-agent"] == \
        PINNED_PAGE_AGENT_VERSION


def test_profiles_are_the_four_deployment_combinations():
    assert PROFILES == {
        "local-hosted": ("local", "hosted"),
        "local-self-hosted": ("local", "self_hosted"),
        "cdn-hosted": ("cdn", "hosted"),
        "cdn-self-hosted": ("cdn", "self_hosted"),
    }
    default = DeploymentProfile.default()
    assert default.name == "local-hosted"
    assert default.bundle_url == LOCAL_BUNDLE_PATH


# -- validation: misconfiguration fails loud, by name ------------------------

def test_unknown_profile_name_refused(monkeypatch):
    monkeypatch.setenv("COPILOT_PROFILE", "satellite-uplink")
    with pytest.raises(ConfigError) as caught:
        DeploymentProfile.from_env(PageAgentConfig())
    assert caught.value.code == "profile_unknown"
    assert "satellite-uplink" in caught.value.message


def test_cdn_profile_without_a_url_refused():
    with pytest.raises(ConfigError) as caught:
        profile("cdn-hosted").validate(HOSTED)
    assert caught.value.code == "cdn_url_missing"


def test_cdn_url_without_a_version_pin_refused():
    with pytest.raises(ConfigError) as caught:
        profile("cdn-hosted",
                "https://cdn.example.com/page-agent.js").validate(
                    HOSTED)
    assert caught.value.code == "cdn_url_unpinned"


def test_cdn_version_mismatch_refused_with_both_versions_named():
    with pytest.raises(ConfigError) as caught:
        profile("cdn-hosted",
                "https://unpkg.com/page-agent@1.9.0/dist/iife/"
                "page-agent.demo.js").validate(HOSTED)
    assert caught.value.code == "cdn_version_mismatch"
    assert "1.9.0" in caught.value.message
    assert PINNED_PAGE_AGENT_VERSION in caught.value.message


def test_cdn_url_that_is_not_a_url_refused():
    with pytest.raises(ConfigError) as caught:
        profile("cdn-hosted",
                f"ftp://cdn.example.com/page-agent@"
                f"{PINNED_PAGE_AGENT_VERSION}/x.js").validate(HOSTED)
    assert caught.value.code == "cdn_url_invalid"


def test_hosted_endpoint_without_a_key_refused():
    keyless = PageAgentConfig(
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    with pytest.raises(ConfigError) as caught:
        profile("local-hosted").validate(keyless)
    assert caught.value.code == "hosted_key_missing"


def test_hosted_with_nothing_configured_is_the_offline_default():
    # No model, no endpoint, no key: the live path is simply not
    # configured (the demo posture) — not a misconfiguration.
    profile("local-hosted").validate(PageAgentConfig())


def test_self_hosted_keyless_validates():
    profile("local-self-hosted").validate(SELF_HOSTED)


def test_self_hosted_unparseable_base_url_refused():
    broken = PageAgentConfig(model="llama3.1",
                             base_url="localhost:8000")
    with pytest.raises(ConfigError) as caught:
        profile("local-self-hosted").validate(broken)
    assert caught.value.code == "base_url_invalid"
    assert "localhost:8000" in caught.value.message


def test_profile_from_env_resolves_and_validates(monkeypatch):
    monkeypatch.setenv("COPILOT_PROFILE", "cdn-self-hosted")
    monkeypatch.setenv("COPILOT_CDN_URL", CDN_URL)
    resolved = DeploymentProfile.from_env(SELF_HOSTED)
    assert resolved.name == "cdn-self-hosted"
    assert resolved.delivery == "cdn"
    assert resolved.endpoint_class == "self_hosted"
    assert resolved.bundle_url == CDN_URL


def test_app_startup_refuses_a_misconfigured_deployment():
    with pytest.raises(ConfigError) as caught:
        create_app(settings_for(profile("cdn-hosted"), HOSTED),
                   InMemoryStore(), seed=True)
    assert caught.value.code == "cdn_url_missing"


# -- the CSP, computed from the profile ---------------------------------------

def test_csp_local_profile_is_self_only_for_scripts():
    policy = profile("local-hosted").content_security_policy(
        PageAgentConfig())
    assert "script-src 'self';" in policy
    assert CDN_ORIGIN not in policy
    assert "connect-src 'self';" in policy


def test_csp_connect_src_names_the_configured_model_origin():
    policy = profile("local-hosted").content_security_policy(HOSTED)
    assert "connect-src 'self' https://dashscope.aliyuncs.com" \
        in policy


def test_csp_cdn_profile_names_exactly_the_cdn_origin():
    policy = profile("cdn-hosted", CDN_URL) \
        .content_security_policy(HOSTED)
    assert f"script-src 'self' {CDN_ORIGIN}" in policy


def test_csp_header_is_served_and_matches_the_profile():
    client = client_for(profile("cdn-hosted", CDN_URL), HOSTED)
    expected = profile("cdn-hosted", CDN_URL) \
        .content_security_policy(HOSTED)
    for path in ("/", "/api/agent-config", "/health"):
        assert client.get(path).headers[
            "content-security-policy"] == expected


# -- delivery: the page loads the profile's bundle ----------------------------

def test_console_page_loads_the_local_bundle_by_default():
    client = client_for(profile("local-hosted"), PageAgentConfig())
    page = client.get("/").text
    assert f'src="{LOCAL_BUNDLE_PATH}"' in page
    assert CDN_URL not in page


def test_console_page_loads_the_cdn_bundle_under_a_cdn_profile():
    client = client_for(profile("cdn-hosted", CDN_URL), HOSTED)
    page = client.get("/").text
    assert f'src="{CDN_URL}"' in page
    assert LOCAL_BUNDLE_PATH not in page


# -- the config read carries the profile ---------------------------------------

def test_agent_config_carries_the_profile():
    client = client_for(profile("cdn-hosted", CDN_URL), HOSTED)
    cfg = client.get("/api/agent-config").json()
    assert cfg["profile"] == "cdn-hosted"
    assert cfg["delivery"] == "cdn"
    assert cfg["endpointClass"] == "hosted"
    assert cfg["bundleURL"] == CDN_URL
    assert cfg["csp"] == client.get("/").headers[
        "content-security-policy"]
    # The library's own fields are untouched.
    assert cfg["model"] == "qwen-plus"
    assert cfg["baseURL"] == HOSTED.base_url


def test_readiness_names_the_profile():
    client = client_for(profile("local-self-hosted"), SELF_HOSTED)
    assert client.get("/readiness").json()["profile"] == \
        "local-self-hosted"


# -- attempts record the deployment ----------------------------------------------

def _run_one(settings: Settings) -> CopilotService:
    svc = CopilotService(settings, InMemoryStore())
    svc.seed()
    task = svc.intake(CREDIT, "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    return svc


def test_attempt_records_the_profile_it_ran_under():
    svc = _run_one(settings_for(profile("local-self-hosted"),
                                SELF_HOSTED))
    (attempt,) = svc.get_attempts(
        svc.list_tasks()[0].task_id)
    assert attempt.deployment_profile == "local-self-hosted"
    assert attempt.bundle_delivery == "local"
    assert attempt.endpoint_class == "self_hosted"
    assert attempt.model == "llama3.1"


def test_the_key_is_never_on_the_attempt_record():
    svc = _run_one(settings_for(profile("local-hosted"), HOSTED))
    task = svc.list_tasks()[0]
    (attempt,) = svc.get_attempts(task.task_id)
    assert attempt.model == "qwen-plus"
    assert "sk-test-key" not in attempt.model_dump_json()
    assert "sk-test-key" not in json.dumps(
        svc.audit(task.task_id), default=str)


def test_attempts_from_before_profiles_read_as_unknown():
    svc = _run_one(settings_for(profile("local-hosted"), HOSTED))
    task = svc.list_tasks()[0]
    raw = svc.store.list_records("executions")[0]
    for key in ("deployment_profile", "bundle_delivery",
                "endpoint_class", "model"):
        raw.pop(key, None)
    svc.store.upsert_record("executions", raw)
    (attempt,) = svc.get_attempts(task.task_id)
    assert attempt.deployment_profile == ""
    assert attempt.bundle_delivery == ""
    assert attempt.endpoint_class == ""
    assert attempt.model == ""
