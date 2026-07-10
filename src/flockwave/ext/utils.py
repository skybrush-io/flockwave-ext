from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from functools import partial as partial_
from functools import wraps
from inspect import Parameter, iscoroutinefunction, signature
from logging import Logger
from typing import (
    Any,
    Protocol,
    TypedDict,
    TypeVar,
    cast,
    overload,
    runtime_checkable,
)

from trio import CancelScope, Event

from .errors import InvalidConfigurationSchemaError
from .types import ExtensionConfigurationSchema, PydanticModel

__all__ = (
    "AwaitableCancelScope",
    "bind",
    "cancellable",
    "format_pydantic_validation_error",
    "get_json_schema_from_pydantic_model",
    "get_name_of_function",
    "normalize_configuration_schema",
    "keydefaultdict",
    "protected",
)


K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")
T2 = TypeVar("T2")


C = TypeVar("C", bound="AwaitableCancelScope")


class AwaitableCancelScope:
    """Wrapper for a Trio cancel scope that allows waiting until the cancellation
    has been processed.

    This object wraps a cancel scope and an event. Unlike in a Trio cancel
    scope, `cancel()` is an async operation that calls `cancel()` in the wrapped
    cancel scope and then waits for the event. The task that the cancel scope
    cancels is expected to trigger the event when it is about to terminate.
    """

    _wrapped_cancel_scope: CancelScope
    """The native Trio cancel scope wrapped by this instance."""

    _event: Event
    """The event that must be set by the associated task when the cancellation
    was processed.
    """

    _entered: bool
    """Whether the wrapped native Trio cancel scope was entered."""

    def __init__(self):
        self._entered = False
        self._wrapped_cancel_scope = CancelScope()
        self._event = Event()

    async def cancel(self) -> None:
        """Cancels the cancel scope and waits for the cancellation to be
        processed by the associated task.
        """
        self.cancel_nowait()
        if self._entered:
            await self._event.wait()

    def cancel_nowait(self) -> None:
        """Cancels the cancel scope and returns immediately, without waiting
        for the cancellation to be processed by the associated task.
        """
        self._wrapped_cancel_scope.cancel()

    def notify_processed(self) -> None:
        """Notifies the cancel scope that the cancellation has been processed.
        This is called automatically when the cancel scope is exited, but you
        may also call it manually if needed.
        """
        self._event.set()

    def __enter__(self: C) -> C:
        if self._entered:
            raise RuntimeError("AwaitableCancelScope may only be entered once")
        self._wrapped_cancel_scope.__enter__()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        try:
            return self._wrapped_cancel_scope.__exit__(exc_type, exc_value, tb)
        finally:
            self.notify_processed()


def bind(func, args: Sequence[Any] | None = None, kwds=None, *, partial=False):
    """Variant of `functools.partial()` that allows the argument list to
    be longer than the number of arguments accepted by the function if
    `partial` is set to `True`. If this is the case, the argument list
    will be truncated to the number of positional arguments accepted by
    the function.

    Parameters:
        args: the positional arguments to bind to the function
        kwds: the keyword arguments to bind to the function
    """
    if not args and not kwds:
        return func

    if args is None:
        args = ()

    if partial:
        num_args = 0
        for parameter in signature(func).parameters.values():
            if parameter.kind == Parameter.VAR_POSITIONAL:
                num_args = len(args)
                break
            elif parameter.kind in (Parameter.KEYWORD_ONLY, Parameter.VAR_KEYWORD):
                pass
            else:
                num_args += 1

        args = args[:num_args]

    if kwds is None:
        return partial_(func, *args)
    else:
        return partial_(func, *args, **kwds)


def cancellable(func):
    """Decorator that extends an async function with an extra `cancel_scope`
    keyword argument and makes the function enter the cancel scope.
    """

    @wraps(func)
    async def decorated(*args, cancel_scope: AwaitableCancelScope, **kwds):
        with cancel_scope:
            return await func(*args, **kwds)

    decorated._cancellable = True  # type: ignore

    return decorated


class ErrorDetails(TypedDict):
    """Typed dictionary describing how Pydantic ErrorDetails dicts look like,
    without actually depending on Pydantic.
    """

    ctx: Any
    input: Any
    loc: Sequence[str | int]
    msg: str
    type: str
    url: str


@runtime_checkable
class ValidationError(Protocol):
    """Protocol that specifies how Pydantic validation errors look like, without
    actually depending on Pydantic.
    """

    def errors(self) -> list[dict[str, Any]]: ...
    def error_count(self) -> int: ...


def format_pydantic_validation_error(err: Exception) -> str:
    """Formats a Pydantic validation error into a readable string."""
    if err.__class__.__name__ != "ValidationError" or not hasattr(err, "errors"):
        return str(err)

    errors = cast(Any, err).errors()
    formatted_errors = []
    for error in errors:
        loc_parts: list[str] = []
        last_is_field: bool = False
        for part in error.get("loc", []):
            if isinstance(part, int):
                loc_parts.append(f"[{part}]")
                last_is_field = False
            else:
                loc_parts.append(f".{part}" if last_is_field else part)
                last_is_field = True

        loc = "".join(loc_parts)
        msg = error.get("msg", "unknown error")
        formatted_errors.append(f"  - {loc}: {msg}")

    return "\n" + "\n".join(formatted_errors)


def get_name_of_function(func, *, recursion_limit: int = 5) -> str:
    """Retrieves the name of the given function if it provides a name, or
    returns a generic name otherwise.
    """
    if hasattr(func, "__name__"):
        return func.__name__
    elif isinstance(func, partial_) and recursion_limit > 0:
        return (
            "<partial function of "
            + get_name_of_function(func.func, recursion_limit=recursion_limit - 1)
            + ">"
        )
    else:
        return "<unknown function>"


def get_json_schema_from_pydantic_model(
    model: PydanticModel, *, extension_name: str
) -> ExtensionConfigurationSchema:
    """Generates a normalized JSON schema from a Pydantic model class.

    Args:
        model: the Pydantic model class to generate the schema from
        extension_name: the name of the extension that uses this configuration
            schema, used for error messages

    Raises:
        InvalidConfigurationSchemaError: if the schema cannot be generated from the
            Pydantic model
    """
    try:
        schema = model.model_json_schema()
    except Exception as ex:
        raise InvalidConfigurationSchemaError(
            "failed to generate configuration schema for extension "
            f"{extension_name!r}: {ex}"
        ) from ex

    return normalize_configuration_schema(schema)


def normalize_configuration_schema(schema: Any) -> ExtensionConfigurationSchema:
    """Normalizes a JSON schema describing an extension configuration.

    The schema must describe a JSON object. The returned schema is deep-copied,
    so callers may mutate it freely.

    Raises:
        InvalidConfigurationSchemaError: if the schema is not a dictionary or
            does not describe a JSON object
    """
    if not isinstance(schema, dict):
        raise InvalidConfigurationSchemaError(
            "configuration schema must be a dictionary"
        )

    if "type" in schema and schema["type"] != "object":
        raise InvalidConfigurationSchemaError(
            "configuration schema must describe a JSON object"
        )

    result = deepcopy(schema)
    result.setdefault("type", "object")
    return result


class keydefaultdict(defaultdict[K, V]):
    """defaultdict subclass that passes the key of the item being created
    to the default factory.
    """

    default_factory: Callable[[K], V] | None = None

    def __init__(self, factory: Callable[[K], V] | None = None):
        self.default_factory = factory

    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        else:
            ret = self[key] = self.default_factory(key)
            return ret


def nop(*args, **kwds) -> None:
    """Helper function that can be called with any number of arguments and
    does nothing.
    """
    pass


def _derive_real_handler(
    handler: Callable[[BaseException], Any] | Logger,
) -> Callable[[BaseException], Any]:
    if isinstance(handler, Logger):
        log = handler

        def log_exception(_: BaseException) -> None:
            log.exception("Unexpected exception caught")

        return log_exception
    else:
        return handler


@overload
def protected(
    handler: Logger,
) -> Callable[[Callable[..., T]], Callable[..., T | None]]: ...


@overload
def protected(
    handler: Callable[[BaseException], T2],
) -> Callable[[Callable[..., T]], Callable[..., T | T2]]: ...


@overload
def protected(
    handler: Callable[[BaseException], Awaitable[T2]],
) -> Callable[[Callable[..., T]], Callable[..., Awaitable[T | T2]]]: ...


def protected(handler: Callable[[BaseException], Any] | Logger) -> Any:
    """Decorator factory that creates a decorator that decorates a function and
    ensures that the exceptions do not propagate out from the function.

    When an exception is raised within the body of the function, it is forwarded
    to the given handler function, and the original function will return
    whatever the handler function returned. The handler may also be a logger, in
    which case the logger will be used to log the exception.

    Parameters:
        handler: the handler function to call when an exception happens in the
            decorated function, or a logger to log the exception to
    """
    real_handler = _derive_real_handler(handler)

    def decorator(func: Callable[..., T]) -> Callable[..., T | None]:
        if iscoroutinefunction(func):

            @wraps(func)
            async def decorated_async(*args, **kwds):
                try:
                    return await func(*args, **kwds)
                except Exception as ex:
                    if iscoroutinefunction(real_handler):
                        return await real_handler(ex)
                    else:
                        return real_handler(ex)

            return decorated_async  # type: ignore

        else:
            if iscoroutinefunction(handler):
                raise ValueError("cannot use async handler with sync function")

            @wraps(func)
            def decorated(*args, **kwds):
                try:
                    return func(*args, **kwds)
                except Exception as ex:
                    real_handler(ex)

            return decorated

    return decorator
