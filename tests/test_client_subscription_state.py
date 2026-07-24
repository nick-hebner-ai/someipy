"""Tests for reporting SubscribeEventgroupAck/Nack to the subscribing client.

A subscribe request is fire-and-forget on the wire, so a client can only learn
the outcome if the daemon forwards the SD answer. These cover the client half:
turning a SubscriptionStateChanged message into per-eventgroup state, notifying a
registered callback, and routing the message to the right service instance.
"""

import asyncio

import pytest

from someipy._internal._daemon.someipy_daemon_client import SomeIpDaemonClient
from someipy._internal._daemon.uds_messages import (
    SUBSCRIPTION_STATE_ACKNOWLEDGED,
    SUBSCRIPTION_STATE_REJECTED,
    SubscriptionStateChanged,
    create_uds_message,
)
from someipy.client_service_instance import ClientServiceInstance
from someipy.service import ServiceBuilder


def _client_instance(service_id: int = 0x1234, instance_id: int = 0x5678):
    service = ServiceBuilder().with_service_id(service_id).with_major_version(1).build()
    instance = ClientServiceInstance.__new__(ClientServiceInstance)
    instance._service = service
    instance._instance_id = instance_id
    instance._subscription_states = {}
    instance._subscription_state_callback = None
    return instance


def _state_message(service_id, instance_id, eventgroup_id, state):
    return create_uds_message(
        SubscriptionStateChanged,
        service_id=service_id,
        instance_id=instance_id,
        event_group_id=eventgroup_id,
        state=state,
    )


class TestSubscriptionStateTracking:
    def test_unknown_before_any_answer(self):
        instance = _client_instance()
        # Distinct from "rejected": the request may simply be unanswered.
        assert instance.subscription_state(0x0321) == "unknown"

    def test_ack_recorded(self):
        instance = _client_instance()
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )
        assert instance.subscription_state(0x0321) == SUBSCRIPTION_STATE_ACKNOWLEDGED

    def test_nack_recorded(self):
        instance = _client_instance()
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_REJECTED)
        )
        assert instance.subscription_state(0x0321) == SUBSCRIPTION_STATE_REJECTED

    def test_eventgroups_tracked_independently(self):
        instance = _client_instance()
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0001, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0002, SUBSCRIPTION_STATE_REJECTED)
        )
        assert instance.subscription_states == {
            0x0001: SUBSCRIPTION_STATE_ACKNOWLEDGED,
            0x0002: SUBSCRIPTION_STATE_REJECTED,
        }

    def test_later_answer_supersedes_earlier(self):
        # A renewed subscription may be refused after having been accepted.
        instance = _client_instance()
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_REJECTED)
        )
        assert instance.subscription_state(0x0321) == SUBSCRIPTION_STATE_REJECTED

    def test_states_property_is_a_copy(self):
        instance = _client_instance()
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )
        snapshot = instance.subscription_states
        snapshot[0x0321] = "tampered"
        assert instance.subscription_state(0x0321) == SUBSCRIPTION_STATE_ACKNOWLEDGED


class TestSubscriptionStateCallback:
    def test_callback_invoked_with_eventgroup_and_state(self):
        instance = _client_instance()
        seen = []
        instance.register_subscription_state_callback(
            lambda eventgroup_id, state: seen.append((eventgroup_id, state))
        )
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )
        assert seen == [(0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)]

    def test_no_callback_registered_is_harmless(self):
        instance = _client_instance()
        instance._subscription_state_received(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )
        assert instance.subscription_state(0x0321) == SUBSCRIPTION_STATE_ACKNOWLEDGED


class TestDaemonClientRouting:
    @pytest.mark.asyncio
    async def test_message_routed_to_matching_instance_only(self):
        daemon_client = SomeIpDaemonClient.__new__(SomeIpDaemonClient)
        daemon_client._client_service_instances = []
        daemon_client._server_service_instances = []
        daemon_client._rx_message_queue = asyncio.Queue()

        target = _client_instance(0x1234, 0x5678)
        other_service = _client_instance(0x4321, 0x5678)
        other_instance = _client_instance(0x1234, 0x9999)
        daemon_client._client_service_instances = [
            target,
            other_service,
            other_instance,
        ]

        await daemon_client._handle_message(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )

        assert target.subscription_state(0x0321) == SUBSCRIPTION_STATE_ACKNOWLEDGED
        # A different service id or instance id is a different subscription.
        assert other_service.subscription_state(0x0321) == "unknown"
        assert other_instance.subscription_state(0x0321) == "unknown"

    @pytest.mark.asyncio
    async def test_state_message_is_not_left_on_the_request_queue(self):
        # The rx queue belongs to in-flight request/response exchanges; an
        # unsolicited push must be consumed, not queued for a waiting caller.
        daemon_client = SomeIpDaemonClient.__new__(SomeIpDaemonClient)
        daemon_client._client_service_instances = [_client_instance()]
        daemon_client._server_service_instances = []
        daemon_client._rx_message_queue = asyncio.Queue()

        await daemon_client._handle_message(
            _state_message(0x1234, 0x5678, 0x0321, SUBSCRIPTION_STATE_ACKNOWLEDGED)
        )

        assert daemon_client._rx_message_queue.qsize() == 0
