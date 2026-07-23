"""Discovers a service, lets its cached offer expire, then calls it again.

Used by the integration test for active re-discovery: after the cached
OfferService expires (the peer has stopped re-offering), calling the method
again must still succeed -- which requires the daemon to actively send a
FindService to re-resolve the service. Without that, the second call fails
because the service is no longer in the cache.
"""

import asyncio
import logging
import sys

from someipy import (
    TransportLayerProtocol,
    MessageType,
    ReturnCode,
    connect_to_someipy_daemon,
    ClientServiceInstance,
    Method,
    ServiceBuilder,
)
from someipy.someipy_logging import set_someipy_log_level
from addition_method_parameters import Addends, Sum

DEFAULT_INTERFACE_IP = "127.0.0.1"
SAMPLE_SERVICE_ID = 0x1234
SAMPLE_INSTANCE_ID = 0x5678
SAMPLE_METHOD_ID = 0x0123


async def _wait_available(client, timeout_s):
    deadline = timeout_s
    while deadline > 0:
        if await client.is_available():
            return True
        await asyncio.sleep(0.5)
        deadline -= 0.5
    return False


async def _call_once(client):
    result = await client.call_method(SAMPLE_METHOD_ID, Addends(addend1=1, addend2=2).serialize())
    if result.message_type == MessageType.RESPONSE and result.return_code == ReturnCode.E_OK:
        return Sum().deserialize(result.payload).value.value
    return None


async def main():
    set_someipy_log_level(logging.DEBUG)

    interface_ip = DEFAULT_INTERFACE_IP
    for i, arg in enumerate(sys.argv):
        if arg == "--interface_ip" and i + 1 < len(sys.argv):
            interface_ip = sys.argv[i + 1]
            break

    someipy_daemon = await connect_to_someipy_daemon()

    service = (
        ServiceBuilder()
        .with_service_id(SAMPLE_SERVICE_ID)
        .with_major_version(1)
        .with_method(Method(id=SAMPLE_METHOD_ID, protocol=TransportLayerProtocol.UDP))
        .build()
    )
    client = ClientServiceInstance(
        daemon=someipy_daemon,
        service=service,
        instance_id=SAMPLE_INSTANCE_ID,
        endpoint_ip=interface_ip,
        endpoint_port=3002,
    )

    try:
        # Initial discovery (from the peer's start-up offer burst).
        if not await _wait_available(client, timeout_s=12):
            print("INITIAL_DISCOVERY_FAILED")
            return
        print("INITIAL_DISCOVERY_OK")

        # Let the cached offer expire. The peer has stopped re-offering (large
        # cyclic_offer_delay), so the only way to resolve it again is an active
        # FindService from the daemon.
        await asyncio.sleep(8)

        if not await _wait_available(client, timeout_s=8):
            print("REDISCOVER_FAILED")
            return
        value = await _call_once(client)
        if value is not None:
            print(f"REDISCOVERED Sum: {value}")
        else:
            print("REDISCOVER_CALL_FAILED")
    finally:
        await someipy_daemon.disconnect_from_daemon()


if __name__ == "__main__":
    asyncio.run(main())
