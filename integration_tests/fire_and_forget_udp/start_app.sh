#!/bin/bash

script_dir="$(dirname "$0")"

VSOMEIP_CONFIGURATION="${script_dir}/vsomeip-client.json" "${script_dir}/fire_and_forget_udp"
