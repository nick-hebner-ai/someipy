"""Real loopback sockets: a raw payload leaves the daemon's own server socket."""

import asyncio
import base64
import ipaddress
import logging
import select
import socket
from asyncio import DatagramTransport
from unittest.mock import Mock

import pytest
import pytest_asyncio

from someipy._internal._common.endpoint import Endpoint
from someipy._internal._daemon.offer_service_storage import ServiceToOffer
from someipy._internal._daemon.uds_messages import (
    SendRawRequest,
    StopOfferServiceRequest,
    create_uds_message,
)
from someipy._internal.someip_endpoint_factory import SomeipEndpointFactory
from someipy._internal.subscribers import EventGroupSubscriber, Subscribers
from someipy._internal.transport_layer_protocol import TransportLayerProtocol
from someipy.service import Event, EventGroup
from someipy.someipyd import DaemonServer, SomeipDaemon

IP = "127.0.0.1"
PAYLOAD = b"\xde\xad\xbe\xef" + bytes(range(16))


@pytest.fixture
def daemon():
    logger = Mock(spec=logging.Logger)
    daemon = SomeipDaemon(
        DaemonServer(logger), Mock(spec=SomeipEndpointFactory), {}, logger
    )
    daemon._ucast_transport = Mock(spec=DatagramTransport)
    return daemon


@pytest_asyncio.fixture
async def server_endpoint():
    endpoint = await SomeipEndpointFactory.create_server_endpoint(
        Endpoint(ipaddress.IPv4Address(IP), 0),
        TransportLayerProtocol.UDP,
        Mock(),
    )
    yield endpoint
    endpoint.shutdown()


@pytest.fixture
def subscriber_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP, 0))
    sock.setblocking(False)
    yield sock
    sock.close()


@pytest.fixture
def offered(daemon, server_endpoint, subscriber_socket):
    eventgroup = EventGroup(
        id=0x0321, events=[Event(id=0x0123, protocol=TransportLayerProtocol.UDP)]
    )
    service = ServiceToOffer(
        client_writer_id=1,
        instance_id=0x5678,
        service_id=0x1234,
        major_version=1,
        minor_version=0,
        offer_ttl_seconds=5,
        cyclic_offer_delay_ms=2000,
        endpoint=Endpoint(ipaddress.IPv4Address(IP), server_endpoint.src_port()),
        methods=[],
        eventgroups=[eventgroup],
    )
    daemon._services_to_offer.add_service(service)
    subscribers = Subscribers()
    subscribers.add_subscriber(
        EventGroupSubscriber(
            eventgroup_id=0x0321,
            endpoint=subscriber_socket.getsockname(),
            ttl=0xFFFFFF,
        )
    )
    daemon._service_subscribers[service] = subscribers
    daemon._someip_server_endpoints.add_endpoint(1, server_endpoint)
    return service


def _raw_request(port):
    return create_uds_message(
        SendRawRequest,
        service_id=0x1234,
        instance_id=0x5678,
        major_version=1,
        eventgroup_id=0x0321,
        src_endpoint_ip=IP,
        src_endpoint_port=port,
        payload=base64.b64encode(PAYLOAD).decode("utf-8"),
    )


async def _wait_readable(sock, timeout):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if select.select([sock], [], [], 0)[0]:
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_datagram_arrives_verbatim_from_the_offered_endpoint(
    daemon, offered, server_endpoint, subscriber_socket
):
    port = server_endpoint.src_port()

    daemon._handle_send_raw_request(_raw_request(port), 1)

    assert await _wait_readable(subscriber_socket, 2)
    data, source = subscriber_socket.recvfrom(65535)
    assert data == PAYLOAD
    assert source == (IP, port)


@pytest.mark.asyncio
async def test_nothing_arrives_after_stop_offer(
    daemon, offered, server_endpoint, subscriber_socket
):
    port = server_endpoint.src_port()
    stop_offer = create_uds_message(
        StopOfferServiceRequest,
        service_id=0x1234,
        instance_id=0x5678,
        major_version=1,
        minor_version=0,
        endpoint_ip=IP,
        endpoint_port=port,
        ttl=5,
        eventgroup_list=[eg.to_json() for eg in offered.eventgroups],
        method_list=[],
        cyclic_offer_delay_ms=2000,
    )

    daemon._handle_stop_offer_service_request(stop_offer, 1)
    daemon._handle_send_raw_request(_raw_request(port), 1)

    assert not await _wait_readable(subscriber_socket, 0.5)
