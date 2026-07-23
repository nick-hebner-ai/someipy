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
    if NETWORK in existing:
        return
    result = _run(["docker", "network", "create", "--subnet", SUBNET, NETWORK])
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not create docker network {NETWORK} (subnet {SUBNET}); "
            f"another network with an overlapping subnet may already exist.\n"
            f"{result.stderr}"
        )


def someipyd_config() -> str:
    return (
        '{\n'
        f'    "sd_address": "{SD_MULTICAST}",\n'
        f'    "sd_port": {SD_PORT},\n'
        '    "log_level": "DEBUG",\n'
        f'    "interface": "{IP_SOMEIPY}",\n'
        '    "use_tcp": false,\n'
        f'    "tcp_host": "{IP_SOMEIPY}",\n'
        '    "tcp_port": 30500\n'
        '}\n'
    )


def vsomeip_config() -> str:
    return (
        '{\n'
        f'    "unicast": "{IP_VSOMEIP}",\n'
        '    "logging": {"level": "verbose", "console": "true",\n'
        '                "file": {"enable": "false"}, "dlt": "false"},\n'
        '    "applications": [{"name": "Hello", "id": "0x1313"}],\n'
        '    "routing": "Hello",\n'
        '    "service-discovery": {\n'
        '        "enable": "true",\n'
        f'        "multicast": "{SD_MULTICAST}",\n'
        f'        "port": "{SD_PORT}",\n'
        '        "protocol": "udp",\n'
        '        "initial_delay_min": "10", "initial_delay_max": "100",\n'
        '        "repetitions_base_delay": "200", "repetitions_max": "3",\n'
        '        "ttl": "3", "cyclic_offer_delay": "2000",\n'
        '        "request_response_delay": "1500"\n'
        '    }\n'
        '}\n'
    )


class Case:
    """A test case: a someipy example app paired with a vsomeip peer app.

    evaluate(someipy_lines, vsomeip_lines) -> bool decides pass/fail from the
    captured stdout of each side.
    """

    def __init__(self, name, someipy_app, vsomeip_app, evaluate):
        self.name = name
        self.someipy_app = someipy_app  # file under example_apps/
        self.vsomeip_app = vsomeip_app  # installed dir/binary name
        self.evaluate = evaluate


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
            f.write(someipyd_config())
        with open(os.path.join(cfg_dir, "vsomeip-client.json"), "w") as f:
            f.write(vsomeip_config())

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
            f"ip route add {SD_MULTICAST}/32 dev eth0 || ip route add 224.0.0.0/4 dev eth0; "
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


TESTS = [
    Case("offer_method_udp", "offer_method_udp.py", "offer_method_udp", _eval_offer_method),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tests", nargs="*", help="test names to run (default: all)")
    ap.add_argument("--duration", type=int, default=16)
    ap.add_argument("--keep", action="store_true", help="keep containers after run")
    args = ap.parse_args()

    selected = [c for c in TESTS if not args.tests or c.name in args.tests]
    if not selected:
        print(f"No matching tests. Available: {[c.name for c in TESTS]}")
        return 1

    results = {}
    for case in selected:
        print(f"=== running {case.name} ===")
        results[case.name] = run_case(case, args.duration, args.keep)
        print(f"{case.name}: {'PASS' if results[case.name] else 'FAIL'}\n")

    print("Summary:")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
