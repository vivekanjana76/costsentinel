"""Shared fixtures.

Every fixture here keeps the suite offline: the mock provider, the fake model, and a
temporary state directory so no test writes to the repository's ``.costsentinel``.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from costsentinel.config import LLMBackend, RunMode, Settings
from costsentinel.graph.checkpointer import sqlite_checkpointer
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.fake import FakeLLM
from costsentinel.providers.mock import MockAzureProvider


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings pointed at a temporary state directory."""
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        state_dir=tmp_path / "state",
        mock_seed=1337,
    )


@pytest.fixture
def provider() -> MockAzureProvider:
    """The deterministic synthetic estate."""
    return MockAzureProvider(seed=1337)


@pytest.fixture
def llm() -> FakeLLM:
    """The deterministic fake model."""
    return FakeLLM()


@pytest.fixture
def policy() -> PolicyStore:
    """The built-in policy store."""
    return PolicyStore()


@pytest.fixture
def checkpointer(settings: Settings) -> Generator[BaseCheckpointSaver[str]]:
    """A real SQLite checkpointer in the temporary state directory."""
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        yield saver
