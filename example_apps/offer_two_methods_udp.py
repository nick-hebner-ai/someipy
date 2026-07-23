"""Offers two method services from one client on two different UDP ports.

Used by the integration test for method-response endpoint routing: a decoy
service is offered first (so its endpoint is the client's first endpoint), then
the service a peer actually calls is offered second on a different port. A peer
calling the second service must receive the response from that service's own
port; otherwise a strict peer rejects it.
"""

import asyncio
import ipaddress
import logging
import sys
from typing import Tuple

from someipy import (
    TransportLayerProtocol,
    MethodResult,
    ReturnCode,
    MessageType,
    connect_to_someipy_daemon,
    ServerServiceInstance,
    ServiceBuilder,
    Method,
)
from someipy.someipy_logging import set_someipy_log_level
from someipy.serialization import Sint32
from addition_method_parameters import Addends, Sum

DEFAULT_INTERFACE_IP = "127.0.0.1"

# The service the peer actually calls (matches the vsomeip caller's target).
CALLED_SERVICE_ID = 0x1234
# A decoy service offered first, so the called service is not the client's
# first endpoint.
DECOY_SERVICE_ID = 0x1235
SAMPLE_INSTANCE_ID = 0x5678
SAMPLE_METHOD_ID = 0x0123


async def add_method_handler(input_data: bytes, addr: Tuple[str, int]) -> MethodResult:
    print(
        f"Received data: {' '.join(f'0x{b:02x}' for b in input_data)} from IP: {addr[0]} Port: {addr[1]}"
    )
    result = MethodResult()
    try:
        addends = Addends()
        addends.deserialize(input_data)
    except Exception as e:
        print(f"Error during deserialization: {e}")
        result.message_type = MessageType.RESPONSE
        result.return_code = ReturnCode.E_MALFORMED_MESSAGE
        return result

    sum = Sum()
    sum.value = Sint32(addends.addend1.value + addends.addend2.value)
    result.message_type = MessageType.RESPONSE
    result.return_code = ReturnCode.E_OK
    result.payload = sum.serialize()
    return result


def _build_service(service_id):
    method = Method(
        id=SAMPLE_METHOD_ID,
        protocol=TransportLayerProtocol.UDP,
        method_handler=add_method_handler,
    )
    return (
        ServiceBuilder()
        .with_service_id(service_id)
        .with_major_version(1)
        .with_method(method)
        .build()
    )


async def main():
    set_someipy_log_level(logging.DEBUG)

    interface_ip = DEFAULT_INTERFACE_IP
    for i, arg in enumerate(sys.argv):
        if arg == "--interface_ip" and i + 1 < len(sys.argv):
            interface_ip = sys.argv[i + 1]
            break

    someipy_daemon = await connect_to_someipy_daemon()

    # Offer the decoy first (port 3000), then the called service (port 3001).
    decoy = ServerServiceInstance(
        daemon=someipy_daemon,
        service=_build_service(DECOY_SERVICE_ID),
        instance_id=SAMPLE_INSTANCE_ID,
        endpoint_ip=interface_ip,
        endpoint_port=3000,
        ttl=5,
        cyclic_offer_delay_ms=2000,
    )
    called = ServerServiceInstance(
        daemon=someipy_daemon,
        service=_build_service(CALLED_SERVICE_ID),
        instance_id=SAMPLE_INSTANCE_ID,
        endpoint_ip=interface_ip,
        endpoint_port=3001,
        ttl=5,
        cyclic_offer_delay_ms=2000,
    )

    print("Start offering services..")
    await decoy.start_offer()
    await called.start_offer()

    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        await decoy.stop_offer()
        await called.stop_offer()
    finally:
        await someipy_daemon.disconnect_from_daemon()


if __name__ == "__main__":
    asyncio.run(main())
