"""Applications served for real, on ports of localhost, inside the test process.

For the tests that need the platform as a client sees it -- over HTTP, the
services talking to each other as in a cluster -- without building images.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

__all__ = ["Running", "free_port", "private_key_pem", "serve"]


def free_port() -> int:
    """A port of localhost nobody listens on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def private_key_pem() -> str:
    """A fresh RSA key for auth to sign tokens with."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@dataclass
class Running:
    """Servers started by :func:`serve`."""

    servers: list[uvicorn.Server]
    tasks: list[asyncio.Task[None]]

    def __add__(self, other: Running) -> Running:
        return Running(self.servers + other.servers, self.tasks + other.tasks)

    async def stop(self) -> None:
        for server in self.servers:
            server.should_exit = True
        await asyncio.gather(*self.tasks)


async def serve(apps: dict[int, FastAPI]) -> Running:
    """Start each application on its port and wait until all of them listen."""
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        for port, app in apps.items()
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    async with asyncio.timeout(60):
        while not all(server.started for server in servers):
            if any(task.done() for task in tasks):
                [task.result() for task in tasks if task.done()]
            await asyncio.sleep(0.1)
    return Running(servers, tasks)
