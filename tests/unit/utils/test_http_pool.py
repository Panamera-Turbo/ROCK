"""Tests for HttpPoolManager."""

from rock.config import HttpPoolConfig
from rock.utils.http_pool import HttpPoolManager


def test_http_pool_manager_get_creates_client():
    """get() should create a client on first access."""
    configs = {"probe": HttpPoolConfig(timeout=5.0, max_connections=100, max_keepalive_connections=20)}
    manager = HttpPoolManager(configs)

    client = manager.get("probe")

    assert client is not None
    assert not client.is_closed


def test_http_pool_manager_get_reuses_client():
    """get() should return the same client on repeated calls."""
    configs = {"probe": HttpPoolConfig(timeout=5.0, max_connections=100, max_keepalive_connections=20)}
    manager = HttpPoolManager(configs)

    c1 = manager.get("probe")
    c2 = manager.get("probe")

    assert c1 is c2


async def test_http_pool_manager_get_creates_new_when_closed():
    """If the cached client is closed, get() should create a fresh one."""
    configs = {"probe": HttpPoolConfig(timeout=5.0, max_connections=100, max_keepalive_connections=20)}
    manager = HttpPoolManager(configs)

    c1 = manager.get("probe")
    await c1.aclose()
    assert c1.is_closed

    c2 = manager.get("probe")
    assert c2 is not c1
    assert not c2.is_closed


def test_http_pool_manager_get_with_missing_config():
    """get() should use defaults when pool name is not configured."""
    configs = {}
    manager = HttpPoolManager(configs)

    client = manager.get("unknown")

    assert client is not None
    assert not client.is_closed


async def test_http_pool_manager_aclose_all():
    """aclose_all() should close all open clients."""
    configs = {
        "probe": HttpPoolConfig(timeout=5.0, max_connections=100, max_keepalive_connections=20),
        "rpc": HttpPoolConfig(timeout=180.0, max_connections=500, max_keepalive_connections=100),
    }
    manager = HttpPoolManager(configs)

    c1 = manager.get("probe")
    c2 = manager.get("rpc")
    assert not c1.is_closed
    assert not c2.is_closed

    await manager.aclose_all()

    assert c1.is_closed
    assert c2.is_closed
    assert len(manager._clients) == 0


async def test_http_pool_manager_aclose_all_idempotent():
    """aclose_all() should be safe to call multiple times."""
    configs = {"probe": HttpPoolConfig(timeout=5.0, max_connections=100, max_keepalive_connections=20)}
    manager = HttpPoolManager(configs)

    manager.get("probe")

    await manager.aclose_all()
    await manager.aclose_all()

    assert len(manager._clients) == 0


def test_http_pool_config_custom_values():
    """HttpPoolConfig should accept custom values."""
    config = HttpPoolConfig(timeout=30.0, max_connections=500, max_keepalive_connections=100)

    assert config.timeout == 30.0
    assert config.max_connections == 500
    assert config.max_keepalive_connections == 100
