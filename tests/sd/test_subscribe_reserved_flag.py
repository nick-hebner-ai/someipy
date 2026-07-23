import ipaddress

from someipy._internal._sd.sd_message import SdMessage
from someipy._internal._sd.entries.subscribe_eventgroup_entry import (
    SubscribeEventGroupEntry,
)
from someipy._internal._sd.options.endpoint import IpV4EndpointOption
from someipy._internal._sd.deserialization.sd_serialization import serialize_sd_message
from someipy._internal.someip_sd_header import SdEntry, SdEntryType, SdEventGroupEntry
from someipy._internal.transport_layer_protocol import TransportLayerProtocol


# In a SubscribeEventgroup entry, bit 7 of the reserved/counter byte is the
# deprecated "initial data requested" flag. Strict SD stacks require that
# reserved field to be zero and discard entries that leave it set. someipy
# previously hardcoded that bit to 1 in both serializers.


def _subscribe_message(counter):
    message = SdMessage()
    message.multicast = True
    message.session_id = 0x1234
    message.entries.append(
        SubscribeEventGroupEntry(
            service_id=0x0001,
            instance_id=0x0002,
            major_version=0x03,
            ttl=0x10,
            eventgroup_id=0x0003,
            counter=counter,
            ip_v4_endpoints=[
                IpV4EndpointOption(
                    address=ipaddress.IPv4Address("192.168.0.1"),
                    protocol=TransportLayerProtocol.UDP,
                    port=0x2328,
                )
            ],
            ip_v6_endpoints=[],
        )
    )
    return serialize_sd_message(message)


# The single SD entry starts at offset 24 (16-byte SOME/IP header + 4-byte SD
# flags + 4-byte length-of-entries). Within a 16-byte eventgroup entry the
# reserved/flags-and-counter byte is at offset 13, i.e. absolute offset 37.
_RESERVED_FLAGS_OFFSET = 37


def test_sd_serializer_leaves_reserved_field_zero():
    data = _subscribe_message(counter=0)

    # Whole-message check: the reserved/flags byte (offset 37) is 0x00.
    expected = bytes.fromhex(
        "ffff8100000000300000123401010200400000000000001006000010"
        "0001000203000010000000030000000c00090400c0a8000100112328"
    )
    assert data == expected
    assert data[_RESERVED_FLAGS_OFFSET] == 0x00


def test_sd_serializer_preserves_counter_without_reserved_bit():
    data = _subscribe_message(counter=5)

    # Counter is kept in the low nibble; bit 7 (0x80) stays clear.
    assert data[_RESERVED_FLAGS_OFFSET] == 0x05
    assert data[_RESERVED_FLAGS_OFFSET] & 0x80 == 0


def _header_eventgroup_entry(flag, counter):
    sd_entry = SdEntry(
        type=SdEntryType.SUBSCRIBE_EVENT_GROUP,
        index_first_option=0,
        index_second_option=0,
        num_options_1=1,
        num_options_2=0,
        service_id=0x01,
        instance_id=0x02,
        major_version=0x03,
        ttl=0x10,
    )
    return SdEventGroupEntry(
        sd_entry=sd_entry,
        initial_data_requested_flag=flag,
        counter=counter,
        eventgroup_id=0x0003,
    )


def test_header_entry_honors_initial_data_requested_flag():
    # The flags/counter byte is the third-from-last byte (layout: reserved 0x00,
    # flags+counter, eventgroup id high, eventgroup id low).
    cleared = _header_eventgroup_entry(flag=False, counter=5).to_buffer()
    assert cleared[-3] == 0x05  # bit 7 clear, counter preserved

    set_flag = _header_eventgroup_entry(flag=True, counter=5).to_buffer()
    assert set_flag[-3] == 0x85  # bit 7 set only when the flag is set
