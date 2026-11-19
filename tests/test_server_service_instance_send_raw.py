"""Tests for the client half of ``ServerServiceInstance.send_raw``."""

import base64
from unittest.mock import MagicMock, patch

import pytest

from someipy._internal._daemon.uds_messages import SendRawRequest
from someipy._internal.transport_layer_protocol import TransportLayerProtocol
from someipy.server_service_instance import ServerServiceInstance
from someipy.service import Event, EventGroup, ServiceBuilder

UDP_EVENTGROUP_ID = 0x0321
TCP_EVENTGROUP_ID = 0x0322


@pytest.fixture
def daemon():
    return MagicMock()


@pytest.fixture
def instance(daemon):
    service = (
        ServiceBuilder()
        .with_service_id(0x1234)
        .with_major_version(1)
        .with_eventgroup(
            EventGroup(
                id=UDP_EVENTGROUP_ID,
                events=[Event(id=0x0123, protocol=TransportLayerProtocol.UDP)],
            )
        )
        .with_eventgroup(
            EventGroup(
                id=TCP_EVENTGROUP_ID,
                events=[Event(id=0x0124, protocol=TransportLayerProtocol.TCP)],
            )
        )
        .build()
    )
    return ServerServiceInstance(daemon, service, 0x5678, "127.0.0.1", 3000)


def test_sends_one_request_describing_the_instance(instance, daemon):
    instance.send_raw(UDP_EVENTGROUP_ID, b"RAW1")

    daemon.transmit_message_to_daemon.assert_called_once()
    message = daemon.transmit_message_to_daemon.call_args.args[0]
    assert message["type"] == SendRawRequest.__name__
    assert message["service_id"] == 0x1234
    assert message["instance_id"] == 0x5678
    assert message["major_version"] == 1
    assert message["eventgroup_id"] == UDP_EVENTGROUP_ID
    assert message["src_endpoint_ip"] == "127.0.0.1"
    assert message["src_endpoint_port"] == 3000
    assert base64.b64decode(message["payload"]) == b"RAW1"


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x00", b"\xde\xad\xbe\xef", bytes(range(256)), b"no structure at all"],
)
def test_payload_round_trips_unchanged(instance, daemon, payload):
    instance.send_raw(UDP_EVENTGROUP_ID, payload)

    message = daemon.transmit_message_to_daemon.call_args.args[0]
    assert base64.b64decode(message["payload"]) == payload


def test_unknown_eventgroup_raises_and_sends_nothing(instance, daemon):
    with pytest.raises(ValueError):
        instance.send_raw(0x9999, b"RAW1")

    daemon.transmit_message_to_daemon.assert_not_called()


def test_tcp_only_eventgroup_raises_and_sends_nothing(instance, daemon):
    with pytest.raises(ValueError, match="UDP only"):
        instance.send_raw(TCP_EVENTGROUP_ID, b"RAW1")

    daemon.transmit_message_to_daemon.assert_not_called()


def test_does_not_open_a_socket(instance, daemon):
    with patch("socket.socket", side_effect=AssertionError("socket opened")):
        instance.send_raw(UDP_EVENTGROUP_ID, b"RAW1")

    daemon.transmit_message_to_daemon.assert_called_once()
