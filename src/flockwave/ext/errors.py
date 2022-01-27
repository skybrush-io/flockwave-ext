__all__ = ("ExtensionError", "ApplicationExit", "NoSuchExtension")


class ExtensionError(RuntimeError):
    """Superclass for errors related to the extension manager."""

    pass


class ApplicationExit(ExtensionError):
    """Special exception that can be thrown from the `load()` or `unload()`
    method of an extension to request the host application to exit.
    """

    pass


class NoSuchExtension(ExtensionError):
    """Error thrown by the extension module finder when there is no extension
    module for a given extension name.
    """

    pass
