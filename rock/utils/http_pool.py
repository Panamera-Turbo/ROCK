"""Config-driven registry of persistent httpx.AsyncClient pools.

Usage:
    pool = HttpPoolManager(rock_config.http_pools)
    client = pool.get("probe")  # -> httpx.AsyncClient, reused on subsequent calls
    await pool.aclose_all()     # graceful shutdown

Each pool name maps to an HttpPoolConfig in RockConfig.http_pools. Clients are
created lazily on first access and reused for the process lifetime.
"""

import httpx

from rock.config import HttpPoolConfig
from rock.logger import init_logger

logger = init_logger(__name__)


class HttpPoolManager:
    def __init__(self, pool_configs: dict[str, HttpPoolConfig]) -> None:
        self._configs = pool_configs
        self._clients: dict[str, httpx.AsyncClient] = {}

    def get(self, name: str) -> httpx.AsyncClient:
        """Return the named pool's client, creating it on first access."""
        client = self._clients.get(name)
        if client is not None and not client.is_closed:
            return client
        cfg = self._configs.get(name)
        if cfg is None:
            cfg = HttpPoolConfig()  # sensible default
            logger.warning(f"http pool '{name}' not configured, using defaults")
        self._clients[name] = httpx.AsyncClient(
            timeout=cfg.timeout,
            limits=httpx.Limits(
                max_connections=cfg.max_connections,
                max_keepalive_connections=cfg.max_keepalive_connections,
            ),
        )
        logger.info(
            f"http pool '{name}' created: timeout={cfg.timeout}s, "
            f"max_conn={cfg.max_connections}, max_keepalive={cfg.max_keepalive_connections}"
        )
        return self._clients[name]

    async def aclose_all(self) -> None:
        """Close all open clients. Safe to call multiple times."""
        for name, client in self._clients.items():
            if not client.is_closed:
                await client.aclose()
                logger.info(f"http pool '{name}' closed")
        self._clients.clear()
