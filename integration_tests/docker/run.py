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
import errno
import json
import os
import re
import subprocess
import sys
import tempfile
import time

IMAGE = "someipy-itest"
NETWORK = "someipy-itest-net"
SUBNET = "172.28.0.0/16"
IP_SOMEIPY = "172.28.0.2"  # someipy daemon + app
IP_VSOMEIP = "172.28.0.3"  # vsomeip peer
IP_APP = "172.28.0.4"  # someipy app in its own container (remote-app cases)
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


def someipyd_config(sd_address=SD_MULTICAST, use_tcp=False, tcp_host=IP_SOMEIPY) -> str:
    return (
        '{\n'
        f'    "sd_address": "{sd_address}",\n'
        f'    "sd_port": {SD_PORT},\n'
        '    "log_level": "DEBUG",\n'
        f'    "interface": "{IP_SOMEIPY}",\n'
        f'    "use_tcp": {str(use_tcp).lower()},\n'
        f'    "tcp_host": "{tcp_host}",\n'
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
    captured stdout of each side. The optional evaluate_daemon(daemon_lines) ->
    bool adds a check on the daemon log.

    With app_container=True the someipy app runs in its own container and
    reaches the daemon over its TCP control link, so the address the app
    offers is not local to it. app_args are extra command-line arguments for
    the example app.
    """

    def __init__(self, name, someipy_app, vsomeip_app, evaluate,
                 vsomeip_offers=None, sd_address=SD_MULTICAST,
                 vsomeip_sd_overrides=None, duration=16,
                 evaluate_daemon=None, app_container=False, app_args=""):
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
        self.evaluate_daemon = evaluate_daemon
        self.app_container = app_container
        self.app_args = app_args


def run_case(case: "Case", duration: int, keep: bool) -> bool:
    ensure_network()
    name_a = f"someipy-itest-{case.name}-a"
    name_b = f"someipy-itest-{case.name}-b"
    name_c = f"someipy-itest-{case.name}-c"
    _run(["docker", "rm", "-f", name_a, name_b, name_c])

    with tempfile.TemporaryDirectory() as workdir:
        cfg_dir = os.path.join(workdir, "cfg")
        logs_dir = os.path.join(workdir, "logs")
        os.makedirs(cfg_dir)
        os.makedirs(logs_dir)
        with open(os.path.join(cfg_dir, "someipyd.json"), "w") as f:
            f.write(someipyd_config(case.sd_address, use_tcp=case.app_container))
        with open(os.path.join(cfg_dir, "vsomeip-client.json"), "w") as f:
            f.write(vsomeip_config(case.vsomeip_offers, case.sd_address,
                                   case.vsomeip_sd_overrides))

        daemon_cmd = (
            "PYTHONPATH=/work/src python3 /work/src/someipy/someipyd.py "
            "--config /cfg/someipyd.json >/logs/daemon.log 2>&1"
        )
        app_cmd = (
            f"PYTHONPATH=/work/src python3 /work/example_apps/{case.someipy_app} "
        )
        if case.app_container:
            someipy_cmd = daemon_cmd
            app_cmd = (
                "sleep 4; "
                + app_cmd
                + f"--interface_ip {IP_APP} --offer_ip {IP_SOMEIPY} "
                + f"--daemon_tcp_host {IP_SOMEIPY} {case.app_args} >/logs/app.log 2>&1"
            )
        else:
            someipy_cmd = (
                daemon_cmd
                + " & sleep 2; "
                + app_cmd
                + f"--interface_ip {IP_SOMEIPY} {case.app_args} >/logs/app.log 2>&1"
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
        if case.app_container:
            _run(common + ["--name", name_c, "--ip", IP_APP,
                           "-v", f"{REPO_ROOT}:/work:ro", IMAGE, "-c", app_cmd])
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
            _run(["docker", "rm", "-f", name_a, name_b, name_c])

        ok = case.evaluate(someipy_lines, vsomeip_lines)
        if case.evaluate_daemon is not None:
            ok = case.evaluate_daemon(daemon_lines) and ok
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


def _raw_phase_counts(someipy_lines, vsomeip_lines):
    # Payloads the app reports sending per phase, and the notifications the peer
    # received per phase marker (RAWA/RAWB/RAWC, hex "52 41 57 41" ...).
    markers = {"A": "52 41 57 41", "B": "52 41 57 42", "C": "52 41 57 43"}
    sent = {phase: set() for phase in markers}
    for line in someipy_lines:
        m = re.search(r"raw sent phase=([ABC]) n=\d+ payload=(.+)$", line)
        if m:
            sent[m.group(1)].add(m.group(2).strip())
    received = {phase: [] for phase in markers}
    for line in vsomeip_lines:
        m = re.search(r"received a notification .*= \(\d+\) (.+)$", line)
        if not m:
            continue
        payload = m.group(1).strip()
        for phase, marker in markers.items():
            if payload.startswith(marker):
                received[phase].append(payload)
    return sent, received


def _eval_send_raw(someipy_lines, vsomeip_lines):
    # The app builds complete SOME/IP notification datagrams itself and sends
    # them with send_raw. The peer must receive them byte for byte while the
    # service is offered, none while it is withdrawn, and again after it is
    # offered again.
    got_available = any("is available" in l for l in vsomeip_lines)
    sent, received = _raw_phase_counts(someipy_lines, vsomeip_lines)
    verbatim = all(p in sent["A"] for p in received["A"])
    print(f"  service available={got_available} "
          f"A={len(received['A'])} B={len(received['B'])} C={len(received['C'])} "
          f"verbatim={verbatim}")
    return (got_available and len(received["A"]) > 0 and verbatim
            and len(received["B"]) == 0 and len(received["C"]) > 0)


def _eval_send_raw_remote_app(someipy_lines, vsomeip_lines):
    # Same as _eval_send_raw, with the app in another container: the offered
    # address does not exist there, so binding it locally must fail.
    failed = [l for l in someipy_lines
              if "local bind of offer address failed: errno=" in l]
    bind_failed = bool(failed) and failed[0].endswith(f"errno={errno.EADDRNOTAVAIL}")
    print(f"  local bind of offer address failed with EADDRNOTAVAIL={bind_failed}")
    return bind_failed and _eval_send_raw(someipy_lines, vsomeip_lines)


def _eval_send_raw_daemon(daemon_lines):
    # Between the stop-offer and the next offer, the daemon must receive raw
    # send requests (positive control) and send none of them to a subscriber.
    sent_re = re.compile(r"Sent raw payload \(\d+ bytes\) .* to (\d+) subscriber")
    withdrawn = False
    dropped = 0
    sent_while_withdrawn = 0
    for line in daemon_lines:
        if "Send stop offer message for service 0x1234" in line:
            withdrawn = True
        elif "Starting new offer timer" in line:
            withdrawn = False
        elif withdrawn and "not offered" in line and "raw payload" in line:
            dropped += 1
        elif withdrawn:
            m = sent_re.search(line)
            if m and int(m.group(1)) > 0:
                sent_while_withdrawn += 1
    print(f"  daemon: dropped_while_withdrawn={dropped} "
          f"sent_while_withdrawn={sent_while_withdrawn}")
    return dropped > 0 and sent_while_withdrawn == 0


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
    # The application builds whole SOME/IP datagrams itself and sends them with
    # send_raw from the offered endpoint: delivered verbatim while offered,
    # not while withdrawn, and again after re-offer.
    Case("send_raw_udp", "send_raw_udp.py", "send_events", _eval_send_raw,
         evaluate_daemon=_eval_send_raw_daemon, duration=24),
    # Same, with the application in a separate container that reaches the
    # daemon over TCP and offers the daemon container's address, which is not
    # local to the application.
    Case("send_raw_udp_remote_app", "send_raw_udp.py", "send_events",
         _eval_send_raw_remote_app, evaluate_daemon=_eval_send_raw_daemon,
         duration=26, app_container=True, app_args="--probe_local_bind"),
    # Control: the event subscription path the raw cases build on.
    Case("send_events_udp", "send_events_udp.py", "send_events",
         lambda someipy_lines, vsomeip_lines: any(
             "received a notification" in l for l in vsomeip_lines),
         duration=16),
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
