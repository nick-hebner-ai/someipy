import argparse
import asyncio
import logging
import socket
import struct

from someipy import (
    TransportLayerProtocol,
    ServiceBuilder,
    EventGroup,
    connect_to_someipy_daemon,
    ServerServiceInstance,
    Event,
)
from someipy.someipy_logging import set_someipy_log_level

DEFAULT_INTERFACE_IP = "127.0.0.1"  # Default IP if not provided

SAMPLE_SERVICE_ID = 0x1234  # dec 4660
SAMPLE_INSTANCE_ID = 0x5678  # dec 22136
SAMPLE_EVENTGROUP_ID = 0x0321  # dec 801
SAMPLE_EVENT_ID = 0x0123  # dec 291
SAMPLE_PORT = 3000

SEND_INTERVAL_S = 0.5
PHASE_A_S = 5
PHASE_B_S = 3
RESUBSCRIBE_WAIT_S = 4
PHASE_C_S = 4


def build_notification(payload: bytes, session_id: int) -> bytes:
    """Build a complete SOME/IP notification datagram around ``payload``."""
    header = struct.pack(
        ">HHIHHBBBB",
        SAMPLE_SERVICE_ID,
        SAMPLE_EVENT_ID,
        8 + len(payload),
        0x0000,
        session_id,
        1,
        1,
        0x02,
        0x00,
    )
    return header + payload


async def send_phase(
    instance, marker: bytes, phase: str, duration_s: float, start: int
):
    """Send one datagram every SEND_INTERVAL_S for ``duration_s`` seconds."""
    counter = start
    for _ in range(int(duration_s / SEND_INTERVAL_S)):
        payload = marker + struct.pack(">I", counter)
        instance.send_raw(
            SAMPLE_EVENTGROUP_ID, build_notification(payload, counter & 0xFFFF)
        )
        print(f"raw sent phase={phase} n={counter} payload={payload.hex(' ')}")
        counter += 1
        await asyncio.sleep(SEND_INTERVAL_S)
    return counter


def probe_local_bind(ip: str):
    """Report whether ``ip`` is an address this process can bind locally."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((ip, 0))
            print("local bind of offer address succeeded")
        except OSError as e:
            print(f"local bind of offer address failed: errno={e.errno}")


async def main():
    set_someipy_log_level(logging.DEBUG)

    parser = argparse.ArgumentParser()
    parser.add_argument("--interface_ip", default=DEFAULT_INTERFACE_IP)
    parser.add_argument("--offer_ip", default=None)
    parser.add_argument("--daemon_tcp_host", default=None)
    parser.add_argument("--daemon_tcp_port", type=int, default=30500)
    parser.add_argument("--probe_local_bind", action="store_true")
    args = parser.parse_args()
    offer_ip = args.offer_ip or args.interface_ip

    if args.probe_local_bind:
        probe_local_bind(offer_ip)

    if args.daemon_tcp_host is not None:
        someipy_daemon = await connect_to_someipy_daemon(
            {
                "use_tcp": True,
                "tcp_host": args.daemon_tcp_host,
                "tcp_port": args.daemon_tcp_port,
            }
        )
    else:
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

    instance = ServerServiceInstance(
        daemon=someipy_daemon,
        service=service,
        instance_id=SAMPLE_INSTANCE_ID,
        endpoint_ip=offer_ip,
        endpoint_port=SAMPLE_PORT,
        ttl=5,
        cyclic_offer_delay_ms=2000,
    )

    print("Start offering service..")
    await instance.start_offer()

    try:
        counter = await send_phase(instance, b"RAWA", "A", PHASE_A_S, 0)

        await instance.stop_offer()
        print("offer stopped")
        counter = await send_phase(instance, b"RAWB", "B", PHASE_B_S, counter)

        await instance.start_offer()
        print("offer restarted")
        await asyncio.sleep(RESUBSCRIBE_WAIT_S)
        await send_phase(instance, b"RAWC", "C", PHASE_C_S, counter)
        print("raw send done")

        await asyncio.Future()
    except asyncio.CancelledError:
        await instance.stop_offer()
    finally:
        await someipy_daemon.disconnect_from_daemon()


if __name__ == "__main__":
    asyncio.run(main())
