from __future__ import annotations

from contextlib import AbstractContextManager
from enum import Enum
from typing import (
    Any,
    Callable,
    Optional,
    Protocol,
    TYPE_CHECKING,
    Union,
)

if TYPE_CHECKING:
    from .manager import ExtensionAPIProxy

__all__ = ("Disposer", "EnabledState", "Enhancer")


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

    PREFER = "prefer"
    """Value representing the case when the user did not express their explicit
    preference to enabling or disabling the extension, and the extension manager
    should prefer to enable it if possible.
    """

    AVOID = "avoid"
    """Value representing the case when the user did not express their explicit
    preference to enabling or disabling the extension, and the extension manager
    should avoid enabling it unless other extensions need it.
    """

    @classmethod
    def from_object(cls, value: Any):
        if value is True:
            return cls.YES
        elif value is False:
            return cls.NO
        elif value is None:
            return cls.PREFER
        elif isinstance(value, cls):
            return value
        elif isinstance(value, str):
            return cls(value.lower().strip())
        elif isinstance(value, int):
            return cls.YES if value else cls.NO
        else:
            raise ValueError(f"cannot convert object to {cls.__name__}: {value!r}")

    @property
    def is_explicitly_enabled(self) -> bool:
        return self is EnabledState.YES


Disposer = Callable[[], None]
"""Type specification for disposer functions."""


class Enhancer(Protocol):
    """Type specification for extension enhancer functions."""

    def __call__(
        self, api: ExtensionAPIProxy, provider: Optional[Any] = None
    ) -> Optional[Union[Disposer, AbstractContextManager[None]]]: ...
