from asyncio import DatagramTransport
import asyncio
import base64
import ipaddress
import json
import logging
import struct
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import someipy
from someipy._internal._common.endpoint import Endpoint
from someipy._internal._daemon.daemon_server import ClientConnectedEventArgs
from someipy._internal._daemon.daemon_server_client import DaemonServerClient
from someipy._internal._daemon.subscription import Subscription
from someipy._internal._daemon.uds_messages import (
    FindServiceRequest,
    InboundCallMethodResponse,
    OfferServiceRequest,
    SendEventRequest,
    StopOfferServiceRequest,
    StopSubscribeEventGroupRequest,
    SubscribeEventGroupRequest,
    create_uds_message,
)
from someipy._internal._daemon.offer_service_storage import ServiceToOffer
from someipy._internal.subscribers import EventGroupSubscriber, Subscribers
from someipy._internal._sd.deserialization.sd_serialization import serialize_sd_message
from someipy._internal._sd.entries.offer_service_entry import OfferServiceEntry
from someipy._internal._sd.options.endpoint import IpV4EndpointOption
from someipy._internal._sd.service_instance import ServiceInstance
from someipy._internal.someip_endpoint_factory import SomeipEndpointFactory
from someipy._internal.message_types import MessageType
from someipy._internal.someip_header import SomeIpHeader
from someipy._internal.someip_message import SomeIpMessage
from someipy._internal.someip_sd_header import SdSubscription
from someipy._internal.transport_layer_protocol import TransportLayerProtocol
from someipy.service import Event, EventGroup, Method
from someipy.someipyd import DaemonServer, SomeipDaemon


@pytest.fixture
def mock_logger() -> logging.Logger:
    mock_logger = Mock(spec=logging.Logger)
    return mock_logger


@pytest.fixture
def mock_daemon_server(mock_logger) -> DaemonServer:
    return DaemonServer(mock_logger)


@pytest.fixture
def mock_endpoint_factory() -> Mock:
    return Mock(spec=SomeipEndpointFactory)


@pytest.fixture
def daemon(mock_daemon_server, mock_endpoint_factory, mock_logger) -> SomeipDaemon:
    config = {}
    daemon = SomeipDaemon(
        mock_daemon_server, mock_endpoint_factory, config, mock_logger
    )
    daemon._ucast_transport = Mock(spec=DatagramTransport)
    return daemon


@pytest.fixture
def service_instance() -> ServiceInstance:
    service_instance = ServiceInstance(
        service_id=1,
        instance_id=2,
        major_version=3,
        minor_version=0,
        ttl=10,
        endpoint=Endpoint(ipaddress.IPv4Address("192.168.1.1"), 1),
        protocols=frozenset([TransportLayerProtocol.UDP]),
        timestamp=1000,
    )
    return service_instance


@pytest.fixture
def eventgroup() -> EventGroup:
    return EventGroup(
        id=1,
        events=[
            Event(id=1, protocol=TransportLayerProtocol.UDP),
            Event(id=2, protocol=TransportLayerProtocol.TCP),
        ],
    )


@pytest.fixture
def method() -> Method:
    return Method(
        id=1,
        protocol=TransportLayerProtocol.UDP,
    )


@pytest.fixture
def subscribe_event_group_request_udp(eventgroup) -> SubscribeEventGroupRequest:
    return SubscribeEventGroupRequest(
        service_id=1,
        instance_id=2,
        major_version=3,
        ttl_subscription=10,
        eventgroup=eventgroup.to_json(),
        client_endpoint_ip="192.168.1.1",
        client_endpoint_port=1,
        udp=True,
        tcp=False,
    )


@pytest.fixture
def subscribe_event_group_request_tcp(eventgroup) -> SubscribeEventGroupRequest:
    return SubscribeEventGroupRequest(
        service_id=1,
        instance_id=2,
        major_version=3,
        ttl_subscription=10,
        eventgroup=eventgroup.to_json(),
        client_endpoint_ip="192.168.1.1",
        client_endpoint_port=1,
        udp=False,
        tcp=True,
    )


@pytest.fixture
def subscribe_event_group_request_udp_and_tcp(eventgroup) -> SubscribeEventGroupRequest:
    return create_uds_message(
        SubscribeEventGroupRequest,
        service_id=1,
        instance_id=2,
        major_version=3,
        ttl_subscription=10,
        eventgroup=eventgroup.to_json(),
        client_endpoint_ip="123",
        client_endpoint_port=1,
        udp=True,
        tcp=True,
    )


@pytest.fixture
def stop_subscribe_eventgroup_request(
    eventgroup: EventGroup,
) -> StopSubscribeEventGroupRequest:
    return create_uds_message(
        StopSubscribeEventGroupRequest,
        service_id=1,
        instance_id=2,
        major_version=3,
        eventgroup=eventgroup.to_json(),
        client_endpoint_ip="192.168.1.1",
        client_endpoint_port=1,
        udp=eventgroup.has_udp,
        tcp=eventgroup.has_tcp,
    )


@pytest.fixture
def offer_service_request(eventgroup, method) -> OfferServiceRequest:
    return create_uds_message(
        OfferServiceRequest,
        service_id=1,
        instance_id=2,
        major_version=3,
        minor_version=0,
        endpoint_ip="127.0.0.1",
        endpoint_port=1,
        ttl=5,
        eventgroup_list=[eventgroup.to_json()],
        method_list=[method.to_json()],
        cyclic_offer_delay_ms=1000,
    )


@pytest.fixture
def stop_offer_service_request(eventgroup, method) -> StopOfferServiceRequest:
    return create_uds_message(
        StopOfferServiceRequest,
        service_id=1,
        instance_id=2,
        major_version=3,
        minor_version=0,
        endpoint_ip="127.0.0.1",
        endpoint_port=1,
        ttl=5,
        eventgroup_list=[eventgroup.to_json()],
        method_list=[method.to_json()],
        cyclic_offer_delay_ms=1000,
    )


def test_handle_offered_service_adds_service(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):
    # Initially, the found services list should be empty
    assert len(daemon._found_services) == 0

    # Handle the offered service
    daemon._handle_offered_service(service_instance)

    # Now, the found services list should contain the new service
    assert len(daemon._found_services) == 1
    assert daemon._found_services[0] == service_instance


def test_handle_offered_service_updates_timestamp(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):

    # Initially, the found services list should be empty
    assert len(daemon._found_services) == 0

    # Handle the offered service
    daemon._handle_offered_service(service_instance)

    service_instance_2 = ServiceInstance(
        service_id=service_instance.service_id,
        instance_id=service_instance.instance_id,
        major_version=service_instance.major_version,
        minor_version=service_instance.minor_version,
        ttl=service_instance.ttl,
        endpoint=service_instance.endpoint,
        protocols=service_instance.protocols,
        timestamp=2000,
    )

    # Handle the offered service again with updated timestamp
    daemon._handle_offered_service(service_instance_2)

    # Now, the found services list should contain the updated timestamp
    assert len(daemon._found_services) == 1
    assert daemon._found_services[0] == service_instance
    assert daemon._found_services[0].timestamp == 2000


@pytest.mark.asyncio
async def test_handle_offered_service_opens_tcp_client_endpoint(
    daemon: SomeipDaemon,
    mock_endpoint_factory: Mock,
    service_instance: ServiceInstance,
    subscribe_event_group_request_tcp: SubscribeEventGroupRequest,
):
    service_instance.protocols = frozenset([TransportLayerProtocol.TCP])

    # Add a subscription first
    await daemon._handle_subscribe_eventgroup_request(
        subscribe_event_group_request_tcp, 1
    )

    # Handle the offered service
    daemon._handle_offered_service(service_instance)

    # Verify that create_client_endpoint was called for TCP
    mock_endpoint_factory.create_tcp_client_endpoint.assert_called_once_with(
        service_instance.endpoint,
        service_instance.endpoint,
        daemon._someip_message_callback,
        daemon.logger,
    )

    assert len(daemon._pending_subscriptions) == 1
    assert len(daemon._someip_client_endpoints) == 1
    daemon._ucast_transport.sendto.assert_called_once()


@pytest.mark.asyncio
async def test_handle_subscribe_eventgroup_request_adds_requested_subscription(
    daemon: SomeipDaemon,
    subscribe_event_group_request_udp: SubscribeEventGroupRequest,
):
    # Initially, the requested subscriptions list should be empty
    assert len(daemon._requested_subscriptions) == 0

    # Handle the subscribe event group request
    await daemon._handle_subscribe_eventgroup_request(
        subscribe_event_group_request_udp, 1
    )

    # Now, the requested subscriptions list should contain the new subscription
    assert len(daemon._requested_subscriptions) == 1


@pytest.mark.asyncio
async def test_handle_offer_service_request_opens_server_endpoint(
    daemon: SomeipDaemon,
    offer_service_request: OfferServiceRequest,
):
    assert len(daemon._someip_server_endpoints) == 0

    await daemon._handle_offer_service_request(offer_service_request, 1)

    assert len(daemon._someip_server_endpoints) == 1


@pytest.mark.asyncio
async def test_handle_offer_service_does_not_add_service_twice(
    daemon: SomeipDaemon,
    offer_service_request: OfferServiceRequest,
):
    assert len(daemon._services_to_offer) == 0

    await daemon._handle_offer_service_request(offer_service_request, 1)
    await daemon._handle_offer_service_request(offer_service_request, 1)

    assert len(daemon._services_to_offer) == 1


@pytest.mark.asyncio
async def test_handle_stop_offer_service_removes_service_to_offer(
    daemon: SomeipDaemon,
    offer_service_request: OfferServiceRequest,
    stop_offer_service_request: StopOfferServiceRequest,
):
    assert len(daemon._services_to_offer) == 0

    await daemon._handle_offer_service_request(offer_service_request, 1)
    assert len(daemon._services_to_offer) == 1

    daemon._handle_stop_offer_service_request(stop_offer_service_request, 1)

    assert len(daemon._services_to_offer) == 0


@pytest.mark.asyncio
async def test_client_connected_disconnected(
    daemon: SomeipDaemon,
):
    initial_queue_count = len(daemon._tx_queues)

    # Simulate a new client connection
    client_id = 42

    new_client = Mock(spec=DaemonServerClient)
    new_client.id = client_id
    new_client.message_received = someipy._internal._common.event.Event()
    new_client.message_received.add_handler = Mock()
    new_client.message_received.remove_handler = Mock()

    new_client_args = ClientConnectedEventArgs(new_client)

    await daemon.new_client_connected(new_client, new_client_args)

    # Verify that a new queue has been created for the client
    assert client_id in daemon._tx_queues.keys()
    assert daemon._tx_queues[client_id] is not None

    new_client.message_received.add_handler.assert_called_once()

    await asyncio.sleep(0.0)  # Allow tx task to be scheduled

    await daemon.client_disconnected(daemon, new_client_args)

    # Verify that the client's queue and tx task was removed
    assert client_id not in daemon._tx_queues.keys()
    assert client_id not in daemon._tx_tasks.keys()

    # Message received event handler was removed
    new_client.message_received.remove_handler.assert_called_once()

    """ Cleanup of:
        - Offer timers
        - Services to be offered
        - Subscriptions (pending)
        - Subscriptions (active)
        - Method calls pending
        - Pending find calls
        - Close server or client endpoints that are only used by the disconnected client
    """


def test_datagram_received_mcast_calls_handle_offered_service(
    daemon: SomeipDaemon,
):

    # Create an SdMessage with an OfferService entry
    sd_message = someipy._internal._sd.sd_message.SdMessage()
    sd_message.session_id = 1

    ip_endpoint_option_1 = IpV4EndpointOption(
        address=ipaddress.IPv4Address("192.168.1.1"),
        protocol=TransportLayerProtocol.TCP,
        port=8080,
    )
    ip_endpoint_option_2 = IpV4EndpointOption(
        address=ipaddress.IPv4Address("192.168.1.2"),
        protocol=TransportLayerProtocol.UDP,
        port=8080,
    )

    offer_service_entry = OfferServiceEntry(
        service_id=1,
        instance_id=1,
        major_version=1,
        minor_version=0,
        ttl=120,
        ip_v4_endpoints=[ip_endpoint_option_1],
        ip_v6_endpoints=[],
    )

    sd_message.entries.append(offer_service_entry)
    data = serialize_sd_message(sd_message)

    # Patch the _handle_offered_service method to monitor its calls
    with patch.object(
        daemon, "_handle_offered_service", wraps=daemon._handle_offered_service
    ) as mock_handle_offered_service:
        # Simulate receiving a multicast datagram
        daemon.datagram_received_mcast(data, ("127.0.0.1", 5000))

        mock_handle_offered_service.assert_called_once()


def test_check_services_ttl_task_removes_expired_offered_services(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):
    service_instance.timestamp = time.time()
    service_instance.ttl = 10  # seconds

    # Add a service instance with a short TTL
    daemon._found_services.append(service_instance)

    # Run the TTL check task once
    daemon._check_service_ttl_task_impl()

    # Service is still valid
    assert len(daemon._found_services) == 1

    service_instance.timestamp -= service_instance.ttl + 1  # Simulate time passage
    daemon._check_service_ttl_task_impl()

    # Service should be removed due to TTL expiry
    assert len(daemon._found_services) == 0


def _decode_find_response(framed: bytes) -> dict:
    # prepare_message frames a message as a 4-byte little-endian payload length,
    # padding out to a 256-byte header, then the JSON payload.
    length = struct.unpack("<I", framed[:4])[0]
    return json.loads(framed[256 : 256 + length].decode("utf-8"))


def _find_request_for(service_instance) -> FindServiceRequest:
    return create_uds_message(
        FindServiceRequest,
        service_id=service_instance.service_id,
        instance_id=service_instance.instance_id,
        major_version=service_instance.major_version,
        minor_version=service_instance.minor_version,
    )


@pytest.mark.asyncio
async def test_find_service_request_sends_positive_response(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):
    # Cache hit: respond immediately, no active probe.
    daemon._found_services.append(service_instance)
    daemon._tx_queues[1] = asyncio.Queue()

    await daemon._handle_find_service_request(_find_request_for(service_instance), 1)

    assert daemon._tx_queues[1].qsize() == 1
    resp = _decode_find_response(daemon._tx_queues[1].get_nowait())
    assert resp["type"] == "FindServiceResponse"
    assert resp["success"] is True
    assert resp["service_id"] == service_instance.service_id
    assert resp["endpoint_ip"] == str(service_instance.endpoint.ip)
    assert len(daemon._background_tasks) == 0
    daemon._ucast_transport.sendto.assert_not_called()


@pytest.mark.asyncio
async def test_find_service_request_dispatches_probe_when_not_cached(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):
    # Cache miss: the handler must NOT block or answer synchronously. It
    # dispatches a background probe and returns immediately (so the probe's
    # sleeps never stall the event loop).
    daemon._tx_queues[1] = asyncio.Queue()

    await daemon._handle_find_service_request(_find_request_for(service_instance), 1)

    assert daemon._tx_queues[1].qsize() == 0  # no immediate response
    assert len(daemon._background_tasks) == 1

    for task in list(daemon._background_tasks):
        task.cancel()


@pytest.mark.asyncio
async def test_probe_find_service_sends_find_and_negative_when_unresolved(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):
    daemon._tx_queues[1] = asyncio.Queue()

    await daemon._probe_find_service(
        _find_request_for(service_instance), 1, attempts=2, interval_s=0
    )

    # An SD FindService is actively sent to the SD address, once per attempt.
    assert daemon._ucast_transport.sendto.call_count == 2
    sent_args = daemon._ucast_transport.sendto.call_args.args
    assert sent_args[1] == (daemon.sd_address, daemon.sd_port)

    # Unresolved after all attempts -> a negative response is delivered.
    assert daemon._tx_queues[1].qsize() == 1
    resp = _decode_find_response(daemon._tx_queues[1].get_nowait())
    assert resp["success"] is False


@pytest.mark.asyncio
async def test_probe_find_service_reports_positive_when_service_resolves(
    daemon: SomeipDaemon, service_instance: ServiceInstance
):
    # The service is resolvable from the cache (as if a remote answered the
    # FindService): the probe reports success.
    daemon._tx_queues[1] = asyncio.Queue()
    daemon._found_services.append(service_instance)

    await daemon._probe_find_service(
        _find_request_for(service_instance), 1, attempts=4, interval_s=0
    )

    assert daemon._ucast_transport.sendto.called  # a FindService was sent
    assert daemon._tx_queues[1].qsize() == 1
    resp = _decode_find_response(daemon._tx_queues[1].get_nowait())
    assert resp["success"] is True
    assert resp["service_id"] == service_instance.service_id


def test_stop_subscribe_eventgroup_request_removes_requested_subscriptions(
    daemon: SomeipDaemon,
    eventgroup: EventGroup,
    stop_subscribe_eventgroup_request: StopSubscribeEventGroupRequest,
):
    protocols = [
        protocol
        for flag, protocol in (
            (stop_subscribe_eventgroup_request["udp"], TransportLayerProtocol.UDP),
            (stop_subscribe_eventgroup_request["tcp"], TransportLayerProtocol.TCP),
        )
        if flag
    ]

    new_subscription = Subscription(
        service_id=stop_subscribe_eventgroup_request["service_id"],
        instance_id=stop_subscribe_eventgroup_request["instance_id"],
        major_version=stop_subscribe_eventgroup_request["major_version"],
        eventgroup=eventgroup,
        ttl_seconds=10,
        client_endpoint=Endpoint(
            ipaddress.IPv4Address(
                stop_subscribe_eventgroup_request["client_endpoint_ip"]
            ),
            stop_subscribe_eventgroup_request["client_endpoint_port"],
        ),
        server_endpoint=None,
        protocols=frozenset(protocols),
    )

    daemon._requested_subscriptions.add_subscription(
        1,
        new_subscription,
    )

    assert len(daemon._requested_subscriptions) == 1

    daemon._handle_stop_subscribe_eventgroup_request(
        stop_subscribe_eventgroup_request, 1
    )

    assert len(daemon._requested_subscriptions) == 0


def test_stop_subscribe_eventgroup_request_removes_pending_and_active_subscriptions(
    daemon: SomeipDaemon,
    eventgroup: EventGroup,
    stop_subscribe_eventgroup_request: StopSubscribeEventGroupRequest,
):
    protocols = [
        protocol
        for flag, protocol in (
            (stop_subscribe_eventgroup_request["udp"], TransportLayerProtocol.UDP),
            (stop_subscribe_eventgroup_request["tcp"], TransportLayerProtocol.TCP),
        )
        if flag
    ]

    endpoint = Endpoint(
        ipaddress.IPv4Address(stop_subscribe_eventgroup_request["client_endpoint_ip"]),
        stop_subscribe_eventgroup_request["client_endpoint_port"],
    )

    new_subscription = Subscription(
        service_id=stop_subscribe_eventgroup_request["service_id"],
        instance_id=stop_subscribe_eventgroup_request["instance_id"],
        major_version=stop_subscribe_eventgroup_request["major_version"],
        eventgroup=eventgroup,
        ttl_seconds=10,
        client_endpoint=endpoint,
        server_endpoint=endpoint,
        protocols=frozenset(protocols),
    )

    daemon._pending_subscriptions.add(new_subscription)
    daemon._active_subscriptions.add(new_subscription)

    assert len(daemon._pending_subscriptions) == 1
    assert len(daemon._active_subscriptions) == 1

    daemon._handle_stop_subscribe_eventgroup_request(
        stop_subscribe_eventgroup_request, 1
    )

    assert len(daemon._pending_subscriptions) == 0
    assert len(daemon._active_subscriptions) == 0


@pytest.mark.parametrize(
    "message_type",
    [MessageType.REQUEST, MessageType.REQUEST_NO_RETURN],
)
@pytest.mark.asyncio
async def test_someip_message_callback_dispatches_request_types_to_client(
    daemon: SomeipDaemon,
    offer_service_request: OfferServiceRequest,
    message_type: MessageType,
):
    # A method call arriving for an offered service must be forwarded to the
    # offering client. This must hold for both a normal REQUEST and a
    # fire-and-forget REQUEST_NO_RETURN (the latter regressed before the fix:
    # it fell through the dispatch and was silently dropped).
    await daemon._handle_offer_service_request(offer_service_request, 1)
    daemon._tx_queues[1] = asyncio.Queue()

    # The offered service listens on 127.0.0.1:1 for method id 1 (see the
    # offer_service_request fixture).
    header = SomeIpHeader(
        service_id=1,
        method_id=1,
        length=8,
        client_id=3,
        session_id=4,
        protocol_version=1,
        interface_version=2,
        message_type=message_type.value,
        return_code=0x00,
    )
    message = SomeIpMessage(header=header, payload=b"")

    daemon._someip_message_callback(
        message,
        src_addr=("192.168.1.50", 30509),
        dst_addr=("127.0.0.1", 1),
        protocol=TransportLayerProtocol.UDP,
    )

    assert daemon._tx_queues[1].qsize() == 1


@pytest.mark.asyncio
async def test_someip_message_callback_ignores_request_for_unknown_service(
    daemon: SomeipDaemon,
    offer_service_request: OfferServiceRequest,
):
    # A fire-and-forget request whose destination endpoint does not match any
    # offered service must not be dispatched.
    await daemon._handle_offer_service_request(offer_service_request, 1)
    daemon._tx_queues[1] = asyncio.Queue()

    header = SomeIpHeader(
        service_id=1,
        method_id=1,
        length=8,
        client_id=3,
        session_id=4,
        protocol_version=1,
        interface_version=2,
        message_type=MessageType.REQUEST_NO_RETURN.value,
        return_code=0x00,
    )
    message = SomeIpMessage(header=header, payload=b"")

    daemon._someip_message_callback(
        message,
        src_addr=("192.168.1.50", 30509),
        dst_addr=("127.0.0.1", 9999),  # wrong port -> no matching service
        protocol=TransportLayerProtocol.UDP,
    )

    assert daemon._tx_queues[1].qsize() == 0


def _subscribed_service_with_no_endpoint(daemon, protocol):
    """Set up an offered service with one subscriber but no matching server
    endpoint, and return the SendEventRequest that targets it. Sending the
    event must find the subscriber but no endpoint to send from."""
    event = Event(id=0x0001, protocol=protocol)
    eventgroup = EventGroup(id=0x0003, events=[event])
    service = ServiceToOffer(
        client_writer_id=1,
        instance_id=0x5678,
        service_id=0x1234,
        major_version=1,
        minor_version=0,
        offer_ttl_seconds=5,
        cyclic_offer_delay_ms=2000,
        endpoint=Endpoint(ipaddress.IPv4Address("127.0.0.2"), 3000),
        methods=[],
        eventgroups=[eventgroup],
    )
    subscribers = Subscribers()
    subscribers.add_subscriber(
        EventGroupSubscriber(
            eventgroup_id=0x0003, endpoint=("127.0.0.1", 40000), ttl=0xFFFFFF
        )
    )
    daemon._service_subscribers[service] = subscribers

    message = create_uds_message(
        SendEventRequest,
        service_id=0x1234,
        instance_id=0x5678,
        major_version=1,
        client_id=1,
        session_id=1,
        eventgroup_id=0x0003,
        event=event.to_json(),
        src_endpoint_ip="127.0.0.9",  # no server endpoint has this ip/port
        src_endpoint_port=9999,
        payload=base64.b64encode(b"\x00\x00\x00\x01").decode("utf-8"),
    )
    return message


@pytest.mark.parametrize(
    "protocol, expected",
    [
        (TransportLayerProtocol.UDP, "no UDP server endpoint"),
        (TransportLayerProtocol.TCP, "no TCP server endpoint"),
    ],
)
def test_send_event_logs_error_when_no_server_endpoint(daemon, protocol, expected):
    # A subscriber exists but there is no matching server endpoint, so the event
    # cannot be sent. This must be logged rather than silently dropped.
    message = _subscribed_service_with_no_endpoint(daemon, protocol)

    daemon._handle_send_event_request(message, 1)

    assert daemon.logger.error.called
    assert any(
        expected in str(call.args[0]) for call in daemon.logger.error.call_args_list
    )


def test_send_event_does_not_log_error_when_endpoint_present(daemon):
    # When a matching server endpoint exists, the event is sent and no
    # endpoint-not-found error is logged.
    event = Event(id=0x0001, protocol=TransportLayerProtocol.UDP)
    eventgroup = EventGroup(id=0x0003, events=[event])
    service = ServiceToOffer(
        client_writer_id=1,
        instance_id=0x5678,
        service_id=0x1234,
        major_version=1,
        minor_version=0,
        offer_ttl_seconds=5,
        cyclic_offer_delay_ms=2000,
        endpoint=Endpoint(ipaddress.IPv4Address("127.0.0.2"), 3000),
        methods=[],
        eventgroups=[eventgroup],
    )
    subscribers = Subscribers()
    subscribers.add_subscriber(
        EventGroupSubscriber(
            eventgroup_id=0x0003, endpoint=("127.0.0.1", 40000), ttl=0xFFFFFF
        )
    )
    daemon._service_subscribers[service] = subscribers

    endpoint = MagicMock()
    endpoint.src_ip.return_value = "127.0.0.9"
    endpoint.src_port.return_value = 9999
    endpoint.protocol.return_value = TransportLayerProtocol.UDP
    daemon._someip_server_endpoints.add_endpoint(1, endpoint)

    message = create_uds_message(
        SendEventRequest,
        service_id=0x1234,
        instance_id=0x5678,
        major_version=1,
        client_id=1,
        session_id=1,
        eventgroup_id=0x0003,
        event=event.to_json(),
        src_endpoint_ip="127.0.0.9",
        src_endpoint_port=9999,
        payload=base64.b64encode(b"\x00\x00\x00\x01").decode("utf-8"),
    )

    daemon._handle_send_event_request(message, 1)

    endpoint.sendto.assert_called_once()
    assert not any(
        "server endpoint" in str(call.args[0])
        for call in daemon.logger.error.call_args_list
    )


def _server_endpoint_mock(ip, port, protocol):
    endpoint = MagicMock()
    endpoint.src_ip.return_value = ip
    endpoint.src_port.return_value = port
    endpoint.protocol.return_value = protocol
    return endpoint


def test_method_response_uses_offered_service_endpoint_not_first(daemon: SomeipDaemon):
    # One client offers two services on different ports. A method response for
    # the second service must be sent from that service's own endpoint, not the
    # first endpoint the client happens to have (which the peer would reject).
    writer_id = 1

    service_a = ServiceToOffer(
        client_writer_id=writer_id,
        instance_id=0x01,
        service_id=0x1111,
        major_version=1,
        minor_version=0,
        offer_ttl_seconds=5,
        cyclic_offer_delay_ms=2000,
        endpoint=Endpoint(ipaddress.IPv4Address("127.0.0.1"), 3000),
        methods=[Method(id=1, protocol=TransportLayerProtocol.UDP)],
        eventgroups=[],
    )
    service_b = ServiceToOffer(
        client_writer_id=writer_id,
        instance_id=0x02,
        service_id=0x2222,
        major_version=1,
        minor_version=0,
        offer_ttl_seconds=5,
        cyclic_offer_delay_ms=2000,
        endpoint=Endpoint(ipaddress.IPv4Address("127.0.0.1"), 3001),
        methods=[Method(id=1, protocol=TransportLayerProtocol.UDP)],
        eventgroups=[],
    )
    daemon._services_to_offer.add_service(service_a)
    daemon._services_to_offer.add_service(service_b)

    # Added in offering order: endpoint_a (port 3000) is first for this client,
    # so the generic get_endpoint(writer_id, UDP) would return it.
    endpoint_a = _server_endpoint_mock("127.0.0.1", 3000, TransportLayerProtocol.UDP)
    endpoint_b = _server_endpoint_mock("127.0.0.1", 3001, TransportLayerProtocol.UDP)
    daemon._someip_server_endpoints.add_endpoint(writer_id, endpoint_a)
    daemon._someip_server_endpoints.add_endpoint(writer_id, endpoint_b)

    message = create_uds_message(
        InboundCallMethodResponse,
        service_id=0x2222,
        instance_id=0x02,
        method_id=1,
        client_id=3,
        session_id=4,
        protocol_version=1,
        interface_version=1,
        major_version=1,
        minor_version=0,
        message_type=0x80,  # RESPONSE
        src_endpoint_ip="127.0.0.50",
        src_endpoint_port=40000,
        protocol=TransportLayerProtocol.UDP.value,
        payload="",
        return_code=0x00,
    )

    daemon._handle_inbound_call_method_response(message, writer_id)

    # The response must go out service B's endpoint (port 3001), not port 3000.
    endpoint_b.sendto.assert_called_once()
    endpoint_a.sendto.assert_not_called()


def _decode_client_message(framed: bytes) -> dict:
    # prepare_message frames a message as: 4-byte little-endian payload length,
    # then padding out to a 256-byte header, then the JSON payload.
    length = struct.unpack("<I", framed[:4])[0]
    return json.loads(framed[256 : 256 + length].decode("utf-8"))


def _offer_service_for_subscription(daemon, writer_id):
    service = ServiceToOffer(
        client_writer_id=writer_id,
        instance_id=0x5678,
        service_id=0x1234,
        major_version=1,
        minor_version=0,
        offer_ttl_seconds=5,
        cyclic_offer_delay_ms=2000,
        endpoint=Endpoint(ipaddress.IPv4Address("127.0.0.1"), 3000),
        methods=[],
        eventgroups=[EventGroup(id=0x0003, events=[Event(id=1, protocol=TransportLayerProtocol.UDP)])],
    )
    daemon._services_to_offer.add_service(service)


def _subscription():
    return SdSubscription(
        service_id=0x1234,
        instance_id=0x5678,
        major_version=1,
        ttl=10,
        initial_data_requested_flag=0,
        counter=0,
        eventgroup_id=0x0003,
        ipv4_address=ipaddress.IPv4Address("192.168.1.50"),
        port=30509,
        protocol=TransportLayerProtocol.UDP,
    )


def test_subscription_notifies_offering_client(daemon: SomeipDaemon):
    # When a remote subscriber subscribes to an offered service, the offering
    # client is notified with an InboundSubscription message.
    writer_id = 1
    _offer_service_for_subscription(daemon, writer_id)
    daemon._tx_queues[writer_id] = asyncio.Queue()

    daemon._handle_subscription(_subscription())

    assert daemon._tx_queues[writer_id].qsize() == 1
    msg = _decode_client_message(daemon._tx_queues[writer_id].get_nowait())
    assert msg["type"] == "InboundSubscription"
    assert msg["service_id"] == 0x1234
    assert msg["instance_id"] == 0x5678
    assert msg["event_group_id"] == 0x0003
    assert msg["subscriber_ip"] == "192.168.1.50"
    assert msg["subscriber_port"] == 30509
    assert msg["ttl_seconds"] == 10
    assert msg["is_renewal"] is False


def test_subscription_renewal_is_flagged(daemon: SomeipDaemon):
    # The same subscriber subscribing again is reported as a renewal.
    writer_id = 1
    _offer_service_for_subscription(daemon, writer_id)
    daemon._tx_queues[writer_id] = asyncio.Queue()

    daemon._handle_subscription(_subscription())
    daemon._handle_subscription(_subscription())

    messages = [
        _decode_client_message(daemon._tx_queues[writer_id].get_nowait())
        for _ in range(daemon._tx_queues[writer_id].qsize())
    ]
    assert [m["is_renewal"] for m in messages] == [False, True]


def test_subscription_without_matching_offer_sends_no_notification(daemon: SomeipDaemon):
    # A subscription for a service this daemon does not offer notifies nobody.
    writer_id = 1
    daemon._tx_queues[writer_id] = asyncio.Queue()

    daemon._handle_subscription(_subscription())  # no service offered

    assert daemon._tx_queues[writer_id].qsize() == 0


@pytest.mark.parametrize(
    "sd_address, expect_multicast",
    [
        ("224.224.224.245", True),
        ("239.192.0.251", True),  # class-D multicast; regressed before the fix
        ("225.0.0.1", True),  # class-D multicast; regressed before the fix
        ("255.255.255.255", False),  # limited broadcast, not multicast
    ],
)
@pytest.mark.asyncio
async def test_start_sd_listening_selects_socket_by_multicast_range(
    daemon: SomeipDaemon, sd_address, expect_multicast
):
    # Any IPv4 multicast address (224.0.0.0/4) must use the multicast receive
    # socket (which joins the group). A "224" prefix check missed 225-239.
    daemon.sd_address = sd_address
    loop = asyncio.get_running_loop()
    with patch("someipy.someipyd.create_rcv_multicast_socket") as mcast, patch(
        "someipy.someipyd.create_rcv_broadcast_socket"
    ) as bcast, patch("someipy.someipyd.create_udp_socket"), patch.object(
        loop, "create_datagram_endpoint", new=AsyncMock(return_value=(Mock(), Mock()))
    ):
        await daemon.start_sd_listening()

    if expect_multicast:
        mcast.assert_called_once()
        bcast.assert_not_called()
    else:
        bcast.assert_called_once()
        mcast.assert_not_called()


# ---------------------------------------------------------------------------
# Subscription state notifications (SubscribeEventgroupAck / Nack)
# ---------------------------------------------------------------------------


def _eventgroup_entry(
    service_id: int, instance_id: int, eventgroup_id: int, major_version: int = 3
):
    """Minimal stand-in for a received SD subscribe-ack/nack eventgroup entry.

    major_version must match the requested subscription: the daemon keys the
    lookup on (service, instance, major_version), so an ack for a different
    major version belongs to a different service and must not be forwarded.
    """
    sd_entry = Mock()
    sd_entry.service_id = service_id
    sd_entry.instance_id = instance_id
    sd_entry.major_version = major_version
    entry = Mock()
    entry.sd_entry = sd_entry
    entry.eventgroup_id = eventgroup_id
    return entry


@pytest.mark.asyncio
async def test_subscribe_ack_notifies_requesting_client(
    daemon: SomeipDaemon,
    subscribe_event_group_request_udp: SubscribeEventGroupRequest,
):
    # A client asks to subscribe, so the daemon knows which writer to notify.
    await daemon._handle_subscribe_eventgroup_request(
        subscribe_event_group_request_udp, 1
    )
    subscription = daemon._requested_subscriptions.subscriptions[0]
    tx_queue = asyncio.Queue()
    daemon._tx_queues[1] = tx_queue

    daemon._handle_sd_subscribe_ack_eventgroup_entry(
        _eventgroup_entry(
            subscription.service_id,
            subscription.instance_id,
            subscription.eventgroup.id,
            subscription.major_version,
        )
    )

    assert tx_queue.qsize() == 1
    message = _decode_client_message(tx_queue.get_nowait())
    assert message["type"] == "SubscriptionStateChanged"
    assert message["state"] == "acknowledged"
    assert message["service_id"] == subscription.service_id
    assert message["event_group_id"] == subscription.eventgroup.id


@pytest.mark.asyncio
async def test_subscribe_nack_reports_rejected(
    daemon: SomeipDaemon,
    subscribe_event_group_request_udp: SubscribeEventGroupRequest,
):
    await daemon._handle_subscribe_eventgroup_request(
        subscribe_event_group_request_udp, 1
    )
    subscription = daemon._requested_subscriptions.subscriptions[0]
    tx_queue = asyncio.Queue()
    daemon._tx_queues[1] = tx_queue

    daemon._handle_sd_subscribe_nack_eventgroup_entry(
        _eventgroup_entry(
            subscription.service_id,
            subscription.instance_id,
            subscription.eventgroup.id,
            subscription.major_version,
        )
    )

    assert tx_queue.qsize() == 1
    message = _decode_client_message(tx_queue.get_nowait())
    assert message["state"] == "rejected"


@pytest.mark.asyncio
async def test_ack_for_other_eventgroup_is_not_forwarded(
    daemon: SomeipDaemon,
    subscribe_event_group_request_udp: SubscribeEventGroupRequest,
):
    await daemon._handle_subscribe_eventgroup_request(
        subscribe_event_group_request_udp, 1
    )
    subscription = daemon._requested_subscriptions.subscriptions[0]
    tx_queue = asyncio.Queue()
    daemon._tx_queues[1] = tx_queue

    daemon._handle_sd_subscribe_ack_eventgroup_entry(
        _eventgroup_entry(
            subscription.service_id,
            subscription.instance_id,
            subscription.eventgroup.id + 1,  # a different eventgroup
            subscription.major_version,
        )
    )

    assert tx_queue.qsize() == 0


@pytest.mark.asyncio
async def test_ack_without_a_requesting_client_is_ignored(daemon: SomeipDaemon):
    tx_queue = asyncio.Queue()
    daemon._tx_queues[1] = tx_queue

    # Nobody subscribed, so an ack for this service must not be forwarded.
    daemon._handle_sd_subscribe_ack_eventgroup_entry(
        _eventgroup_entry(0x1234, 1, 1)
    )

    assert tx_queue.qsize() == 0


# ---------------------------------------------------------------------------
# Ephemeral client reception port (client_endpoint_port = 0)
# ---------------------------------------------------------------------------


def _subscribe_request_with_port(eventgroup, port: int):
    return SubscribeEventGroupRequest(
        service_id=1,
        instance_id=2,
        major_version=3,
        ttl_subscription=10,
        eventgroup=eventgroup.to_json(),
        client_endpoint_ip="192.168.1.1",
        client_endpoint_port=port,
        udp=True,
        tcp=False,
    )


def _fake_udp_endpoint(ip: str, port: int):
    """Stand-in for a bound UDP endpoint reporting the port the OS gave it."""
    endpoint = Mock()
    endpoint.src_ip = Mock(return_value=ip)
    endpoint.src_port = Mock(return_value=port)
    endpoint.protocol = Mock(return_value=TransportLayerProtocol.UDP)
    return endpoint


@pytest.mark.asyncio
async def test_port_zero_subscription_advertises_the_bound_port(
    daemon: SomeipDaemon, eventgroup
):
    # The factory binds port 0 and reports back whatever the OS chose.
    daemon._endpoint_factory.create_server_endpoint = AsyncMock(
        return_value=_fake_udp_endpoint("192.168.1.1", 54321)
    )

    await daemon._handle_subscribe_eventgroup_request(
        _subscribe_request_with_port(eventgroup, 0), 1
    )

    subscription = daemon._requested_subscriptions.subscriptions[0]
    # Advertising 0 would tell the offering side to send events nowhere.
    assert subscription.client_endpoint.port == 54321


@pytest.mark.asyncio
async def test_named_port_is_used_as_given(daemon: SomeipDaemon, eventgroup):
    daemon._endpoint_factory.create_server_endpoint = AsyncMock(
        return_value=_fake_udp_endpoint("192.168.1.1", 3002)
    )

    await daemon._handle_subscribe_eventgroup_request(
        _subscribe_request_with_port(eventgroup, 3002), 1
    )

    subscription = daemon._requested_subscriptions.subscriptions[0]
    assert subscription.client_endpoint.port == 3002


@pytest.mark.asyncio
async def test_second_port_zero_subscription_reuses_one_endpoint(
    daemon: SomeipDaemon, eventgroup
):
    # A client asking for "any free port" wants one reception port for all of its
    # subscriptions, not a fresh socket per subscription.
    endpoint = _fake_udp_endpoint("192.168.1.1", 54321)
    daemon._endpoint_factory.create_server_endpoint = AsyncMock(return_value=endpoint)
    daemon._someip_client_endpoints.get_endpoints = Mock(return_value=[endpoint])

    await daemon._handle_subscribe_eventgroup_request(
        _subscribe_request_with_port(eventgroup, 0), 1
    )
    await daemon._handle_subscribe_eventgroup_request(
        _subscribe_request_with_port(eventgroup, 0), 1
    )

    assert daemon._endpoint_factory.create_server_endpoint.await_count == 0
    for subscription in daemon._requested_subscriptions.subscriptions:
        assert subscription.client_endpoint.port == 54321


@pytest.mark.asyncio
async def test_port_zero_unsubscribe_matches_the_subscription(
    daemon: SomeipDaemon, eventgroup
):
    # client_endpoint is part of Subscription equality, so an unsubscribe that
    # rebuilt the key from the requested 0 would never remove anything.
    endpoint = _fake_udp_endpoint("192.168.1.1", 54321)
    daemon._endpoint_factory.create_server_endpoint = AsyncMock(return_value=endpoint)

    await daemon._handle_subscribe_eventgroup_request(
        _subscribe_request_with_port(eventgroup, 0), 1
    )
    assert len(daemon._requested_subscriptions) == 1

    daemon._someip_client_endpoints.get_endpoints = Mock(return_value=[endpoint])
    daemon._handle_stop_subscribe_eventgroup_request(
        StopSubscribeEventGroupRequest(
            service_id=1,
            instance_id=2,
            major_version=3,
            eventgroup=eventgroup.to_json(),
            client_endpoint_ip="192.168.1.1",
            client_endpoint_port=0,
            udp=True,
            tcp=False,
        ),
        1,
    )
    assert len(daemon._requested_subscriptions) == 0
