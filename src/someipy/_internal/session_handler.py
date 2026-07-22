# Copyright (C) 2024 Christian H.
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from typing import Tuple

class SessionHandler:
    session_id: int
    reboot_flag: bool

    def __init__(self, initial_value=0):
        self.session_id = initial_value
        self.reboot_flag = True

    def update_session(self) -> Tuple[int, bool]:
        self.session_id += 1
        if self.session_id > 0xFFFF:
            # The session id must never be 0, so it wraps to 1 rather than 0.
            self.session_id = 1

        # Per AUTOSAR PRS_SOMEIPSD_00423 the reboot flag is set only on the
        # first SD message after (re)boot and cleared on every message after
        # that. Clear it after the first update_session() call instead of
        # waiting for the session id to wrap at 0xFFFF: otherwise the flag
        # stays set for up to 65535 messages, and two independently paced
        # handlers (e.g. a fast unicast handler and a slow multicast handler)
        # clear it at very different times. A strict SD peer then sees a
        # cleared-flag message followed by a still-set-flag message from the
        # same source and reports a spurious reboot.
        current_reboot_flag = self.reboot_flag
        self.reboot_flag = False

        return self.session_id, current_reboot_flag