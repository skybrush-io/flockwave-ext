from enum import Enum
from typing import Any


class EnabledState(Enum):
    """Possible values of a variable representing whether an extension is
    enabled.
    """

    UNKNOWN = "unknown"
    """Value representing the case when the extension is not known to the
    extension manager.
    """

    NO = "false"
    """Value representing the case when an extension is disabled explicitly by
    the user.
    """

    YES = "true"
    """Value representing the case when an extension is enabled explicitly by
    the user and we should show an error if we are not able to load it.
    """

    AUTO = "auto"
    """Value representing the case when the user did not express their explicit
    preference to enabling or disabling the extension, so the extension manager
    is free to decide whether it should be enabled or not.
    """

    @classmethod
    def from_object(cls, value: Any):
        if value is True:
            return cls.YES
        elif value is False:
            return cls.NO
        elif value is None:
            return cls.AUTO
        elif isinstance(value, str):
            return cls(value.lower().strip())
        elif isinstance(value, int):
            return cls.YES if value else cls.NO
        else:
            raise ValueError(f"cannot convert object to {cls.__name__}: {value!r}")

    @property
    def is_explicitly_enabled(self) -> bool:
        return self is EnabledState.YES
