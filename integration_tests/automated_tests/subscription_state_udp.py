from test_base import TestBase
import re


class TestSubscriptionStateUdp(TestBase):
    """Check that a SubscribeEventgroupAck from the offering side reaches the app.

    Reuses the receive_events_udp pair: the vsomeip app offers the service and
    answers someipy's SubscribeEventgroup with an Ack. The someipy app registers
    a subscription-state callback and prints the outcome, so the log tells us
    whether the Ack made it all the way from the SD socket, through the daemon,
    to the subscribing application.
    """

    def __init__(self, repository, ld_library_path=None, interface_ip="127.0.0.1"):
        super().__init__()

        self.ld_library_path = ld_library_path
        self.vsomeip_app = [
            f"{repository}/integration_tests/install/receive_events_udp/receive_events_udp"
        ]
        self.someipy_app = [
            "python3",
            f"{repository}/example_apps/receive_events_udp.py",
            f"--interface_ip",
            f"{interface_ip}",
        ]
        self.vsomeip_config = f"{repository}/integration_tests/install/receive_events_udp/vsomeip-client.json"

        self.someipydaemon_app = [
            "python3",
            f"{repository}/src/someipy/someipyd.py",
            "--config",
            f"{repository}/src/someipy/someipyd.json",
        ]

    def evaluate(self) -> bool:
        acknowledged = 0
        rejected = 0

        state_pattern = r"Subscription to eventgroup 0x([0-9a-fA-F]+) is (\w+)"
        for line in self.output_someipy_app:
            match = re.search(state_pattern, line)
            if match:
                if match.group(2) == "acknowledged":
                    acknowledged += 1
                elif match.group(2) == "rejected":
                    rejected += 1

        # The daemon must also have logged the ack it processed.
        daemon_acks = sum(
            1 for line in self.output_daemon if "Subscription acknowledged" in line
        )

        print(
            f"Reported acknowledged: {acknowledged}. Reported rejected: {rejected}. "
            f"Daemon acks: {daemon_acks}"
        )

        if rejected > 0:
            print("A subscription was rejected, which this test does not expect.")
            return False
        if daemon_acks == 0:
            print("The daemon never processed a SubscribeEventgroupAck.")
            return False
        if acknowledged == 0:
            print("The daemon saw an ack but the application was never told.")
            return False
        return True
