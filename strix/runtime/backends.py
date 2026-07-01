"""Sandbox backend registry — selected via STRIX_RUNTIME_BACKEND (default: docker)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from agents.sandbox.manifest import Manifest


logger = logging.getLogger(__name__)


SandboxBackend = Callable[..., Awaitable[tuple[Any, Any]]]


async def _docker_backend(
    *,
    image: str,
    manifest: Manifest,
    exposed_ports: tuple[int, ...],
    bind_mounts: list[dict[str, Any]] | None = None,
) -> tuple[Any, Any]:
    """Bring up a session backed by the local Docker daemon.

    Uses :class:`StrixDockerSandboxClient` to inject NET_ADMIN /
    NET_RAW caps + ``host.docker.internal`` host-gateway. Imports
    ``docker`` lazily so deployments that target a non-Docker
    backend don't need the docker-py library installed.

    ``session.start()`` is what materializes the manifest entries
    (LocalDir copies and manifest-declared volume/FUSE mounts) into the
    running container — the SDK's ``client.create()`` only builds the inner
    session object without applying the manifest. ``async with session:``
    would call it too, but Strix manages session lifetime explicitly via
    ``client.delete()`` so we trigger ``start()`` ourselves.

    ``bind_mounts`` are host directories (e.g. large repos passed via
    ``--mount``) bind-mounted read-only; unlike manifest entries they are
    applied by Docker at container-create time, not by ``start()``.
    """
    import docker
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    from strix.runtime.docker_client import StrixDockerSandboxClient

    client = StrixDockerSandboxClient(docker.from_env())
    client.strix_bind_mounts = bind_mounts or []
    options = DockerSandboxClientOptions(image=image, exposed_ports=exposed_ports)
    session = await client.create(options=options, manifest=manifest)
    await session.start()
    return client, session


async def _local_backend(
    *,
    image: str,
    manifest: Manifest,
    exposed_ports: tuple[int, ...],
    bind_mounts: list[dict[str, Any]] | None = None,
) -> tuple[Any, Any]:
    """Bring up a session that runs commands directly on the host.

    Backed by the SDK's :class:`UnixLocalSandboxClient`, which executes
    every ``session.exec`` via a host subprocess with the working
    directory rooted at ``manifest.root``. No Docker daemon, no image,
    no container — intended for static/whitebox code review where the
    agent only needs to read and grep a local source tree.

    ``image`` and ``bind_mounts`` are accepted for interface parity with
    :func:`_docker_backend` but ignored: there is no image to pull and no
    container to bind into. The workspace is whatever ``manifest.root``
    points at (the caller sets it to the source repo for zero-copy). We
    do NOT populate manifest entries in local mode, so ``session.start()``
    only ensures the root directory exists — it never copies files.

    ⚠️ Commands run with the current user's privileges on the host. On
    macOS the SDK confines them to ``manifest.root`` via ``sandbox-exec``;
    on Linux there is no confinement. Only point this at code you trust.
    """
    del image, bind_mounts  # interface parity with _docker_backend; unused locally

    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    client = UnixLocalSandboxClient()
    options = UnixLocalSandboxClientOptions(exposed_ports=exposed_ports)
    session = await client.create(options=options, manifest=manifest)
    await session.start()
    return client, session


_BACKENDS: dict[str, SandboxBackend] = {
    "docker": _docker_backend,
    "local": _local_backend,
}


def get_backend(name: str) -> SandboxBackend:
    """Return the backend factory for ``name`` or raise.

    Args:
        name: Backend identifier (e.g. ``"docker"``). Match is exact;
            no fallback. Unknown values raise so config typos surface
            immediately instead of silently picking a default.
    """
    backend = _BACKENDS.get(name)
    if backend is None:
        supported = ", ".join(sorted(_BACKENDS))
        raise ValueError(
            f"Unknown STRIX_RUNTIME_BACKEND: {name!r} (supported: {supported})",
        )
    logger.debug("Selected sandbox backend: %s", name)
    return backend


def register_backend(name: str, backend: SandboxBackend) -> None:
    """Register a custom backend under ``name``.

    Intended for downstream users who ship their own runtime — register
    before any ``session_manager.create_or_reuse`` call. Re-registering
    an existing name overwrites the prior entry.
    """
    _BACKENDS[name] = backend
    logger.info("Registered sandbox backend: %s", name)


def supported_backends() -> list[str]:
    return sorted(_BACKENDS)
