# Copyright (C) 2025 Christian H.
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


from dataclasses import dataclass

from someipy._internal._sd.entries.sd_entry import SdEntry, SdEntryType


@dataclass
class FindServiceEntry(SdEntry):
    service_id: int
    instance_id: int
    major_version: int
    minor_version: int
    ttl: int

    def __init__(
        self,
        service_id: int,
        instance_id: int,
        major_version: int,
        minor_version: int,
        ttl: int = 0xFFFFFF,
    ):
        super().__init__(SdEntryType.FIND_SERVICE)
        self.service_id = service_id
        self.instance_id = instance_id
        self.major_version = major_version
        self.minor_version = minor_version
        # The TTL of a FindService entry states how long the query is valid.
        # It must be > 0: per the SOME/IP-SD spec a TTL of 0 marks a "stop"
        # entry, so spec-compliant peers (e.g. vsomeip) ignore a FindService
        # with TTL 0 and never answer it. Default to 0xFFFFFF ("until
        # stopped"), the same sentinel someipy uses elsewhere for TTLs.
        self.ttl = ttl
