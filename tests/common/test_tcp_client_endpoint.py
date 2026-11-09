import asyncio

import pytest
import pytest_asyncio

from someipy._internal.someip_endpoint import TCPClientSomeipEndpoint
from someipy._internal.utils import create_tcp_client_socket


class _Server:
    def __init__(self):
        self.peers = []
        self.writers = []
        self.accepted = asyncio.Event()

    async def handle(self, reader, writer):
        self.peers.append(writer.get_extra_info("peername"))
        self.writers.append(writer)
        self.accepted.set()
        await reader.read()

    async def wait_accepted(self):
        await asyncio.wait_for(self.accepted.wait(), 5)
        self.accepted.clear()


@pytest_asyncio.fixture
async def server():
    peer = _Server()
    srv = await asyncio.start_server(peer.handle, "127.0.0.1", 0)
    peer.port = srv.sockets[0].getsockname()[1]
    yield peer
    srv.close()


@pytest.mark.asyncio
async def test_port_zero_connects_from_the_port_it_reports(server):
    endpoint = TCPClientSomeipEndpoint("127.0.0.1", server.port, "127.0.0.1", 0)

    await asyncio.wait_for(endpoint.wait_connected(), 5)
    await server.wait_accepted()

    assert endpoint.src_port() != 0
    assert server.peers == [("127.0.0.1", endpoint.src_port())]
    endpoint.shutdown()


@pytest.mark.asyncio
async def test_connects_from_a_given_pre_bound_socket(server):
    sock = create_tcp_client_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    endpoint = TCPClientSomeipEndpoint(
        "127.0.0.1", server.port, "127.0.0.1", port, sock=sock
    )

    await asyncio.wait_for(endpoint.wait_connected(), 5)
    await server.wait_accepted()

    assert server.peers == [("127.0.0.1", port)]
    endpoint.shutdown()


@pytest.mark.asyncio
async def test_reconnects_from_the_same_port(server):
    endpoint = TCPClientSomeipEndpoint("127.0.0.1", server.port, "127.0.0.1", 0)
    await asyncio.wait_for(endpoint.wait_connected(), 5)
    await server.wait_accepted()

    server.writers[0].close()
    await server.wait_accepted()

    assert len(server.peers) == 2
    assert server.peers[1] == server.peers[0]
    endpoint.shutdown()


@pytest.mark.asyncio
async def test_is_not_connected_while_the_peer_refuses():
    sock = create_tcp_client_socket("127.0.0.1", 0)
    closed_port = sock.getsockname()[1]
    sock.close()
    endpoint = TCPClientSomeipEndpoint("127.0.0.1", closed_port, "127.0.0.1", 0)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(endpoint.wait_connected(), 0.3)
    assert not endpoint.is_connected()
    endpoint.shutdown()
