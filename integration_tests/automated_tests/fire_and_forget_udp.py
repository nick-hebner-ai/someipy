from test_base import TestBase


class TestFireAndForgetUdp(TestBase):
    """A vsomeip client sends fire-and-forget (REQUEST_NO_RETURN) method calls
    to a service offered by someipy. The someipy method handler must be invoked
    for each call. No response is expected, so success is measured on the
    someipy side (the handler logs each received call) rather than on the
    vsomeip client side."""

    def __init__(self, repository, ld_library_path=None, interface_ip="127.0.0.1"):
        super().__init__()

        self.ld_library_path = ld_library_path
        self.vsomeip_app = [
            f"{repository}/integration_tests/install/fire_and_forget_udp/fire_and_forget_udp"
        ]
        self.someipy_app = [
            "python3",
            f"{repository}/example_apps/offer_method_udp.py",
            f"--interface_ip",
            f"{interface_ip}",
        ]
        self.vsomeip_config = f"{repository}/integration_tests/install/fire_and_forget_udp/vsomeip-client.json"

        self.someipydaemon_app = [
            "python3",
            f"{repository}/src/someipy/someipyd.py",
            "--config",
            f"{repository}/src/someipy/someipyd.json",
        ]

    def evaluate(self) -> bool:
        # The vsomeip client logs each fire-and-forget request it sends.
        requests_sent = 0
        for l in self.output_vsomeip_app:
            if "sent a fire-and-forget request to Service" in l:
                requests_sent += 1

        # The someipy method handler logs each call it receives. Without the
        # REQUEST_NO_RETURN dispatch fix, the daemon drops these and the handler
        # is never invoked, so received_calls stays at 0.
        received_calls = 0
        for l in self.output_someipy_app:
            if "Received data:" in l:
                received_calls += 1

        print(f"Fire-and-forget requests sent: {requests_sent}. Received by someipy: {received_calls}")

        if requests_sent == 0:
            print("vsomeip client never sent a request (service not discovered).")
            self.print_outputs()
            return False

        difference = requests_sent - received_calls
        tolerance = max(0.1 * requests_sent, 1)
        if abs(difference) <= tolerance and received_calls > 0:
            return True
        else:
            self.print_outputs()
            return False
