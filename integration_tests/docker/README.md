# Dockerized integration tests

Runs the someipy daemon and a vsomeip peer in two separate containers on a
private bridge network, so the two SOME/IP stacks communicate over real
multicast Service Discovery -- like two hosts on a network.

This avoids two problems that make the single-host `automated_tests` runner
awkward:

- Two independent SOME/IP stacks cannot cleanly share one loopback interface.
- vsomeip will not start Service Discovery until it detects a route covering the
  SD multicast group on its interface (`sd_wait_route`); the vsomeip container
  adds that route at start-up.

The image is self-contained: it builds vsomeip from source (an independent,
known-good peer) and compiles the C++ test-peer apps against it. someipy is not
baked in -- the working tree is mounted at run time, so whatever revision is
checked out is what gets tested.

## Build the image (once)

```bash
docker build -f integration_tests/docker/Dockerfile -t someipy-itest integration_tests/
```

Pin a different vsomeip release with `--build-arg VSOMEIP_VERSION=<tag>`.

## Run

```bash
# all registered tests
python3 integration_tests/docker/run.py

# a specific test, keeping containers for inspection
python3 integration_tests/docker/run.py offer_method_udp --keep
```

Requires Docker and permission to create a bridge network (no root needed inside
the tests; the vsomeip container is granted `NET_ADMIN` only to add its SD
route).

## Adding a test

Register a `Case` in `run.py` with the someipy example app, the vsomeip peer app
(built into the image from `integration_tests/<name>/`), and an `evaluate`
function over the two sides' captured stdout.
