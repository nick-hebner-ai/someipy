from someipy._internal.session_handler import SessionHandler


def test_first_message_after_boot_has_reboot_flag_set():
    # AUTOSAR PRS_SOMEIPSD_00423: the very first SD message after (re)boot
    # carries the reboot flag set.
    handler = SessionHandler()

    session_id, reboot_flag = handler.update_session()

    assert session_id == 1
    assert reboot_flag is True


def test_reboot_flag_cleared_after_first_message():
    # Every message after the first must carry the reboot flag cleared, and it
    # must be cleared immediately -- not only once the session id wraps at
    # 0xFFFF (the bug this test guards against, where the flag stayed set for
    # up to 65535 messages).
    handler = SessionHandler()

    handler.update_session()  # first message, reboot flag set

    for expected_session_id in range(2, 12):
        session_id, reboot_flag = handler.update_session()
        assert session_id == expected_session_id
        assert reboot_flag is False


def test_reboot_flag_stays_cleared_across_session_id_wrap():
    # When the session id wraps it must go to 1 (never 0), and the reboot flag
    # must remain cleared -- the wrap is not a reboot.
    handler = SessionHandler(initial_value=0xFFFE)

    session_id, reboot_flag = handler.update_session()  # -> 0xFFFF, first msg
    assert session_id == 0xFFFF
    assert reboot_flag is True

    session_id, reboot_flag = handler.update_session()  # wraps
    assert session_id == 1
    assert reboot_flag is False

    session_id, reboot_flag = handler.update_session()
    assert session_id == 2
    assert reboot_flag is False


def test_two_handlers_clear_reboot_flag_independently_of_pacing():
    # Reproduces the daemon scenario: a fast unicast handler and a slow
    # multicast handler. Regardless of how many messages each has sent, both
    # report the reboot flag cleared after their own first message, so a strict
    # peer never sees a set-flag message follow a cleared-flag one.
    unicast = SessionHandler()
    multicast = SessionHandler()

    # Fast handler sends many messages.
    _, unicast_reboot_first = unicast.update_session()
    assert unicast_reboot_first is True
    for _ in range(1000):
        _, unicast_reboot = unicast.update_session()
        assert unicast_reboot is False

    # Slow handler sends its first message much later.
    _, multicast_reboot_first = multicast.update_session()
    assert multicast_reboot_first is True

    # Both handlers now agree: reboot flag cleared.
    _, unicast_reboot = unicast.update_session()
    _, multicast_reboot = multicast.update_session()
    assert unicast_reboot is False
    assert multicast_reboot is False
