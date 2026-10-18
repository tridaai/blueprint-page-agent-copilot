import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SQLITE_PATH", "data/test-copilot.db")

import pytest  # noqa: E402

from page_agent_copilot.config import Settings  # noqa: E402
from page_agent_copilot.service import CopilotService  # noqa: E402
from page_agent_copilot.store import InMemoryStore, SqlStore  # noqa: E402


@pytest.fixture()
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture()
def service(settings) -> CopilotService:
    svc = CopilotService(settings, InMemoryStore())
    svc.seed()
    return svc


@pytest.fixture()
def sqlite_service(settings, tmp_path) -> CopilotService:
    store = SqlStore("sqlite", str(tmp_path / "copilot.db"))
    svc = CopilotService(settings, store)
    svc.seed()
    return svc
