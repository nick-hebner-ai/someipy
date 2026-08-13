"""Tests for correlating a method response back to the caller that issued it.

A client may declare endpoint_port=0 and let the OS assign the real port. The
daemon then holds two keys for the same call: one built when the request is
issued, one built when the response arrives. The response side can only use the
real bound port, because that is the address the datagram arrives on -- so the
request side has to use it too, or the keys never match and every response is
dropped with "Received response for unknown method call".

The same 0-vs-real-port mismatch existed one layer up, in the daemon client's
dispatch loop, which filtered candidate service instances on an endpoint tuple
holding the declared port. Both are covered here: a declared port of 0 is used
throughout, since that is the case where the two values differ.
"""

import asyncio
import base64

import pytest

from someipy._internal._daemon.someipy_daemon_client import SomeIpDaemonClient
from someipy._internal._daemon.uds_messages import (
    OutboundCallMethodRequest,
    OutboundCallMethodResponse,
    create_uds_message,
)
from someipy._internal.someip_header import SomeIpHeader
from someipy._internal.transport_layer_protocol import TransportLayerProtocol
from someipy.client_service_instance import ClientServiceInstance, MethodCall
from someipy.service import Method, ServiceBuilder
from someipy.someipyd import MethodCall as DaemonMethodCall
from someipy.someipyd import SomeipDaemon

SERVICE_ID = 0x1234
INSTANCE_ID = 0x5678
METHOD_ID = 0x0001
CLIENT_ID = 0x0011
SESSION_ID = 0x0022

CALLER_IP = "192.168.1.10"
DECLARED_PORT = 0  # what a client that wants an OS-assigned port sends
REAL_BOUND_PORT = 51000  # what the OS actually gave it
PROVIDER_IP = "192.168.1.20"
PROVIDER_PORT = 30509


def _service_with_method():
    return (
        ServiceBuilder()
        .with_service_id(SERVICE_ID)
        .with_major_version(1)
        .with_method(Method(id=METHOD_ID, protocol=TransportLayerProtocol.UDP))
        .build()
    )


class _FakeEndpoint:
    """Stands in for a UDP client endpoint bound to an OS-assigned port."""

    def __init__(self, real_port: int):
        self._real_port = real_port
        self.sent = []

    def src_port(self) -> int:
        return self._real_port

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class _FakeEndpointStorage:
    def __init__(self, endpoint):
        self._endpoint = endpoint

    def has_endpoint(self, ip, port, protocol) -> bool:
        return True

    def get_endpoint_by_ip_port(self, ip, port, protocol):
        return self._endpoint


class _RecordingLogger:
    def __init__(self):
        self.warnings = []

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, msg, *_args, **_kwargs):
        self.warnings.append(msg)

    def error(self, *_args, **_kwargs):
        pass


def _request_message(src_port: int):
    return create_uds_message(
        OutboundCallMethodRequest,
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        method_id=METHOD_ID,
        client_id=CLIENT_ID,
        session_id=SESSION_ID,
        protocol_version=0x01,
        major_version=1,
        minor_version=0,
        dst_endpoint_ip=PROVIDER_IP,
        dst_endpoint_port=PROVIDER_PORT,
        src_endpoint_ip=CALLER_IP,
        src_endpoint_port=src_port,
        protocol=TransportLayerProtocol.UDP.value,
        payload=base64.b64encode(b"\x01\x02").decode(),
    )


def _response_message(dst_port: int, payload: bytes = b"\x03\x04", method_id=METHOD_ID):
    return create_uds_message(
        OutboundCallMethodResponse,
        service_id=SERVICE_ID,
        method_id=method_id,
        client_id=CLIENT_ID,
        session_id=SESSION_ID,
        return_code=0x00,
        dst_endpoint_ip=CALLER_IP,
        dst_endpoint_port=dst_port,
        payload=base64.b64encode(payload).decode(),
    )


def _daemon_with_endpoint(endpoint):
    daemon = SomeipDaemon.__new__(SomeipDaemon)
    daemon.logger = _RecordingLogger()
    daemon._issued_method_calls = {}
    daemon._someip_client_endpoints = _FakeEndpointStorage(endpoint)
    return daemon


class TestDaemonIssuedCallKey:
    @pytest.mark.asyncio
    async def test_issued_call_is_keyed_by_the_real_bound_port(self):
        endpoint = _FakeEndpoint(REAL_BOUND_PORT)
        daemon = _daemon_with_endpoint(endpoint)

        await daemon._handle_outbound_call_method_request(
            _request_message(DECLARED_PORT), client_id=7
        )

        assert len(daemon._issued_method_calls) == 1
        issued = next(iter(daemon._issued_method_calls))
        # Keyed by 0 -- the declared value -- the response can never match it.
        assert issued.src_port == REAL_BOUND_PORT

    @pytest.mark.asyncio
    async def test_issued_key_matches_the_key_the_response_path_builds(self):
        # The invariant that actually matters. _someip_message_callback builds
        # its lookup key from the address the datagram arrived on, so that key
        # is reconstructed here and required to be present.
        endpoint = _FakeEndpoint(REAL_BOUND_PORT)
        daemon = _daemon_with_endpoint(endpoint)

        await daemon._handle_outbound_call_method_request(
            _request_message(DECLARED_PORT), client_id=7
        )

        header = SomeIpHeader(
            service_id=SERVICE_ID,
            method_id=METHOD_ID,
            client_id=CLIENT_ID,
            session_id=SESSION_ID,
            protocol_version=0x01,
            interface_version=1,
            message_type=0x80,  # RESPONSE
            return_code=0x00,
            length=8,
        )
        arrived_on = (CALLER_IP, REAL_BOUND_PORT)
        response_key = DaemonMethodCall(
            service_id=header.service_id,
            method_id=header.method_id,
            client_id=header.client_id,
            session_id=header.session_id,
            src_ip=arrived_on[0],
            src_port=arrived_on[1],
        )

        assert response_key in daemon._issued_method_calls

    @pytest.mark.asyncio
    async def test_request_is_still_sent_to_the_provider(self):
        # Guard against fixing correlation by breaking the send path.
        endpoint = _FakeEndpoint(REAL_BOUND_PORT)
        daemon = _daemon_with_endpoint(endpoint)

        await daemon._handle_outbound_call_method_request(
            _request_message(DECLARED_PORT), client_id=7
        )

        assert len(endpoint.sent) == 1
        _payload, addr = endpoint.sent[0]
        assert addr == (PROVIDER_IP, PROVIDER_PORT)


def _client_instance_awaiting_response(declared_port: int):
    """A client instance with one method call in flight."""
    instance = ClientServiceInstance.__new__(ClientServiceInstance)
    instance._service = _service_with_method()
    instance._instance_id = INSTANCE_ID
    instance._client_id = CLIENT_ID
    # `endpoint` is a read-only property over these two fields.
    instance._endpoint_ip = CALLER_IP
    instance._endpoint_port = declared_port
    future = asyncio.get_event_loop().create_future()
    instance._method_call_futures = {
        MethodCall(
            service_id=SERVICE_ID,
            method_id=METHOD_ID,
            client_id=CLIENT_ID,
            session_id=SESSION_ID,
        ): future
    }
    return instance, future


def _daemon_client_with(instances):
    daemon_client = SomeIpDaemonClient.__new__(SomeIpDaemonClient)
    daemon_client._client_service_instances = instances
    daemon_client._server_service_instances = []
    daemon_client._rx_message_queue = asyncio.Queue()
    return daemon_client


class TestResponseReachesTheCaller:
    @pytest.mark.asyncio
    async def test_response_delivered_when_bound_port_differs_from_declared(self):
        instance, future = _client_instance_awaiting_response(DECLARED_PORT)
        daemon_client = _daemon_client_with([instance])

        await daemon_client._handle_message(_response_message(REAL_BOUND_PORT))

        assert future.done(), (
            "response was dropped before reaching the caller: the instance "
            "declared port 0 while the daemon reported the real bound port"
        )
        assert future.result().payload == b"\x03\x04"

    @pytest.mark.asyncio
    async def test_response_for_an_unknown_method_is_not_delivered(self):
        # Dispatch is by service and method; an unrelated method must not
        # resolve this caller's future.
        instance, future = _client_instance_awaiting_response(DECLARED_PORT)
        daemon_client = _daemon_client_with([instance])

        await daemon_client._handle_message(
            _response_message(REAL_BOUND_PORT, method_id=0x00FF)
        )

        assert not future.done()

    @pytest.mark.asyncio
    async def test_sibling_instance_of_same_service_does_not_steal_the_response(self):
        # Dispatch no longer filters on the endpoint tuple, so every instance of
        # the service is offered the response. Only the one holding a matching
        # in-flight call may consume it -- otherwise the eight-instances-of-one
        # -service case would cross-resolve.
        caller, caller_future = _client_instance_awaiting_response(DECLARED_PORT)
        sibling, sibling_future = _client_instance_awaiting_response(DECLARED_PORT)
        # The sibling is waiting on a different session of the same method.
        sibling._method_call_futures = {
            MethodCall(
                service_id=SERVICE_ID,
                method_id=METHOD_ID,
                client_id=CLIENT_ID,
                session_id=SESSION_ID + 1,
            ): sibling_future
        }
        daemon_client = _daemon_client_with([caller, sibling])

        await daemon_client._handle_message(_response_message(REAL_BOUND_PORT))

        assert caller_future.done()
        assert not sibling_future.done()

    @pytest.mark.asyncio
    async def test_response_is_not_left_on_the_request_queue(self):
        instance, _future = _client_instance_awaiting_response(DECLARED_PORT)
        daemon_client = _daemon_client_with([instance])

        await daemon_client._handle_message(_response_message(REAL_BOUND_PORT))

        assert daemon_client._rx_message_queue.qsize() == 0
