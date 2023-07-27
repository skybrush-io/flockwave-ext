"""Base class for extensions."""

from contextlib import asynccontextmanager
from logging import Logger
from trio import Lock, Nursery, open_nursery, WouldBlock
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    Optional,
    TypeVar,
)
from warnings import warn

from .utils import get_name_of_function

__all__ = ("ExtensionBase",)


Configuration = dict[str, Any]


TApp = TypeVar("TApp")


class ExtensionBase(Generic[TApp]):
    """Interface specification for Flockwave extensions."""

    _app: Optional[TApp] = None
    """The application hosting the extension."""

    _nursery: Optional[Nursery] = None
    """A nursery that the extension may use to spawn subtasks in."""

    _nursery_lock: Lock
    """Lock to prevent concurrent access to the ``_nursery`` property."""

    log: Optional[Logger] = None
    """Logger that the extension may use to write log messages to."""

    name: str = ""
    """Name of the extension; assigned by the extension manager when the
    extension is constructed.
    """

    def __init__(self):
        """Constructor."""
        self._nursery_lock = Lock()

    @property
    def app(self) -> Optional[TApp]:
        """The application that the extension is attached to."""
        return self._app

    @app.setter
    def app(self, value: Optional[TApp]) -> None:
        old_value = self._app
        self._app = value
        self.on_app_changed(old_value, self._app)

    def configure(self, configuration: Configuration) -> None:
        """Configures the extension with the given configuration object.

        This method is called only once from :meth:`load()`_ during the
        initialization of the extension.

        The default implementation of this method is empty. There is no
        need to call the superclass when you override it.
        """
        pass

    def load(self, app: TApp, configuration: Configuration, logger: Logger) -> None:
        """Handler that is called by the extension manager when the
        extension is loaded into the application.

        Typically, you don't need to override this method; override
        :meth:`configure()` instead.

        Arguments:
            app: the application
            configuration: the extension-specific configuration dictionary of
                the application
            logger: a logger object that the extension may use to write to the
                application log
        """
        self.app = app
        self.log = logger
        self.configure(configuration)

    def on_app_changed(self, old_app: Optional[TApp], new_app: Optional[TApp]) -> None:
        """Handler that is called when the extension is associated to an
        application or detached from an application.

        Arguments:
            old_app: the old application
            new_app: the new application
        """
        pass

    def run_in_background(
        self, func: Callable[..., Awaitable[Any]], *args, protect: bool = True
    ) -> None:
        warn(
            "run_in_background() is deprecated, use start_in_background_soon() instead",
            DeprecationWarning,
            stacklevel=1,
        )
        return self.start_in_background_soon(func, *args, protect=protect)

    async def start_in_background(
        self, func: Callable[..., Awaitable[Any]], *args, protect: bool = True
    ) -> Any:
        """Schedules the given function to be executed in the background in the
        context of this extension, waiting for the function to start up. The
        function is automatically stopped when the extension is unloaded.

        This function requires the extension-specific nursery to be open; use
        the `use_nursery()` context manager to open the nursery.

        Parameters:
            protect: whether to protect the nursery that the function is running
                in from closing when the function raises an exception
        """
        self._ensure_nursery()
        assert self._nursery is not None

        if protect:
            return await self._nursery.start(
                self._run_protected_with_task_status, func, *args
            )
        else:
            return await self._nursery.start(func, *args)

    def start_in_background_soon(
        self, func: Callable[..., Awaitable[Any]], *args, protect: bool = True
    ) -> None:
        """Schedules the given function to be executed in the background in the
        context of this extension. The function is automatically stopped when
        the extension is unloaded.

        This function requires the extension-specific nursery to be open; use
        the `use_nursery()` context manager to open the nursery.

        Parameters:
            protect: whether to protect the nursery that the function is running
                in from closing when the function raises an exception
        """
        self._ensure_nursery()
        assert self._nursery is not None

        if protect:
            self._nursery.start_soon(self._run_protected, func, *args)
        else:
            self._nursery.start_soon(func, *args)

    def spindown(self) -> None:
        """Handler that is called by the extension manager when the
        last client disconnects from the server.

        The default implementation of this method is empty. There is no
        need to call the superclass when you override it.
        """
        pass

    def spinup(self) -> None:
        """Handler that is called by the extension manager when the
        first client connects to the server.

        The default implementation of this method is empty. There is no
        need to call the superclass when you override it.
        """
        pass

    def teardown(self) -> None:
        """Tears down the extension and prepares it for unloading.

        This method is called only once from `unload()`_ during the
        unloading of the extension.

        The default implementation of this method is empty. There is no
        need to call the superclass when you override it.
        """
        pass

    def unload(self, app: TApp) -> None:
        """Handler that is called by the extension manager when the
        extension is unloaded.

        Typically, you don't need to override this method; override
        `teardown()` instead.

        Arguments:
            app: the application; provided for sake of API compatibility with
                simple classless extensions where the module provides a single
                `unload()` function
        """
        self.teardown()
        self.log = None
        self.app = None

    @asynccontextmanager
    async def use_nursery(self) -> AsyncIterator[Nursery]:
        """Async context manager that opens a private, extension-specific nursery
        that the extension can use to run background tasks in.
        """
        if self._nursery_lock is None:
            self._nursery_lock = Lock()

        try:
            self._nursery_lock.acquire_nowait()
        except WouldBlock:
            raise RuntimeError("The nursery of the extension is already open") from None

        try:
            async with open_nursery() as nursery:
                self._nursery = nursery
                yield self._nursery
        finally:
            self._nursery = None
            self._nursery_lock.release()

    def _ensure_nursery(self) -> None:
        """Ensures that the extension has a nursery assigned to it that can be
        used to run background tasks.

        Raises:
            RuntimeError: if the extension has not been assigned a nursery yet
        """
        if not self._nursery:
            raise RuntimeError(
                "Cannot run task in background, the extension has not started "
                + "serving background tasks yet. Did you forget to call "
                + "use_nursery()?"
            )

    async def _run_protected(self, func: Callable[..., Awaitable[Any]], *args):
        """Runs the given function in a "protected" mode that prevents exceptions
        emitted from it to crash the nursery that the function is being executed
        in.
        """
        try:
            return await func(*args)
        except Exception:
            if self.log:
                self.log.exception(
                    "Unexpected exception caught from background task "
                    + get_name_of_function(func)
                )

    async def _run_protected_with_task_status(
        self, func: Callable[..., Awaitable[Any]], *args, task_status
    ):
        """Runs the given function in a "protected" mode that prevents exceptions
        emitted from it to crash the nursery that the function is being executed
        in.
        """
        try:
            return await func(*args, task_status=task_status)
        except Exception:
            if self.log:
                self.log.exception(
                    "Unexpected exception caught from background task "
                    + get_name_of_function(func)
                )
