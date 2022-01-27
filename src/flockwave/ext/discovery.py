"""Functions and classes to help the automatic discovery of extensions
managed by an extension manager.
"""

from importlib import import_module
from pkgutil import get_loader
from types import ModuleType
from typing import Dict, Iterable, List

# This can be replaced with importlib.metadata when we drop support for
# Python <3.10 because the selectable interface was added in Python 3.10
from importlib_metadata import entry_points

from .errors import NoSuchExtension


__all__ = ("ExtensionModuleFinder",)


class ExtensionModuleFinder:
    """Class that helps the automatic discovery of extensions managed by
    an extension manager.
    """

    _entry_point_groups: List[str]
    """Names of PyPA entry point groups that can be used to discover
    extension modules.
    """

    _module_name_cache: Dict[str, str]
    """Cache that contains discovered module names for extensions."""

    _package_roots: List[str]
    """Names of Python packages that act as root namespaces for extension
    modules.
    """

    def __init__(self):
        """Constructor."""
        self._module_name_cache = {}
        self._entry_point_groups = []
        self._package_roots = []

    def add_entry_point_group(self, name: str) -> None:
        """Adds an entry point group to the list of entry point groups considered
        by this finder object.

        When the entry point group is already added, it is moved to the front
        of the priority list.
        """
        try:
            self._entry_point_groups.remove(name)
        except ValueError:
            pass
        self._entry_point_groups.append(name)
        self._module_name_cache.clear()

    def add_package_root(self, name: str) -> None:
        """Adds a namespace package to the list of namespace packages for
        extensions.

        When the namespace package is already added, it is moved to the front
        of the priority list.
        """
        try:
            self._package_roots.remove(name)
        except ValueError:
            pass
        self._package_roots.append(name)
        self._module_name_cache.clear()

    def exists(self, extension_name: str) -> bool:
        """Returns whether the extension with the given name exists.

        Parameters:
            extension_name: the name of the extension

        Returns:
            whether the extension exists
        """
        try:
            self.get_module_name_for_extension(extension_name)
            return True
        except NoSuchExtension:
            return False

    def get_module_for_extension(self, name: str) -> ModuleType:
        """Imports and returns the module that contains the given extension.

        Parameters:
            name: the name of the extension to look up

        Returns:
            the imported extension module

        Raises:
            ImportError: if an error happened while importing the extension
            NoSuchExtension: when the extension with the given name does not
                exist in the currently registered namespaces and entry point
                groups
        """
        return import_module(self.get_module_name_for_extension(name))

    def get_module_name_for_extension(self, name: str) -> str:
        """Returns the name of the module that contains the given extension.

        Parameters:
            name: the name of the extension to look up

        Returns:
            the full, dotted name of the module that contains the
            extension with the given name

        Raises:
            NoSuchExtension: when the extension with the given name does not
                exist in the currently registered namespaces and entry point
                groups
        """
        result = self._module_name_cache.get(name)
        if not result:
            for candidate in self._iter_module_name_candidates_for_extension(name):
                loader = get_loader(candidate)
                if loader is not None:
                    result = candidate
                    break
            else:
                raise NoSuchExtension(name)
            self._module_name_cache[name] = result
        return result

    def _iter_module_name_candidates_for_extension(self, name: str) -> Iterable[str]:
        """Iterator that yields possible module names for an extension name,
        given the currently registered namespace packages and entry point
        groups.
        """
        # We start with packages because those are faster to test; the first
        # call to entry_points() takes a bit more time. Also, this prevents
        # the user from overriding core extension modules by providing an
        # entry point with the same name

        for package_root in self._package_roots:
            yield f"{package_root}.{name}"

        for entry_point_group in self._entry_point_groups:
            for entry_point in entry_points(group=entry_point_group):
                yield entry_point.value
