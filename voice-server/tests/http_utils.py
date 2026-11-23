"""Real aiohttp HTTP parsing/streaming over local IPC, without listening sockets.

The managed test sandbox forbids TCP binds. Socket pairs keep these integration
checks entirely local while using aiohttp's TestClient, TestServer and protocols.
"""

import asyncio
import itertools
import socket

from aiohttp import ClientConnectorError, TCPConnector
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer as AiohttpTestServer
from yarl import URL

_PORTS = itertools.count(20000)
_SERVERS = {}


def unused_port():
    return next(_PORTS)


class LocalTestServer(AiohttpTestServer):
    async def start_server(self, loop=None, **kwargs):
        if self.runner:
            return
        self._loop = loop or asyncio.get_running_loop()
        # Match web.run_app's default: disconnected handlers finish/drain work.
        self.runner = await self._make_runner(handler_cancellation=False)
        await self.runner.setup()
        self.port = self.port or unused_port()
        self._root = URL(f"http://127.0.0.1:{self.port}")
        _SERVERS[self.port] = self

    async def close(self):
        _SERVERS.pop(self.port, None)
        await super().close()


class LocalConnector(TCPConnector):
    async def _create_connection(self, req, traces, timeout):
        server = _SERVERS.get(req.url.port)
        if server is None:
            raise ClientConnectorError(
                req.connection_key, ConnectionRefusedError("no local test server")
            )
        client_sock, server_sock = socket.socketpair()
        client_sock.setblocking(False)
        server_sock.setblocking(False)
        try:
            await self._loop.connect_accepted_socket(server.runner.server, server_sock)
            _, protocol = await self._loop.create_connection(self._factory, sock=client_sock)
            return protocol
        except BaseException:
            client_sock.close()
            server_sock.close()
            raise


class LocalTestClient(AiohttpTestClient):
    def __init__(self, server, **kwargs):
        super().__init__(server, connector=LocalConnector(), **kwargs)
