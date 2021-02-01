__all__ = ("ExtensionError", "ApplicationExit")


class ExtensionError(RuntimeError):
    """Superclass for errors related to the extension manager."""

    pass


class ApplicationExit(ExtensionError):
    """Special exception that can be thrown from the `load()` or `unload()`
    method of an extension to request the host application to exit.
    """

    pass
