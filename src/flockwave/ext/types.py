from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    TypeAlias,
    runtime_checkable,
)

if TYPE_CHECKING:
    from .manager import ExtensionAPIProxy

__all__ = (
    "Configuration",
    "Disposer",
    "EnabledState",
    "Enhancer",
    "ExtensionConfiguration",
    "ExtensionConfigurationSchema",
    "ExtensionConfigurationSpec",
    "PydanticModel",
)


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


@runtime_checkable
class PydanticModel(Protocol):
    """Protocol describing Pydantic model classes used as configuration
    schemas by extensions.
    """

    @classmethod
    def model_validate(cls, obj: Any) -> Any: ...

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]: ...


Disposer = Callable[[], None]
"""Type specification for disposer functions."""

ExtensionConfiguration: TypeAlias = dict[str, Any]
"""Type alias for the individual configuration objects of the extensions."""

ExtensionConfigurationSchema: TypeAlias = dict[str, Any]
"""Type alias for the configuration schema of an extension."""

ExtensionConfigurationSpec: TypeAlias = ExtensionConfigurationSchema | PydanticModel
"""Type alias for values that may be used to describe the configuration of an
extension.
"""

Configuration: TypeAlias = Mapping[str, ExtensionConfiguration]
"""Type alias for objects mapping extension names to their individual configuration
objects, represented as Python dictionaries.
"""


class Enhancer(Protocol):
    """Type specification for extension enhancer functions."""

    def __call__(
        self, api: ExtensionAPIProxy, provider: Any | None = None
    ) -> Disposer | AbstractContextManager[None] | None: ...
