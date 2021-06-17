from functools import partial as partial_, wraps
from inspect import iscoroutinefunction, Parameter, signature
from logging import Logger
from typing import (
    Any,
    Awaitable,
    Callable,
    DefaultDict,
    Optional,
    TypeVar,
    Union,
    overload,
)

from trio import MultiError

__all__ = ("bind", "cancellable", "keydefaultdict", "protected")


K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")
T2 = TypeVar("T2")


def bind(func, args=None, kwds=None, *, partial=False):
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
    async def decorated(*args, cancel_scope, **kwds):
        with cancel_scope:
            return await func(*args, **kwds)

    decorated._cancellable = True

    return decorated


class keydefaultdict(DefaultDict[K, V]):
    """defaultdict subclass that passes the key of the item being created
    to the default factory.
    """

    default_factory: Optional[Callable[[K], V]] = None

    def __init__(self, factory: Optional[Callable[[K], V]] = None):
        self.default_factory = factory

    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        else:
            ret = self[key] = self.default_factory(key)
            return ret


def _multierror_has_base_exception(multi_ex: MultiError) -> bool:
    for ex in multi_ex.exceptions:
        if isinstance(ex, MultiError) and _multierror_has_base_exception(ex):
            return True
        if not isinstance(ex, Exception):
            return True
    return False


@overload
def protected(
    handler: Logger,
) -> Callable[[Callable[..., T]], Callable[..., Optional[T]]]:
    ...


@overload
def protected(
    handler: Callable[[BaseException], T2]
) -> Callable[[Callable[..., T]], Callable[..., Union[T, T2]]]:
    ...


@overload
def protected(
    handler: Callable[[BaseException], Awaitable[T2]]
) -> Callable[[Callable[..., T]], Callable[..., Awaitable[Union[T, T2]]]]:
    ...


def protected(handler):
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

    real_handler: Callable[[BaseException], Any]

    if isinstance(handler, Logger):
        log = handler

        def log_exception(_: BaseException) -> Any:
            log.exception("Unexpected exception caught")

        real_handler = log_exception
    else:
        real_handler = handler

    def decorator(func: Callable[..., T]) -> Callable[..., Optional[T]]:
        if iscoroutinefunction(func):

            @wraps(func)
            async def decorated_async(*args, **kwds):
                try:
                    return await func(*args, **kwds)  # type: ignore
                except MultiError as multi_ex:
                    # If there is at least one BaseException in the MultiError,
                    # re-raise the entire MultiError. This is needed to allow
                    # Trio to handle Cancelled exceptions properly
                    if _multierror_has_base_exception(multi_ex):
                        raise
                    else:
                        if iscoroutinefunction(real_handler):
                            return await real_handler(multi_ex)
                        else:
                            return real_handler(multi_ex)
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
