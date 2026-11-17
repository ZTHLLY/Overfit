"""The test suite is offline: an accidental service connection fails immediately."""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in offline tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture(autouse=True)
def reset_active_settings(monkeypatch, tmp_path_factory):
    """Never read the developer's ~/.config/overfit/.env, and never leak the
    process-wide settings course-directory mode installs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    yield
    from overfit.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_real_provider_clients(monkeypatch):
    """Fail before construction, not just when an accidental SDK call connects."""
    import openai

    def forbidden(*args, **kwargs):
        raise AssertionError("Real provider clients are forbidden in offline tests")

    monkeypatch.setattr(openai, "OpenAI", forbidden)
    monkeypatch.setattr(openai, "AsyncOpenAI", forbidden)
