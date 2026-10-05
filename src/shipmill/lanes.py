"""The release lanes. A module of its own so that every section parser can name a lane
without importing the policy that assembles them"""

from enum import StrEnum


class Lane(StrEnum):
    DEV = "dev"
    RC = "rc"
    STABLE = "stable"
    HOTFIX = "hotfix"

    @property
    def is_pre(self) -> bool:
        return self in (Lane.DEV, Lane.RC)
