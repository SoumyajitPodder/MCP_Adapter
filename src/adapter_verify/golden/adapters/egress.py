"""In-process egress tripwire for golden runs (brief §6.2, M5-Q3).

Patches name resolution and socket connects for the duration of a run, allowing only the
configured hosts. It catches mistakes, not a determined bypass: native code and child processes
aren't covered. Isolation proper is a network-less container (required before a real agent or
model key runs in CI; DESIGN.md D-074).
"""

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_LOOPBACK_SELF_PIPE = frozenset({"127.0.0.1", "::1"})


class SocketEgressGuard:
    def __init__(self, allowed_hosts: frozenset[str] = frozenset()) -> None:
        self._allowed = allowed_hosts

    @contextmanager
    def guard(self) -> Iterator[list[str]]:
        blocked: list[str] = []
        resolved: set[str] = set()  # addresses of allowed hosts, which connects then use
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        allowed = self._allowed

        def name_of(host: Any) -> str:  # noqa: ANN401
            # anyio resolves IDNA-encoded bytes, e.g. b"generativelanguage.googleapis.com".
            return host.decode("idna") if isinstance(host, bytes) else str(host)

        def host_of(address: Any) -> str:  # noqa: ANN401 - socket addresses are untyped tuples
            return name_of(address[0] if isinstance(address, tuple) and address else address)

        def check(host: str) -> None:
            if host not in allowed and host not in resolved:
                blocked.append(host)
                msg = f"egress blocked: {host}"
                raise OSError(msg)

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            check(name_of(host))
            infos = real_getaddrinfo(host, *args, **kwargs)
            resolved.update(str(info[4][0]) for info in infos)
            return infos

        def connect(self: socket.socket, address: Any) -> None:  # noqa: ANN401
            if self.family in {socket.AF_INET, socket.AF_INET6}:
                host = host_of(address)
                # asyncio's own wake-up socket pair on Windows connects to loopback.
                if host not in _LOOPBACK_SELF_PIPE:
                    check(host)
            real_connect(self, address)

        def connect_ex(self: socket.socket, address: Any) -> int:  # noqa: ANN401
            if self.family in {socket.AF_INET, socket.AF_INET6}:
                host = host_of(address)
                if host not in _LOOPBACK_SELF_PIPE:
                    check(host)
            return real_connect_ex(self, address)

        socket.getaddrinfo = getaddrinfo
        socket.socket.connect = connect  # type: ignore[method-assign, assignment]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign, assignment]
        try:
            yield blocked
        finally:
            socket.getaddrinfo = real_getaddrinfo
            socket.socket.connect = real_connect  # type: ignore[method-assign]
            socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
