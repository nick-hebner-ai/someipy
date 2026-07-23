#!/usr/bin/env python3
"""Docker-based integration test runner for someipy.

Brings up two containers on a private bridge network -- one running the someipy
daemon plus an example app, the other running a vsomeip peer application built
from source -- so the two SOME/IP stacks talk over real multicast Service
Discovery exactly like two hosts on a network. This avoids the loopback
limitations of the single-host runner (two stacks cannot share one loopback,
and vsomeip's Service Discovery will not start without a route covering the SD
multicast group on its interface).

Prerequisites: build the image once with
    docker build -f integration_tests/docker/Dockerfile -t someipy-itest integration_tests/

Usage:
    python3 integration_tests/docker/run.py [test_name ...] [--duration N] [--keep]

With no test names, all registered tests run. The someipy source tree is mounted
into the daemon container, so whatever revision is checked out is what is tested.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

IMAGE = "someipy-itest"
NETWORK = "someipy-itest-net"
SUBNET = "172.28.0.0/16"
IP_SOMEIPY = "172.28.0.2"  # someipy daemon + app
IP_VSOMEIP = "172.28.0.3"  # vsomeip peer
SD_MULTICAST = "224.224.224.245"
SD_PORT = 30490

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kwargs)


def ensure_network():
    existing = _run(["docker", "network", "ls", "--format", "{{.Name}}"]).stdout.split()
    if NETWORK not in existing:
        result = _run(["docker", "network", "create", "--subnet", SUBNET, NETWORK])
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not create docker network {NETWORK} (subnet {SUBNET}); "
                f"another network with an overlapping subnet may already exist.\n"
                f"{result.stderr}"
            )
    _disable_bridge_multicast_snooping()


def _disable_bridge_multicast_snooping():
    # Docker bridges enable IGMP snooping but run no querier, which makes
    # multicast delivery between containers unreliable (group memberships are
    # not maintained). Disable snooping so the bridge floods multicast, giving
    # reliable Service Discovery delivery. Needs a privileged helper container
    # to write the host sysfs entry.
    net_id = _run(["docker", "network", "inspect", NETWORK, "-f", "{{.Id}}"]).stdout.strip()
    bridge = f"br-{net_id[:12]}"
    _run(
        ["docker", "run", "--rm", "--privileged", "--network", "host",
         "--entrypoint", "bash", IMAGE, "-c",
         f"echo 0 > /sys/class/net/{bridge}/bridge/multicast_snooping"]
    )


def someipyd_config(sd_address=SD_MULTICAST) -> str:
    return (
        '{\n'
        f'    "sd_address": "{sd_address}",\n'
        f'    "sd_port": {SD_PORT},\n'
        '    "log_level": "DEBUG",\n'
        f'    "interface": "{IP_SOMEIPY}",\n'
        '    "use_tcp": false,\n'
        f'    "tcp_host": "{IP_SOMEIPY}",\n'
        '    "tcp_port": 30500\n'
        '}\n'
    )


def vsomeip_config(offers=None, sd_address=SD_MULTICAST, sd_overrides=None) -> str:
    # `offers`, when given, is a list of {"service","instance","unreliable"}
    # dicts. vsomeip only multicasts OfferService for a service if the config
    # declares its port here; without it vsomeip treats the offer as internal
    # and never puts it on the wire. Client-role peers pass no offers.
    # `sd_overrides` merges into the service-discovery block (e.g. a large
    # cyclic_offer_delay so the peer stops re-offering, forcing the cache to
    # expire and requiring an active FindService to re-resolve it).
    cfg = {
        "unicast": IP_VSOMEIP,
        # vsomeip log levels are trace/debug/info/warning/error/fatal; "trace"
        # is the most verbose. An unrecognized value silently falls back to
        # "info", hiding SD join/find handling, so keep this a valid level.
        "logging": {"level": "trace", "console": "true",
                    "file": {"enable": "false"}, "dlt": "false"},
        "applications": [{"name": "Hello", "id": "0x1313"}],
        "routing": "Hello",
        "service-discovery": {
            "enable": "true",
            "multicast": sd_address,
            "port": str(SD_PORT),
            "protocol": "udp",
            "initial_delay_min": "10", "initial_delay_max": "100",
            "repetitions_base_delay": "200", "repetitions_max": "3",
            "ttl": "3", "cyclic_offer_delay": "2000",
            "request_response_delay": "1500",
        },
    }
    if sd_overrides:
        cfg["service-discovery"].update(sd_overrides)
    if offers:
        cfg["services"] = [
            {"service": o["service"], "instance": o["instance"],
             "unreliable": o["unreliable"]}
            for o in offers
        ]
    return json.dumps(cfg, indent=4)


class Case:
    """A test case: a someipy example app paired with a vsomeip peer app.

    evaluate(someipy_lines, vsomeip_lines) -> bool decides pass/fail from the
    captured stdout of each side.
    """

    def __init__(self, name, someipy_app, vsomeip_app, evaluate,
                 vsomeip_offers=None, sd_address=SD_MULTICAST,
                 vsomeip_sd_overrides=None, duration=16):
        self.name = name
        self.someipy_app = someipy_app  # file under example_apps/
        self.vsomeip_app = vsomeip_app  # installed dir/binary name
        self.evaluate = evaluate
        # Services the vsomeip peer should offer on the network (server-role
        # tests). Each is {"service","instance","unreliable"}. None = client.
        self.vsomeip_offers = vsomeip_offers
        self.sd_address = sd_address  # SD multicast group for this case
        self.vsomeip_sd_overrides = vsomeip_sd_overrides  # merge into SD config
        self.duration = duration  # per-case default run duration (seconds)


def run_case(case: "Case", duration: int, keep: bool) -> bool:
    ensure_network()
    name_a = f"someipy-itest-{case.name}-a"
    name_b = f"someipy-itest-{case.name}-b"
    _run(["docker", "rm", "-f", name_a, name_b])

    with tempfile.TemporaryDirectory() as workdir:
        cfg_dir = os.path.join(workdir, "cfg")
        logs_dir = os.path.join(workdir, "logs")
        os.makedirs(cfg_dir)
        os.makedirs(logs_dir)
        with open(os.path.join(cfg_dir, "someipyd.json"), "w") as f:
            f.write(someipyd_config(case.sd_address))
        with open(os.path.join(cfg_dir, "vsomeip-client.json"), "w") as f:
            f.write(vsomeip_config(case.vsomeip_offers, case.sd_address,
                                   case.vsomeip_sd_overrides))

        someipy_cmd = (
            "PYTHONPATH=/work/src python3 /work/src/someipy/someipyd.py "
            "--config /cfg/someipyd.json >/logs/daemon.log 2>&1 & "
            "sleep 2; "
            f"PYTHONPATH=/work/src python3 /work/example_apps/{case.someipy_app} "
            f"--interface_ip {IP_SOMEIPY} >/logs/app.log 2>&1"
        )
        # vsomeip needs a route covering the SD multicast group before its
        # Service Discovery will start (sd_wait_route).
        vsomeip_cmd = (
            "ip route add 224.0.0.0/4 dev eth0; "
            "sleep 3; "
            "VSOMEIP_CONFIGURATION=/cfg/vsomeip-client.json "
            f"/opt/integration_tests/install/{case.vsomeip_app}/{case.vsomeip_app} --udp "
            ">/logs/vsomeip.log 2>&1"
        )

        common = [
            "docker", "run", "-d", "--network", NETWORK,
            "-v", f"{cfg_dir}:/cfg", "-v", f"{logs_dir}:/logs",
            "--entrypoint", "bash",
        ]
        _run(common + ["--name", name_a, "--ip", IP_SOMEIPY,
                       "-v", f"{REPO_ROOT}:/work:ro", IMAGE, "-c", someipy_cmd])
        _run(common + ["--name", name_b, "--ip", IP_VSOMEIP,
                       "--cap-add", "NET_ADMIN", "--user", "root", IMAGE, "-c", vsomeip_cmd])

        time.sleep(duration)

        def read(fn):
            p = os.path.join(logs_dir, fn)
            if not os.path.exists(p):
                return []
            with open(p, errors="replace") as f:
                return f.read().splitlines()

        someipy_lines = read("app.log")
        daemon_lines = read("daemon.log")
        vsomeip_lines = read("vsomeip.log")

        if not keep:
            _run(["docker", "rm", "-f", name_a, name_b])

        ok = case.evaluate(someipy_lines, vsomeip_lines)
        if not ok:
            print(f"--- [{case.name}] someipy app ---")
            print("\n".join(someipy_lines[-40:]))
            print(f"--- [{case.name}] someipy daemon ---")
            print("\n".join(daemon_lines[-40:]))
            print(f"--- [{case.name}] vsomeip ---")
            print("\n".join(vsomeip_lines[-40:]))
        return ok


# ---- Registered test cases ---------------------------------------------------
# Each held fix adds its own case here (and its example app / vsomeip peer app).


def _eval_offer_method(someipy_lines, vsomeip_lines):
    # vsomeip calls a method offered by someipy and gets responses back.
    got_available = any("is available" in l for l in vsomeip_lines)
    responses = sum("Received a response from Service" in l for l in vsomeip_lines)
    received = sum("Received data:" in l for l in someipy_lines)
    print(f"  service available={got_available} responses={responses} handler_calls={received}")
    return got_available and responses > 0 and received > 0


def _eval_fire_and_forget(someipy_lines, vsomeip_lines):
    # vsomeip sends fire-and-forget (REQUEST_NO_RETURN) calls to a method
    # someipy offers. someipy's handler must be invoked for each; no response is
    # expected, so success is measured on the someipy side. Before the dispatch
    # fix the daemon dropped REQUEST_NO_RETURN and the handler never ran.
    got_available = any("is available" in l for l in vsomeip_lines)
    sent = sum("sent a fire-and-forget request" in l for l in vsomeip_lines)
    received = sum("Received data:" in l for l in someipy_lines)
    print(f"  service available={got_available} sent={sent} handler_calls={received}")
    return got_available and sent > 0 and received > 0


def _eval_discovery(someipy_lines, vsomeip_lines):
    # someipy (client) must discover a service a vsomeip peer offers over
    # multicast SD and call its method. This requires the daemon's multicast
    # receive socket to actually receive the peer's offers.
    called = sum("Sum:" in l for l in someipy_lines)
    print(f"  successful method results (implies discovery): {called}")
    return called > 0


def _eval_rediscovery(someipy_lines, vsomeip_lines):
    # The service is discovered initially, then its cached offer expires. The
    # later call succeeds only if the daemon actively re-resolves it via
    # FindService; otherwise it stays unresolved.
    initial = any("INITIAL_DISCOVERY_OK" in l for l in someipy_lines)
    rediscovered = any("REDISCOVERED Sum:" in l for l in someipy_lines)
    print(f"  initial_discovery={initial} rediscovered_after_expiry={rediscovered}")
    return initial and rediscovered


TESTS = [
    Case("offer_method_udp", "offer_method_udp.py", "offer_method_udp", _eval_offer_method),
    Case("fire_and_forget_udp", "offer_method_udp.py", "fire_and_forget_udp", _eval_fire_and_forget),
    # someipy (client) discovers a vsomeip-offered service on a NON-224 (239.x)
    # multicast SD group. The daemon only joins that group -- and thus receives
    # the offer -- if it treats the full class-D range as multicast; before the
    # fix a "224" prefix check sent 239.x down the broadcast path (no join), so
    # discovery never happened. vsomeip is the server (offers the service).
    Case("non224_discovery_udp", "call_method_udp.py", "call_method_udp",
         _eval_discovery,
         vsomeip_offers=[{"service": "0x1234", "instance": "0x5678",
                          "unreliable": "30509"}],
         sd_address="239.192.0.251"),
    # someipy (client) discovers a service, its cached offer expires (the vsomeip
    # peer stops re-offering: large cyclic_offer_delay), then a later call must
    # still succeed -- which requires the daemon to actively send a FindService
    # to re-resolve. Without that the service stays unresolved after expiry.
    Case("rediscover_after_expiry_udp", "rediscover_after_expiry_udp.py",
         "call_method_udp", _eval_rediscovery,
         vsomeip_offers=[{"service": "0x1234", "instance": "0x5678",
                          "unreliable": "30509"}],
         vsomeip_sd_overrides={"cyclic_offer_delay": "30000"},
         # Budget: ~2s app start + up to 12s initial discovery + 8s for the
         # cached offer to expire + several seconds to re-resolve (each find is
         # answered after vsomeip's request_response_delay). 26s races the
         # teardown against the answer; 34s leaves comfortable margin.
         duration=34),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tests", nargs="*", help="test names to run (default: all)")
    ap.add_argument("--duration", type=int, default=None,
                    help="override run duration (default: per-case)")
    ap.add_argument("--keep", action="store_true", help="keep containers after run")
    args = ap.parse_args()

    selected = [c for c in TESTS if not args.tests or c.name in args.tests]
    if not selected:
        print(f"No matching tests. Available: {[c.name for c in TESTS]}")
        return 1

    results = {}
    for case in selected:
        print(f"=== running {case.name} ===")
        duration = args.duration if args.duration is not None else case.duration
        results[case.name] = run_case(case, duration, args.keep)
        print(f"{case.name}: {'PASS' if results[case.name] else 'FAIL'}\n")

    print("Summary:")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
