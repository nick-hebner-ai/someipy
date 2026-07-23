from unittest.mock import MagicMock

from someipy._internal.someip_endpoint_storage import SomeipEndpointStorage


def test_accepts_more_than_two_endpoints_for_one_client():
    # A single client offering services on 3+ distinct ports needs an endpoint
    # per port. The previous limit of 2 silently rejected the third.
    storage = SomeipEndpointStorage()
    client_id = 1

    assert storage.add_endpoint(client_id, MagicMock()) is True
    assert storage.add_endpoint(client_id, MagicMock()) is True
    assert storage.add_endpoint(client_id, MagicMock()) is True

    assert len(storage.get_endpoints(client_id)) == 3


def test_no_fixed_cap_on_endpoints_per_client():
    # There is no artificial per-client cap: a client may offer on as many
    # distinct ports as it needs.
    storage = SomeipEndpointStorage()
    client_id = 1

    count = 100
    for _ in range(count):
        assert storage.add_endpoint(client_id, MagicMock()) is True

    assert len(storage.get_endpoints(client_id)) == count


def test_endpoints_tracked_independently_per_client():
    storage = SomeipEndpointStorage()

    for _ in range(50):
        storage.add_endpoint(1, MagicMock())
    storage.add_endpoint(2, MagicMock())

    assert len(storage.get_endpoints(1)) == 50
    assert len(storage.get_endpoints(2)) == 1
