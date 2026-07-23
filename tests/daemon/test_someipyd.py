from asyncio import DatagramTransport
import asyncio
import base64
import ipaddress
import logging
import time
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, Mock, patch
import someipy
from someipy._internal._common.endpoint import Endpoint
from someipy._internal._daemon.daemon_server import ClientConnectedEventArgs
from someipy._internal._daemon.daemon_server_client import DaemonServerClient
from someipy._internal._daemon.subscription import Subscription
from someipy._internal._daemon.uds_messages import (
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


def test_find_service_request_sends_negative_response(daemon: SomeipDaemon):
    pass


def test_find_service_request_sends_positive_response(daemon: SomeipDaemon):
    pass


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
