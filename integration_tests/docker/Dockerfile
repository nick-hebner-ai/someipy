# Self-contained image for the someipy integration tests.
#
# It builds vsomeip from source to act as an independent, known-good SOME/IP
# peer, and compiles the C++ test-peer applications against it. someipy itself
# is NOT baked in -- the fork's source tree is mounted at run time, so the image
# stays independent of any particular someipy revision.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        libboost-system-dev \
        libboost-thread-dev \
        libboost-log-dev \
        libboost-filesystem-dev \
        python3 \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Build and install vsomeip from source (pinned for reproducibility).
ARG VSOMEIP_VERSION=3.4.10
RUN git clone --depth 1 --branch ${VSOMEIP_VERSION} \
        https://github.com/COVESA/vsomeip.git /tmp/vsomeip \
    && cmake -S /tmp/vsomeip -B /tmp/vsomeip/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/vsomeip/build -j"$(nproc)" --target install \
    && ldconfig \
    && rm -rf /tmp/vsomeip

# Compile the C++ test-peer applications against the installed vsomeip.
COPY . /opt/integration_tests
RUN cmake -S /opt/integration_tests -B /opt/integration_tests/build \
    && cmake --build /opt/integration_tests/build -j"$(nproc)" --target install

WORKDIR /opt/integration_tests
