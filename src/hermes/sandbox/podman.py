"""Production sandbox backend: rootless Podman over its Docker-compatible socket.

Podman is chosen over the Docker daemon socket on purpose (Plan 11b-a):
daemonless (a sandbox crash can't take down a daemon the agent depends on) and
rootless (no root-equivalent host control mounted into the agent). The rootless
user socket speaks the Docker REST API, so this talks plain HTTP over a UDS and
stays the only Podman-aware module.

Not exercised by the default test suite — there is no Podman daemon in CI. It is
covered by opt-in integration tests and the manual verification in the plan.
"""

from __future__ import annotations

import io
import struct
import tarfile
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import structlog

from hermes.sandbox.errors import (
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxNotRunning,
)
from hermes.sandbox.models import (
    FILE_SIZE_CAP,
    ExecEvent,
    ExecExit,
    ExecOutput,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)

# Slack for the tar header (512B) + padding around the single-file archive.
# Any bytes past `FILE_SIZE_CAP + _READ_OVERHEAD_CAP` mean the payload itself
# is over cap, so we can bail mid-stream without parsing.
_READ_OVERHEAD_CAP = 4 * 1024
_FILE_SIZE_CAP = FILE_SIZE_CAP  # local alias for readability

logger = structlog.get_logger(__name__)

# Docker stream multiplexing: each frame is an 8-byte header
# [stream(1)][0][0][0][size(4, big-endian)] followed by `size` payload bytes.
_FRAME_HEADER = struct.Struct(">BxxxI")
_STREAM_STDOUT = 1
_STREAM_STDERR = 2


def _socket_path(socket_url: str) -> str:
    """Extract the UDS path from a unix:// url (or accept a bare path)."""
    if socket_url.startswith("unix://"):
        return urlparse(socket_url).path
    return socket_url


class PodmanSandboxBackend:
    def __init__(self, socket_url: str, *, disk_quota: bool = False) -> None:
        # Whether to apply the overlay disk quota — requires XFS+pquota, so it
        # is opt-in; ext4/btrfs reject `StorageOpt size` and fail the create.
        self._disk_quota = disk_quota
        # base_url host is ignored for UDS transport but required by httpx.
        # Finite default timeout on lifecycle calls so a wedged socket can't
        # block the single asyncio worker forever; only the exec *stream*
        # overrides read=None (long-running output is expected there).
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=_socket_path(socket_url)),
            base_url="http://podman",
            timeout=httpx.Timeout(30.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- lifecycle ---------------------------------------------------------

    # --- error-translation helpers ----------------------------------------
    # All client calls go through these so transport/HTTP errors surface as
    # SandboxError (the typed contract SandboxManager catches), not as raw
    # httpx exceptions that would bypass the manager and propagate into the
    # agent.

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SandboxError(f"{method} {path} failed: {exc}") from exc

    def _check(self, resp: httpx.Response, *, op: str) -> None:
        """raise_for_status equivalent that converts to SandboxError."""
        try:
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SandboxError(f"{op}: {resp.status_code} {resp.text}") from exc

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        # "none"/"" = no networking. The agent drives the sandbox over the
        # control socket (exec), so a sandbox needs no network — and with none
        # it provably cannot reach the agent, its DB/secrets, or other
        # sandboxes. Separate Podman networks are NOT isolated from each other
        # by default, so attaching to an "own" network would not be enough.
        no_network = spec.network in ("none", "")
        host_config: dict[str, object] = {
            "NanoCpus": int(spec.limits.cpus * 1_000_000_000),
            "Memory": spec.limits.memory_mb * 1024 * 1024,
            "NetworkMode": "none" if no_network else spec.network,
        }
        # Disk quota only when explicitly enabled on XFS+pquota storage —
        # otherwise the overlay driver rejects it and the create fails.
        if self._disk_quota:
            host_config["StorageOpt"] = {"size": f"{spec.limits.disk_mb}m"}
        body: dict[str, object] = {
            "Image": spec.image,
            # Idle entrypoint so the persistent container stays up for exec.
            "Cmd": ["sleep", "infinity"],
            "WorkingDir": "/workspace",
            "HostConfig": host_config,
        }
        if not no_network:
            body["NetworkingConfig"] = {"EndpointsConfig": {spec.network: {}}}
        # Workspace sandboxes bind a named volume at /workspace so files survive
        # a restart (the crash/OOM recovery path); ephemeral ones intentionally
        # have no volume and lose their writable layer on removal.
        if spec.volume:
            host_config["Binds"] = [f"{spec.volume}:/workspace"]
        created = await self._post("/containers/create", json=body)
        cid = created["Id"]
        await self._post(f"/containers/{cid}/start")
        logger.info("sandbox_container_started", sandbox_id=cid, kind=spec.kind.value)
        return SandboxHandle(id=cid, kind=spec.kind, spec=spec)

    async def stop(self, handle: SandboxHandle) -> None:
        resp = await self._request("POST", f"/containers/{handle.id}/stop")
        if resp.status_code not in (204, 304, 404):
            raise SandboxError(f"stop failed: {resp.status_code} {resp.text}")

    async def remove(self, handle: SandboxHandle) -> None:
        resp = await self._request(
            "DELETE", f"/containers/{handle.id}", params={"force": "true", "v": "true"}
        )
        if resp.status_code not in (200, 204, 404):
            raise SandboxError(f"remove failed: {resp.status_code} {resp.text}")

    # --- file transfer ---------------------------------------------------
    # The Docker REST API exposes /containers/{id}/archive (GET extracts a path
    # as a tar, PUT extracts a tar into the container). One file per call is
    # tar-wrapped here so callers can stay in plain bytes-land.

    async def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        if not path.startswith("/"):
            raise SandboxError(f"read_file path must be absolute: {path}")
        # Stream the archive so we can bail before a hostile / runaway file
        # buffers gigabytes into the agent process. `_READ_OVERHEAD_CAP` covers
        # the tar header (512B) + padding for a single member; anything past
        # that means the payload itself exceeds the cap.
        cap_with_overhead = _FILE_SIZE_CAP + _READ_OVERHEAD_CAP
        buf = bytearray()
        try:
            async with self._client.stream(
                "GET",
                f"/containers/{handle.id}/archive",
                params={"path": path},
                timeout=httpx.Timeout(30.0, read=None),
            ) as resp:
                if resp.status_code == 404:
                    raise SandboxFileNotFound(
                        f"{path} not found in sandbox {handle.id}"
                    )
                if resp.status_code == 409:
                    raise SandboxNotRunning(f"sandbox {handle.id} is not running")
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    raise SandboxError(
                        f"read_file {handle.id}:{path}: {resp.status_code} {detail}"
                    )
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > cap_with_overhead:
                        raise SandboxFileTooLarge(
                            f"{path} archive exceeds cap {_FILE_SIZE_CAP}"
                        )
        except httpx.HTTPError as exc:
            raise SandboxError(f"read_file {path}: {exc}") from exc

        try:
            with tarfile.open(fileobj=io.BytesIO(bytes(buf)), mode="r|") as tar:
                for member in tar:
                    # First non-empty member is the entry at `path`. A
                    # directory there means the caller pointed at a folder —
                    # silently returning some inner file would hide the bug.
                    if member.isdir():
                        raise SandboxError(
                            f"{path} is a directory, not a file"
                        )
                    if not member.isfile():
                        continue
                    if member.size > _FILE_SIZE_CAP:
                        raise SandboxFileTooLarge(
                            f"{path} is {member.size} bytes, cap is {_FILE_SIZE_CAP}"
                        )
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    return extracted.read()
        except tarfile.TarError as exc:
            raise SandboxError(f"read_file {path}: bad tar stream: {exc}") from exc
        raise SandboxFileNotFound(f"{path} archive was empty")

    async def write_file(
        self, handle: SandboxHandle, path: str, data: bytes
    ) -> None:
        if not path.startswith("/"):
            raise SandboxError(f"write_file path must be absolute: {path}")
        if len(data) > _FILE_SIZE_CAP:
            raise SandboxFileTooLarge(
                f"write to {path} is {len(data)} bytes, cap is {_FILE_SIZE_CAP}"
            )
        # PUT /archive extracts a tar at `path` (which must be a directory).
        # We split into dir + basename and tar-wrap a single regular file.
        parent, _, name = path.rpartition("/")
        if not name:
            raise SandboxError(f"write_file path must not end with /: {path}")
        parent = parent or "/"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        resp = await self._request(
            "PUT",
            f"/containers/{handle.id}/archive",
            params={"path": parent},
            content=buf.getvalue(),
            headers={"Content-Type": "application/x-tar"},
        )
        if resp.status_code == 404:
            # Parent directory missing — surface as not-found so the caller can
            # mkdir-p via exec or write a different path.
            raise SandboxFileNotFound(f"parent {parent} missing in sandbox {handle.id}")
        if resp.status_code == 409:
            raise SandboxNotRunning(f"sandbox {handle.id} is not running")
        self._check(resp, op=f"write_file {handle.id}:{path}")

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        resp = await self._request("GET", f"/containers/{handle.id}/json")
        if resp.status_code == 404:
            return SandboxStatus(state=SandboxState.removed)
        self._check(resp, op=f"status {handle.id}")
        state = resp.json()["State"]
        return SandboxStatus(state=_map_state(state), exit_code=state.get("ExitCode"))

    # --- exec --------------------------------------------------------------

    async def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        create_body: dict[str, object] = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Cmd": list(argv),
        }
        if cwd is not None:
            create_body["WorkingDir"] = cwd
        if env:
            create_body["Env"] = [f"{k}={v}" for k, v in env.items()]

        resp = await self._request(
            "POST", f"/containers/{handle.id}/exec", json=create_body
        )
        if resp.status_code == 404:
            raise SandboxNotRunning(f"sandbox {handle.id} is not running")
        self._check(resp, op=f"exec create {handle.id}")
        exec_id = resp.json()["Id"]

        buffer = bytearray()
        try:
            async with self._client.stream(
                "POST",
                f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
                # Output may take arbitrarily long; only the stream gets no read cap.
                timeout=httpx.Timeout(30.0, read=None),
            ) as stream:
                if stream.status_code == 409:
                    raise SandboxNotRunning(f"sandbox {handle.id} is not running")
                self._check(stream, op=f"exec start {exec_id}")
                async for chunk in stream.aiter_bytes():
                    buffer.extend(chunk)
                    for event in _drain_frames(buffer):
                        yield event
        except httpx.HTTPError as exc:
            raise SandboxError(f"exec stream {exec_id} failed: {exc}") from exc

        inspect = await self._post_get(f"/exec/{exec_id}/json")
        # Distinguish a genuine 0 from "not yet reaped / unknown" — `None or 0`
        # would silently report a failed command as success.
        exit_code = inspect.get("ExitCode")
        yield ExecExit(exit_code=exit_code if exit_code is not None else -1)

    # --- http helpers ------------------------------------------------------

    async def _post(self, path: str, *, json: dict | None = None) -> dict:
        resp = await self._request("POST", path, json=json)
        self._check(resp, op=f"POST {path}")
        return resp.json() if resp.content else {}

    async def _post_get(self, path: str) -> dict:
        resp = await self._request("GET", path)
        self._check(resp, op=f"GET {path}")
        return resp.json()


def _drain_frames(buffer: bytearray) -> list[ExecEvent]:
    """Pull complete multiplexed frames out of `buffer`, leaving any partial
    frame behind for the next chunk."""
    events: list[ExecEvent] = []
    while len(buffer) >= _FRAME_HEADER.size:
        stream_type, size = _FRAME_HEADER.unpack(buffer[: _FRAME_HEADER.size])
        if len(buffer) < _FRAME_HEADER.size + size:
            break
        payload = bytes(buffer[_FRAME_HEADER.size : _FRAME_HEADER.size + size])
        del buffer[: _FRAME_HEADER.size + size]
        name: Literal["stdout", "stderr"] = (
            "stderr" if stream_type == _STREAM_STDERR else "stdout"
        )
        events.append(ExecOutput(stream=name, data=payload))
    return events


def _map_state(state: dict) -> SandboxState:
    if state.get("OOMKilled"):
        return SandboxState.oom
    if state.get("Running"):
        return SandboxState.running
    if state.get("Dead") or (state.get("ExitCode") or 0) != 0:
        return SandboxState.crashed
    return SandboxState.exited
