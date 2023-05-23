__all__ = (
    "ExtensionError",
    "ApplicationExit",
    "ApplicationRestart",
    "NoSuchExtension",
    "NotLoadableError",
    "NotSupportedError",
)


class ExtensionError(RuntimeError):
    """Superclass for errors related to the extension manager."""

    pass


class ApplicationExit(ExtensionError):
    """Special exception that can be thrown from the `load()` or `unload()`
    method of an extension to request the host application to exit.
    """

    pass


class ApplicationRestart(ExtensionError):
    """Special exception that can be thrown from the `load()`, `unload()` or
    `run()` methods of an extension to request the host application to
    restart itself.
    """

    pass


class NoSuchExtension(ExtensionError):
    """Error thrown by the extension module finder when there is no extension
    module for a given extension name.
    """

    pass


class NotLoadableError(ExtensionError):
    """Special exception that can be thrown from the `load()` or `run()`
    method of an extension to indicate that the extension cannot be loaded at
    this time (probably due to a missing license, permission or something
    similar). This exception will be formatted nicely"""

    pass


class NotSupportedError(ExtensionError):
    """Special exception that can be thrown from the `unload()` method of an
    extension to indicate that the extension is essential and cannot be unloaded.
    """

    pass
