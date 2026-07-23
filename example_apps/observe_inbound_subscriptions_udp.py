"""Offers an eventgroup and prints InboundSubscription notifications.

Used by the integration test for the inbound-subscription notification: when a
remote peer subscribes to the offered eventgroup, the daemon notifies this
client with an InboundSubscription message, which the client surfaces on its
receive queue. This app offers the service and prints those notifications.
"""

import asyncio
import logging
import sys

from someipy import (
    TransportLayerProtocol,
    ServiceBuilder,
    EventGroup,
    connect_to_someipy_daemon,
    ServerServiceInstance,
    Event,
)
from someipy.someipy_logging import set_someipy_log_level

DEFAULT_INTERFACE_IP = "127.0.0.1"

SAMPLE_SERVICE_ID = 0x1234
SAMPLE_INSTANCE_ID = 0x5678
SAMPLE_EVENTGROUP_ID = 0x0321
SAMPLE_EVENT_ID = 0x0123


async def _observe_subscriptions(daemon):
    while True:
        msg = await daemon._rx_message_queue.get()
        if isinstance(msg, dict) and msg.get("type") == "InboundSubscription":
            print(
                "InboundSubscription received: "
                f"service=0x{msg['service_id']:04x} "
                f"eventgroup=0x{msg['event_group_id']:04x} "
                f"subscriber={msg['subscriber_ip']}:{msg['subscriber_port']} "
                f"is_renewal={msg['is_renewal']}"
            )


async def main():
    set_someipy_log_level(logging.DEBUG)

    interface_ip = DEFAULT_INTERFACE_IP
    for i, arg in enumerate(sys.argv):
        if arg == "--interface_ip" and i + 1 < len(sys.argv):
            interface_ip = sys.argv[i + 1]
            break

    someipy_daemon = await connect_to_someipy_daemon()

    event = Event(id=SAMPLE_EVENT_ID, protocol=TransportLayerProtocol.UDP)
    eventgroup = EventGroup(id=SAMPLE_EVENTGROUP_ID, events=[event])
    service = (
        ServiceBuilder()
        .with_service_id(SAMPLE_SERVICE_ID)
        .with_major_version(1)
        .with_eventgroup(eventgroup)
        .build()
    )

    service_instance = ServerServiceInstance(
        daemon=someipy_daemon,
        service=service,
        instance_id=SAMPLE_INSTANCE_ID,
        endpoint_ip=interface_ip,
        endpoint_port=3000,
        ttl=5,
        cyclic_offer_delay_ms=2000,
    )

    print("Start offering service..")
    await service_instance.start_offer()

    # Once offering, watch for inbound-subscription notifications from the daemon.
    observer = asyncio.create_task(_observe_subscriptions(someipy_daemon))
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        observer.cancel()
        await service_instance.stop_offer()
    finally:
        await someipy_daemon.disconnect_from_daemon()


if __name__ == "__main__":
    asyncio.run(main())
