"""Extension manager class for Flockwave."""

from __future__ import annotations

from blinker import Signal
from contextlib import AbstractContextManager, contextmanager
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from importlib_metadata import version as get_version_from_metadata
from inspect import iscoroutinefunction, signature
from logging import Logger, getLogger
from semver import Version
from trio import open_memory_channel, open_nursery, TASK_STATUS_IGNORED
from trio.abc import SendChannel
from types import ModuleType
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Iterable,
    Iterator,
    Optional,
    TypeVar,
    Union,
)

from .base import Configuration, ExtensionBase, TApp
from .discovery import ExtensionModuleFinder
from .errors import (
    ApplicationExit,
    ApplicationRestart,
    NoSuchExtension,
    NotLoadableError,
    NotSupportedError,
)
from .types import Disposer, EnabledState, Enhancer
from .utils import (
    AwaitableCancelScope,
    bind,
    cancellable,
    keydefaultdict,
    nop,
    protected,
)

__all__ = ("ExtensionManager",)

EXT_PACKAGE_NAME = __name__.rpartition(".")[0]
base_log = getLogger(__name__)

T = TypeVar("T")

#: Type alias for the configuration objects of the extensions
ExtensionConfiguration = dict[str, Any]

#: Type alias for the configuration schema of an extension
ExtensionConfigurationSchema = dict[str, Any]


@dataclass
class Node(Generic[T]):
    """Single node in a linked list, used by MRUContainer_."""

    next: "Node[T]" = field(init=False)
    prev: "Node[T]" = field(init=False)
    data: Optional[T] = None

    def __post_init__(self):
        self.next = self.prev = self


class MRUContainer(Generic[T]):
    """Container for arbitrary objects that records the order in which the
    items were added, moving recently added items to the end of the container.

    Whenever a new item is added to the container, the new item is added to
    the end of the container. If the item is already in the container, it is
    moved to the end of the container instead of adding another instance.
    """

    _guard: Node[T]
    """Guard node at the front of the linked list."""

    _tail: Node[T]
    """The tail of the linked list for backward traversal."""

    _dict: dict[T, Node[T]]
    """Dictionary mapping objects to the corresponding linked list nodes."""

    def __init__(self):
        """Constructor."""
        self._guard = self._tail = Node()
        self._dict = {}

    def __contains__(self, item: T) -> bool:
        """Returns whether the given item is in the container."""
        return item in self._dict

    def items(self) -> Iterable[T]:
        """Iterator that yields the items of the container in the order they
        were added.
        """
        node = self._guard
        node = node.next
        while node is not self._guard:
            node, data = node.next, node.data
            assert data is not None
            yield data

    def append(self, item: T) -> None:
        """Adds the given object to the end of the container.

        If the object is already in the container, it is moved to the end of
        the container.
        """
        assert item is not None

        node = self._dict.get(item)
        if not node:
            node = self._dict[item] = Node(item)
        else:
            self._unlink_node(node)

        node.prev = self._tail
        node.next = self._guard

        self._tail.next = node
        self._tail = node

    def remove(self, item: T) -> None:
        """Removes the given object from the container."""
        node = self._dict.pop(item, None)
        if node:
            return self._unlink_node(node)

    def reversed(self) -> Iterable[T]:
        """Iterator that yields the items of the container in reverse order."""
        node = self._tail
        while node is not self._guard:
            node, data = node.prev, node.data
            assert data is not None
            yield data

    def _unlink_node(self, node: Node[T]) -> None:
        if self._tail is node:
            self._tail = node.prev

        node.prev.next = node.next
        node.next.prev = node.prev

        node.prev = node
        node.next = node


@dataclass
class ExtensionData:
    """Data that the extension manager stores related to each extension.

    Do not instantiate this object directly; use the ``for_extension()``
    class method instead.
    """

    name: str
    """The name of the extension."""

    api_proxy: Optional[ExtensionAPIProxy] = None
    """The API object that the extension exports."""

    configuration: ExtensionConfiguration = field(default_factory=dict)
    """The current configuration object of the extension."""

    dependents: set[str] = field(default_factory=set)
    """Names of other loaded extensions that depend on this extension."""

    enhances: dict[str, Enhancement] = field(default_factory=dict)
    """List of enhancement objects in which this extension is the provider.

    This list is the primary owner of Enhancement_ objects. When an enhancement
    is removed from this list, it must also be removed from the corresponding
    ``enhanced_by`` property in the target extension.
    """

    enhanced_by: dict[str, Enhancement] = field(default_factory=dict)
    """List of enhancement objects in which this extension is the target.

    See the ``enhances`` property for ownership rules.
    """

    instance: Optional[object] = None
    """The loaded instance of the extension; ``None`` if the extension is
    not loaded.
    """

    loaded: bool = False
    """Whether the extension is loaded."""

    log: Optional[Logger] = None
    """The logger associated to the extension."""

    next_configuration: Optional[ExtensionConfiguration] = None
    """The next configuration object of the extension that will be
    activated when the extension is loaded the next time.
    """

    requested_host_app_restart: bool = False
    """Whether the extension has requested the host application to restart
    itself.
    """

    task: Optional[AwaitableCancelScope] = None
    """A cancellation scope for the background task that was spawned for the
    extension when it was loaded, or `None` if no such task was spawned.
    """

    worker: Optional[AwaitableCancelScope] = None
    """A cancellation scope for the worker task that was spawned for the
    extension when the first client connected to the server, or ``None`` if no
    such worker was spawned or there are no clients connected.
    """

    @classmethod
    def for_extension(cls, name: str) -> ExtensionData:
        return cls(name=name, log=base_log.getChild(name))

    def commit_configuration_changes(self) -> ExtensionConfiguration:
        """Moves the "next configuration" of the extension into the
        configuration field. This function should be called before an extension
        is loaded and after the extension is unloaded to commit any changes that
        the user has made to the configuration while it was loaded.

        Returns:
            the updated configuration of the extension
        """
        if self.next_configuration is not None:
            self.configuration = self.next_configuration
            self.next_configuration = None
        return self.configuration

    def configure(self, configuration: ExtensionConfiguration) -> None:
        """Configures the extension with a new configuration object.

        If the extension is not loaded, the new configuration takes effect
        immediately.

        If the extension is loaded, the new configuration is saved and will
        take effect the next time the extension is reloaded.

        In both cases, the configuration object passed here is deep-copied. You
        are safe to make changes to the configuration object without affecting
        the extension.
        """
        self.next_configuration = deepcopy(configuration)
        if not self.loaded:
            self.commit_configuration_changes()

    @property
    def needs_restart(self) -> bool:
        """Returns whether the extension needs a restart to activate its next
        configuration.
        """
        return self.next_configuration is not None

    def _notify_enhanced_by(
        self, extension_name: str, enhancement: Enhancement
    ) -> None:
        self.enhanced_by[extension_name] = enhancement


@dataclass
class Enhancement:
    """Enhancement link between two extensions.

    Enhancements allow one extension to extend the functionality of another
    extension by using the API of the latter, but _only_ when both extensions
    are loaded. This is essentially a "soft dependency" between the two
    extensions such that they can be loaded and unloaded freely but they can
    still work together when both are loaded.

    The two extensions involved in an enhancement are called _provider_ and
    _target_. The enhancement is declared in the metadata of the provider. The
    target is not aware of the enhancement until the provider is loaded.
    """

    enhancer: Enhancer
    """Function that will be called when both extensions are loaded."""

    provider: str = ""
    """Name of the extension that _provides_ the enhancement to another
    extension.
    """

    target: str = ""
    """Name of the extension that the enhancement _targets_."""

    disposer: Optional[Disposer] = None
    """Disposer function to call when any of the two extensions involved are
    unloaded. ``None`` means that the enhancement is not active yet because
    at least one of the extensions involved is not loaded.
    """

    @property
    def active(self) -> bool:
        """Returns whether the enhancement is active. The enhancement is active
        if the enhancer function has already been called and we received a
        disposer function to call when any of them are unloaded.
        """
        return self.disposer is not None

    def other(self, extension_name: str) -> str:
        """Returns the _other_ endpoint of the extension that is not equal to
        the given extension name.
        """
        return self.provider if extension_name != self.provider else self.target

    def activate(self, api: ExtensionAPIProxy, provider: Any) -> None:
        """Activates the enhancement by calling its activation function with
        the API of the target extension and the provider extension instance.
        If the activation function has one argument only, it is called with the
        API of the target extension only for sake of backwards compatibility.
        """
        if self.active:
            raise RuntimeError("enhancement is already activated")

        sig = signature(self.enhancer)
        if len(sig.parameters) >= 2:
            result = self.enhancer(api, provider)
        else:
            result = self.enhancer(api)  # type: ignore

        if result is None:
            disposer = nop
        elif isinstance(result, AbstractContextManager):
            result.__enter__()

            def disposer():
                return result.__exit__(None, None, None)

        else:
            disposer = result

        self.disposer = disposer  # type: ignore

    def deactivate(self) -> None:
        if not self.active:
            raise RuntimeError("enhancement is already inactive")

        assert self.disposer is not None

        disposer, self.disposer = self.disposer, None
        disposer()


class ExtensionManager(Generic[TApp]):
    """Central extension manager for a Flockwave application that manages
    the loading, configuration and unloading of extensions.
    """

    loaded = Signal()
    """Signal that is sent by the extension manager when an extension has been
    configured and loaded. The signal has two keyword arguments: ``name`` and
    ``extension``.
    """

    unloaded = Signal()
    """Signal that is sent by the extension manager when an extension has been
    unloaded. The signal has two keyword arguments: ``name`` and ``extension``.
    """

    restart_requested = Signal()
    """Signal that is sent by the extension manager when an extension requests the
    host application to reload itself. The signal has one keyword argument:
    ``name``, containing the name of the extension.
    """

    module_finder: ExtensionModuleFinder
    """Object that is responsible for finding extension modules given their
    names.
    """

    _app: Optional[TApp]
    """The application that owns the extension manager."""

    _app_restart_requested_by: set[str]
    """Iterable containing the names of the extensions that have requested the
    host application to restart itself.
    """

    _extension_data: keydefaultdict[str, ExtensionData]
    """Dictionary mapping extension names to their associated data object."""

    _load_order: MRUContainer[str]
    """Order in which the extensions were loaded."""

    _pending_actions_on_discovery: defaultdict[
        str, list[Callable[[ExtensionData], None]]
    ]
    """Functions to call when an extension with the given name is discovered
    for the first time.
    """

    _shutting_down: bool
    """Whether the extension manager is shutting down."""

    _silent_mode_entered: int
    """Counter that indicates how many times the extension manager was requested
    to enter silent mode without another request to counterbalance it.
    """

    _spinning: bool
    """Whether the extension manager is "spinning". See the documentation of
    the `spinning` property for more details.
    """

    _task_queue: Optional[
        SendChannel[
            tuple[
                Callable[..., Any], Any, Optional[AwaitableCancelScope], Optional[str]
            ]
        ]
    ]
    """Queue containing background tasks to be spawned and their associated names
    and cancel scopes.
    """

    def __init__(self, package_root: Optional[str] = None):
        """Constructor.

        Parameters:
            package_root: the root package in which all other extension
                packages should live; ``None`` if no such package exists
            entry_point_group: name of the PyPA entry point group that can be
                used to discover extensions managed by this manager; ``None``
                if discovery based on entry points should not be used
        """
        self.module_finder = ExtensionModuleFinder()
        if package_root:
            self.module_finder.add_package_root(package_root)

        self._app = None

        self._app_restart_requested_by = set()
        self._extension_data = keydefaultdict(self._create_extension_data)
        self._load_order = MRUContainer()
        self._pending_actions_on_discovery = defaultdict(list)
        self._silent_mode_entered = 0
        self._spinning = False

    @property
    def app(self) -> Optional[TApp]:
        """The application context of the extension manager. This will also
        be passed on to the extensions when they are initialized.
        """
        return self._app

    @property
    def app_restart_requested(self) -> bool:
        """Returns whether at least one extension has requested the host
        application to restart itself.
        """
        return bool(self._app_restart_requested_by)

    async def set_app(self, value: Optional[TApp]) -> None:
        """Asynchronous setter for the application context of the
        extension manager.
        """
        if self._app is value:
            return

        if self._spinning:
            await self._spindown_all_extensions()

        self._app = value

        if self._spinning:
            await self._spinup_all_extensions()

    async def _configure_and_load_extensions(
        self, configuration: Configuration, **kwds
    ) -> None:
        """Configures the extension manager and loads all configured extensions.

        Extensions that were loaded earlier will be unloaded before loading
        the new ones with the given configuration.

        Parameters:
            configuration: a dictionary mapping names of the
                extensions to their configuration.

        Keyword arguments:
            app: when specified, sets the application context of the
                extension manager as well
        """
        if "app" in kwds:
            await self.set_app(kwds["app"])

        # Remember all the extensions that were loaded before this function
        # was called
        maybe_to_load = set(self.loaded_extensions)

        # Temporarily stop all extensions while we process the new configuration
        await self.teardown()

        # Process the configuration
        for extension_name, extension_cfg in configuration.items():
            try:
                self.configure(extension_name, extension_cfg)
                maybe_to_load.add(extension_name)
            except NoSuchExtension:
                # It is not a problem if the extension is not enabled explicitly
                enabled = self._get_enabled_state_from_configuration(extension_cfg)
                if enabled.is_explicitly_enabled:
                    base_log.warning(f"No such extension: {extension_name!r}")
            except ImportError:
                # It is not a problem if the extension is disabled anyway
                enabled = self._get_enabled_state_from_configuration(extension_cfg)
                if enabled.is_explicitly_enabled:
                    base_log.exception(
                        f"Error while importing extension: {extension_name!r}"
                    )

        # Check the extensions that were originally loaded, and any new
        # extensions that were discovered from the configuration, and attempt
        # to load them if they are enabled
        for extension_name in sorted(maybe_to_load):
            enabled = self.get_enabled_state_of_extension(extension_name)
            if enabled is EnabledState.YES:
                await self.load(extension_name)
            elif enabled is EnabledState.PREFER:
                with self._use_silent_mode():
                    await self.load(extension_name)

    def _create_extension_data(self, extension_name: str) -> ExtensionData:
        """Creates a helper object holding all data related to the extension
        with the given name.

        Parameters:
            extension_name: the name of the extension

        Raises:
            NoSuchExtension: if the extension with the given name does not exist
        """
        if not self.exists(extension_name):
            raise NoSuchExtension(extension_name)

        data = ExtensionData.for_extension(extension_name)
        data.api_proxy = ExtensionAPIProxy(self, extension_name)

        for func in self._pending_actions_on_discovery.pop(extension_name, ()):
            func(data)

        return data

    def _get_loaded_extension_by_name(self, extension_name: str) -> Any:
        """Returns the extension object corresponding to the extension
        with the given name if it is loaded.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the extension with the given name

        Raises:
            KeyError: if the extension with the given name is not declared in
                the configuration file or if it is not loaded
        """
        if extension_name not in self._extension_data:
            raise KeyError(extension_name)

        ext = self._extension_data[extension_name]
        if not ext.loaded or ext.instance is None:
            raise KeyError(extension_name)
        else:
            return ext.instance

    def _get_module_for_extension(self, extension_name: str) -> ModuleType:
        """Returns the module that contains the given extension.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the module containing the extension with the given name

        Raises:
            NoSuchExtension: if there is no such extension
        """
        return self.module_finder.get_module_for_extension(extension_name)

    def _get_module_for_extension_safely(
        self, extension_name: str, *, raise_on_error: bool = True
    ) -> Optional[ModuleType]:
        """Returns the module that contains the given extension, logging
        errors appropriately and returning ``None`` if the extension does not
        exist.

        Parameters:
            extension_name: the name of the extension
            raise_on_error: whether to re-raise exceptions when the import fails

        Returns:
            the module containing the extension with the given name, or ``None``
            if there is no such extension with the given name
        """
        try:
            return self.module_finder.get_module_for_extension(extension_name)
        except NoSuchExtension:
            base_log.warning(f"No such extension: {extension_name!r}")
        except ImportError:
            base_log.exception(f"Error while importing extension {extension_name!r}")
            if raise_on_error:
                raise
        except Exception:
            base_log.exception(f"Error while importing extension {extension_name!r}")
            if raise_on_error:
                raise

    def configure(
        self, extension_name: str, configuration: ExtensionConfiguration
    ) -> None:
        """Configures the extension with the given name.

        Changes to the configuration of an extension that is already loaded will
        not take effect until the extension is reloaded. Attempts to retrieve a
        configuration snapshot from the extension will still yield the old
        configuration until the next reload.
        """
        self._extension_data[extension_name].configure(configuration)

    def exists(self, extension_name: str) -> bool:
        """Returns whether the extension with the given name exists,
        irrespectively of whether it was loaded already or not.

        Parameters:
            extension_name: the name of the extension

        Returns:
            whether the extension exists
        """
        return self.module_finder.exists(extension_name)

    def get_configuration_schema(
        self, extension_name: str
    ) -> Optional[ExtensionConfigurationSchema]:
        """Returns a JSON-Schema description of the expected format of the
        configuration of the given extension, if the extension provides one.

        Parameters:
            extension_name: the name of the extension whose configuration schema
                is to be retrieved

        Returns:
            the configuration schema of the extension or `None` if the extension
            does not exist or does not provide a configuration schema
        """
        module = self._get_module_for_extension_safely(extension_name)
        if module is None:
            return None

        func = getattr(module, "get_schema", None)
        if callable(func):
            try:
                schema = func()
            except Exception:
                base_log.exception(
                    f"Error while retrieving configuration schema of "
                    f"extension {extension_name!r}"
                )
                raise
        else:
            schema = getattr(module, "schema", None)

        if schema is None:
            return schema

        if not isinstance(schema, dict):
            raise RuntimeError("configuration schema must be a dictionary")

        if "type" in schema and schema["type"] != "object":
            raise RuntimeError("configuration schema must describe a JSON object")

        schema = deepcopy(schema)

        if "type" not in schema:
            schema["type"] = "object"

        return schema

    def get_configuration_snapshot(self, extension_name: str) -> ExtensionConfiguration:
        """Returns a snapshot of the configuration of the given extension.

        The snapshot is a deep copy of the configuration object of the
        extension; you may freely modify it without affecting the extension
        even if it is loaded.

        Parameters:
            extension_name: the name of the extension whose configuration is
                to be snapshotted

        Returns:
            a deep copy of the configuration object of the extension
        """
        extension_data = self._extension_data.get(extension_name)
        return deepcopy(extension_data.configuration) if extension_data else {}

    def get_configuration_snapshot_dict(
        self, disable_unloaded: bool = False
    ) -> dict[str, ExtensionConfiguration]:
        """Returns a dictionary mapping extension names to snapshots of the
        configurations of the given extensions.

        The snapshot is a deep copy of the configuration object of the
        extension; you may freely modify it without affecting the extension
        even if it is loaded.

        Parameters:
            disable_unloaded: whether to mark currently unloaded extensions as
                disabled

        Returns:
            a dictionary mapping extension names to deep copies of the
            configuration objects
        """
        result = {
            name: self.get_configuration_snapshot(name)
            for name in self.known_extensions
        }
        if disable_unloaded:
            for name, config in result.items():
                if self.is_loaded(name):
                    # If the extension is loaded, and in the configuration it is
                    # marked with something else than "auto" or "yes", let us
                    # override it and enable it explicitly
                    if "enabled" in config:
                        enabled = self._get_enabled_state_from_configuration(config)
                        if enabled not in (EnabledState.YES, EnabledState.PREFER):
                            config["enabled"] = True
                else:
                    # If the extension is not loaded, and in the configuration
                    # it is marked with something else than "avoid", let us
                    # override it an disable it explicitly
                    enabled = self._get_enabled_state_from_configuration(config)
                    if enabled not in (EnabledState.NO, EnabledState.AVOID):
                        config["enabled"] = False
        return result

    def get_dependencies_of_extension(self, extension_name: str) -> set[str]:
        """Determines the list of extensions that a given extension depends
        on directly.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the names of the extensions that the given extension depends on;
            an empty set if the extension does not exist
        """
        module = self._get_module_for_extension_safely(extension_name)
        if module is None:
            return set()

        func = getattr(module, "get_dependencies", None)
        if callable(func):
            try:
                dependencies = func()
            except Exception:
                base_log.exception(
                    f"Error while determining dependencies of extension {extension_name!r}"
                )
                dependencies = None
        else:
            dependencies = getattr(module, "dependencies", None)

        return set(dependencies or [])

    def get_description_of_extension(self, extension_name: str) -> Optional[str]:
        """Returns a human-readable description of the extension with the given
        name (if the extension provides a description).

        An extension may provide a description by defining a `get_description`
        attribute (which must be a function that can be called with no arguments)
        or a `description` attribute (which must be a string). In the absence of
        these attributes, the description will be derived from the first
        paragraph of the docstring of the extension.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the description of the extension or `None` if the extension does not
            provide a description
        """
        module = self._get_module_for_extension_safely(extension_name)
        if module is None:
            return None

        func = getattr(module, "get_description", None)
        if callable(func):
            try:
                description = func()
            except Exception:
                base_log.exception(
                    f"Error while getting the description of extension {extension_name!r}"
                )
                description = None
        elif hasattr(module, "description"):
            description = str(module.description)
        else:
            description = getattr(module, "__doc__", None)
            if description and isinstance(description, str):
                description, _, _ = description.strip().partition("\n\n")
            else:
                description = None

        return description

    def get_enhancements_of_extension(self, extension_name: str) -> dict[str, Enhancer]:
        """Returns a dictionary mapping names of other extensions that an
        extension enhances to the corresponding enhancer function or context
        manager.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the mapping of extension names to enhancements; an empty dictionary
            if the extension does not enhance any other extension
        """
        module = self._get_module_for_extension_safely(extension_name)
        if module is None:
            return {}

        func = getattr(module, "get_enhancers", None)
        if callable(func):
            try:
                enhancers = func()
            except Exception:
                base_log.exception(
                    f"Error while getting the enhancers of extension {extension_name!r}"
                )
                enhancers = {}
        elif hasattr(module, "enhancers"):
            enhancers = module.enhancers
        elif hasattr(module, "enhances"):
            enhancers = module.enhances
        else:
            enhancers = {}

        return enhancers

    def get_extensions_requesting_app_restart(self) -> Iterable[str]:
        """Returns an iterable that yields the names of all the extensions that
        have requested the application to restart itself.
        """
        return sorted(self._app_restart_requested_by)

    def get_reverse_dependencies_of_extension(self, extension_name: str) -> set[str]:
        """Determines the list of _loaded_ extensions that depend on a given
        extension directly.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the names of the extensions that the given extension depends on
        """
        reverse_dependencies = set()

        if self.is_loaded(extension_name):
            for other_name in self._load_order.reversed():
                if other_name == extension_name:
                    break

                if extension_name in self.get_dependencies_of_extension(other_name):
                    reverse_dependencies.add(other_name)

        return reverse_dependencies

    def get_tags_of_extension(self, extension_name: str) -> set[str]:
        """Returns the list of tags associated to the extension with the given
        name.

        Tags are primarily used to mark extensions as experimental or deprecated,
        or to organize them into categories.

        An extension may provide tags by defining a `get_tags` attribute (which
        must be a function that can be called with no arguments) or a `tags`
        attribute (which must be an iterable of strings). In the absence of
        these attributes, the extension is assumed to have no tags.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the tags of the extension
        """
        module = self._get_module_for_extension_safely(extension_name)
        if module is None:
            return set()

        func = getattr(module, "get_tags", None)
        if callable(func):
            try:
                tags = func()
            except Exception:
                base_log.exception(
                    f"Error while getting the tags of extension {extension_name!r}"
                )
                tags = ()
        else:
            tags = getattr(module, "tags", None) or ()

        if isinstance(tags, str):
            return set(tags.split())
        else:
            return {str(tag) for tag in tags}

    def get_version_of_extension(self, extension_name: str) -> Optional[Version]:
        """Returns the version number of the extension with the given name (if
        the extension provides a version number).

        An extension may provide a version number by defining a `get_version`
        attribute (which must be a function that can be called with no arguments
        and must return a string or a `semver.Version` object), a `version`
        attribute (which must be a string or a `semver.Version` object) or a
        `__version__` attribute (for sake of compatibility with typical Python
        modules). In the absence of these attributes, the version will be derived
        from the version metadata of the module providing the extension. When
        all these attempts to derive a version number fails, the result will be
        ``None``.

        Parameters:
            extension_name: the name of the extension

        Returns:
            the version of the extension or `None` if the extension does not
            provide a version number
        """
        module = self._get_module_for_extension_safely(extension_name)
        if module is None:
            return None

        func = getattr(module, "get_version", None)
        maybe_version: Optional[Union[str, Version]] = None

        try:
            if callable(func):
                maybe_version = func()
            elif hasattr(module, "version") or hasattr(module, "__version__"):
                maybe_version = getattr(module, "version", None) or getattr(
                    module, "__version__", None
                )

                # version or __version__ may also be a Python module
                if isinstance(maybe_version, ModuleType):
                    maybe_version = getattr(maybe_version, "version", None) or getattr(
                        maybe_version, "__version__", None
                    )

        except Exception:
            base_log.exception(
                f"Error while getting the version of extension {extension_name!r}"
            )

        if maybe_version is None:
            try:
                module_name = self.module_finder.get_module_name_for_extension(
                    extension_name
                )
                maybe_version = get_version_from_metadata(module_name)
            except Exception:
                pass

        try:
            return (
                Version.parse(str(maybe_version)) if maybe_version is not None else None
            )
        except ValueError:
            # Not a valid semantic version
            base_log.error(
                f"Version of extension {extension_name!r} is not a semantic version number: {maybe_version!r}"
            )
            return None
        except Exception:
            base_log.exception(
                f"Error while parsing the version of extension {extension_name!r}"
            )
            return None

    def import_api(self, extension_name: str) -> ExtensionAPIProxy:
        """Imports the API exposed by an extension.

        Extensions *may* have a dictionary named ``exports`` that allows the
        extension to export some of its variables, functions or methods.
        Other extensions may access the exported members of an extension by
        calling the `import_api`_ method of the extension manager.

        This function supports "lazy imports", i.e. one may import the API
        of an extension before loading the extension. When the extension
        is not loaded, the returned API object will have a single property
        named ``loaded`` that is set to ``False``. When the extension is
        loaded, the returned API object will set ``loaded`` to ``True``.
        Attribute retrievals on the returned API object are forwarded to the
        API of the extension.

        Parameters:
            extension_name: the name of the extension whose API is to be
                imported

        Returns:
            a proxy object to the API of the extension that forwards attribute
            retrievals to the API, except for the property named ``loaded``,
            which returns whether the extension is loaded or not.

        Raises:
            KeyError: if the extension with the given name does not exist
        """
        proxy = self._extension_data[extension_name].api_proxy
        if proxy is None:
            raise RuntimeError(f"Extension {extension_name!r} does not have an API")
        return proxy

    def get_enabled_state_of_extension(self, extension_name: str) -> EnabledState:
        """Returns whether the extension with the given name is enabled
        according to its configuration object.

        The return value of this function is a multi-state enum; see the
        documentation of `EnabledState` for more information.

        Returns:
            an enum describing the enabled state of the extension, or
            EnabledState.UNKNOWN if the extension is not known.
        """
        try:
            cfg = self._extension_data[extension_name].configuration
        except KeyError:
            return EnabledState.UNKNOWN

        return self._get_enabled_state_from_configuration(cfg)

    def is_experimental(self, extension_name: str) -> bool:
        """Returns whether the extension with the given name is experimental.

        An extension is experimental if it is tagged with ``"experimental"``.
        See `get_tags_of_extension()` for more details.

        Parameters:
            extension_name: the name of the extension

        Returns:
            whether the extension is experimental. Non-existent extensions are
            not considered experimental.
        """
        tags = self.get_tags_of_extension(extension_name)
        return "experimental" in tags

    @property
    def known_extensions(self) -> list[str]:
        """Returns a list containing the names of all the extensions that the
        extension manager currently knows about (even if they are currently
        unloaded). The caller is free to modify the list; it will not affect
        the extension manager.

        Returns:
            the names of all the extensions that are currently known to the
            extension manager
        """
        return sorted(self._extension_data.keys())

    async def load(self, extension_name: str) -> None:
        """Loads an extension with the given name.

        The extension name will be resolved to the extension itself using the
        extension discovery mechanism that is configured in the module finder
        associated to the extension manager; see the `module_finder` property
        and the `ExtensionModuleFinder` class for more details. Typically, the
        module finder resolves an extension name to a Python module using
        Python package entry points or by looking them up in a designated
        namespace package (e.g., ``flockwave.ext``). When the module contains a
        callable named ``construct()``, it will be called to construct a new
        instance of the extension. Otherwise, the entire module is assumed to be
        the extension instance.

        Extension instances should have methods named ``load()`` and
        ``unload()``; these methods will be called when the extension
        instance is loaded or unloaded. The ``load()`` method is always
        called with the application context, the configuration object of
        the extension and a logger instance that the extension should use
        for logging. The ``unload()`` method is always called without an
        argument.

        Extensions may declare dependencies if they provide a function named
        ``get_dependencies()``. The function must return a list of extension
        names that must be loaded _before_ the extension itself is loaded.
        This function will take care of loading all dependencies before
        loading the extension itself.

        Parameters:
            extension_name: the name of the extension to load

        Returns:
            whatever the `load()` function of the extension returns
        """
        return await self._load(extension_name, forbidden=[], history=[])

    @property
    def loaded_extensions(self) -> list[str]:
        """Returns a list containing the names of all the extensions that
        are currently loaded into the extension manager. The caller is free
        to modify the list; it will not affect the extension manager.

        Returns:
            the names of all the extensions that are currently loaded
        """
        return sorted(key for key, ext in self._extension_data.items() if ext.loaded)

    @property
    def loaded_leaf_extensions(self) -> list[str]:
        """Returns a list containing the names of extensions that are currently
        loaded into the extension manager such that no other extensions depend on
        them. The caller is free to modify the list; it will not affect the
        extension manager.

        Returns:
            the names of all the extensions that are currently loaded and where
            no other extensions depend on them
        """
        loaded = set(self.loaded_extensions)
        to_remove = {
            key
            for key in self.loaded_extensions
            if not loaded.isdisjoint(self._extension_data[key].dependents)
        }
        return sorted(loaded - to_remove)

    def is_loaded(self, extension_name: str) -> bool:
        """Returns whether the given extension is loaded."""
        try:
            self._get_loaded_extension_by_name(extension_name)
            return True
        except KeyError:
            return False

    def needs_restart(self, extension_name: str) -> bool:
        """Returns whether the extension with the given name should be restarted
        in order to activate the new configuration.
        """
        extension_data = self._extension_data.get(extension_name)
        return extension_data.needs_restart if extension_data else False

    async def reload(self, extension_name: str) -> None:
        """Reloads the extension with the given name, taking care of unloading
        its reverse dependencies first and loading them again after the
        extension itself was reloaded.
        """
        history = []
        await self._unload(extension_name, forbidden=[], history=history)
        for dep in reversed(history):
            await self.load(dep)

    def request_host_app_restart(self, extension_name: str) -> None:
        """Callback function that can be called from an extension if the
        extension wishes to request the host application to restart itself.

        No-op if the extension has already requested a restart.

        Args:
            extension_name: name of the extension requesting the restart
        """
        self._on_extension_requested_app_restart(extension_name)

    async def run(
        self,
        *,
        configuration: Configuration,
        app: TApp,
        task_status=TASK_STATUS_IGNORED,
    ) -> None:
        """Asynchronous task that runs the extension manager itself.

        This task simply waits for messages that request certain tasks managed
        by the extensions to be started. It also takes care of catching
        exceptions from the managed tasks and logging them without crashing the
        entire application.
        """
        try:
            self._task_queue, task_queue_rx = open_memory_channel(1024)

            await self._configure_and_load_extensions(configuration, app=app)

            async with open_nursery() as nursery:
                task_status.started()
                async for func, args, scope, name in task_queue_rx:
                    if scope is not None:
                        func = partial(func, cancel_scope=scope)
                    if name:
                        nursery.start_soon(func, *args, name=name)
                    else:
                        nursery.start_soon(func, *args)

        finally:
            self._task_queue = None

    async def _run_in_background(
        self,
        func: Callable[..., Any],
        *args,
        name: Optional[str] = None,
        cancellable: bool = False,
    ) -> Optional[AwaitableCancelScope]:
        """Runs the given function as a background task in the extension
        manager.

        Blocks until the task is started.
        """
        assert self._task_queue is not None

        scope = (
            AwaitableCancelScope()
            if cancellable or hasattr(func, "_cancellable")
            else None
        )
        await self._task_queue.send((func, args, scope, name))
        return scope

    def _run_when_discovered(
        self, extension_name: str, func: Callable[[ExtensionData], None]
    ):
        """Registers a function to be executed when the extension with the given
        name is discovered for the first time and its extension data object is
        created.

        The function is executed immediately if it is already discovered.

        Arguments:
            extension_name: name of the extension
            func: function to call
        """
        data = self._extension_data.get(extension_name)
        if data is not None:
            func(data)
        else:
            self._pending_actions_on_discovery[extension_name].append(func)

    def rescan(self) -> None:
        """Refreshes the list of extensions known to the extension manager by
        scanning all registered entry points in the extension module finder.
        """
        for name in self.module_finder.iter_extension_names():
            try:
                # Trigger the creation of an entry for this extension
                self._extension_data[name]
            except Exception:
                # doesn't matter; probably it is not an extension or the
                # extension bailed out during import
                pass

    @property
    def shutting_down(self) -> bool:
        """Whether the extension manager is currently shutting down. Can be used
        by essential extensions that are not supposed to be unloaded to detect
        that the entire extension manager is shutting down and act accordingly.
        """
        return self._shutting_down

    @property
    def spinning(self) -> bool:
        """Whether the extensions in the extension manager are "spinning".

        This property is relevant for extensions that can exist in an idle
        state and in a "spinning" state. Setting the property to `True` will
        put all such extensions in the "spinning" state by invoking the
        `spinup()` method of the extensions. Setting the property to `False`
        will put all such extensions in the "idle" state by invoking the
        `spindown()` method of the extensions. Additionally, the `worker()`
        task of the extension will be running only if it is in the "spinning"
        state.
        """
        return self._spinning

    async def set_spinning(self, value: bool) -> None:
        """Asynchronous setter for the `spinning` property."""
        value = bool(value)

        if self._spinning == value:
            return

        if self._spinning:
            await self._spindown_all_extensions()

        self._spinning = value

        if self._spinning:
            await self._spinup_all_extensions()

    async def teardown(self) -> None:
        """Tears down the extension manager and prepares it for destruction."""
        self._shutting_down = True
        try:
            for ext_name in self._load_order.reversed():
                await self.unload(ext_name)
        finally:
            self._shutting_down = False

    async def unload(self, extension_name: str) -> None:
        """Unloads the extension with the given name.

        Parameters:
            extension_name: the name of the extension to unload
        """
        return await self._unload(extension_name, forbidden=[], history=[])

    def was_app_restart_requested_by(self, extension_name: str) -> bool:
        """Returns whether the extension has already requested the host
        application to restart itself.
        """
        if extension_name not in self._extension_data:
            return False

        return self._extension_data[extension_name].requested_host_app_restart

    async def _load(
        self, extension_name: str, forbidden: list[str], history: list[str]
    ):
        """Loads an extension with the given name, ensuring that all its
        dependencies are loaded before the extension itself.

        This function is internal; use `load()` instead if you want to load
        an extension programmatically, and it will take care of loading all
        the dependencies as well.

        Parameters:
            extension_name: the name of the extension to load
            forbidden: list of extensions that are _currently_ being loaded,
                used to detect dependency cycles
            history: list that is used to record which extensions were loaded
                successfully
        """
        if not self._ensure_no_cycle(forbidden, extension_name):
            return

        await self._ensure_dependencies_loaded(extension_name, forbidden, history)
        if not self.is_loaded(extension_name):
            await self._load_single_extension(extension_name)
            history.append(extension_name)

    async def _load_single_extension(self, extension_name: str):
        """Loads an extension with the given name, assuming that all its
        dependencies are already loaded.

        This function is internal; use `load()` instead if you want to load
        an extension programmatically, and it will take care of loading all
        the dependencies as well.

        Parameters:
            extension_name: the name of the extension to load
        """
        if extension_name in ("logger", "manager", "base", "__init__"):
            raise ValueError(f"invalid extension name: {extension_name!r}")

        log = base_log
        extra = {"id": extension_name}

        extension_data = self._extension_data[extension_name]
        configuration = extension_data.commit_configuration_changes()

        log.debug("Loading extension", extra=extra)

        # Find the module for the extension
        try:
            module = self._get_module_for_extension_safely(extension_name)
        except Exception:
            # logged already in self._get_module_for_extension_safely()
            module = None

        if module is None:
            return None

        # Create the extension instance
        instance_factory = getattr(module, "construct", None)

        try:
            extension = instance_factory() if instance_factory else module
        except NotLoadableError as ex:
            self._on_extension_not_loadable(extension_name, str(ex))
            return None
        except Exception:
            log.exception("Error while instantiating extension", extra=extra)
            return None

        if not isinstance(extension, ModuleType) and hasattr(extension, "name"):
            try:
                extension.name = extension_name
            except Exception:
                # Maybe the property is used by the extension for something else?
                pass

        args = (self.app, configuration, extension_data.log)

        # Call the "load" callback
        func = getattr(extension, "load", None)
        if callable(func):
            try:
                result = bind(func, args, partial=True)()
            except NotLoadableError as ex:
                self._on_extension_not_loadable(extension_name, str(ex))
                return None
            except ApplicationRestart:
                self._on_extension_requested_app_restart(extension_name)
                return None
            except ApplicationExit:
                # Let this exception propagate
                raise
            except Exception:
                log.exception("Error while loading extension", extra=extra)
                return None
        else:
            result = None

        # Spawn the "run" task
        task = getattr(extension, "run", None)
        if iscoroutinefunction(task):
            task = bind(task, args, partial=True)
            extension_data.task = await self._run_in_background(
                self._protect_extension_task(cancellable(task), extension_name),
                name=f"extension:{extension_name}/run",
            )
        elif task is not None:
            log.warn("run() must be an async function", extra=extra)

        # Register enhancements
        enhances = self.get_enhancements_of_extension(extension_name)
        extension_data.enhances = {}
        if isinstance(enhances, dict):
            extension_data.enhances.update(
                {
                    k: Enhancement(enhancer=v, provider=extension_name, target=k)
                    for k, v in enhances.items()
                }
            )
        elif enhances is not None:
            log.warn("enhancements must be provided in a dict")

        for target, enhancement in extension_data.enhances.items():
            self._run_when_discovered(
                target,
                partial(
                    ExtensionData._notify_enhanced_by,
                    extension_name=extension_name,
                    enhancement=enhancement,
                ),
            )

        # Finalize loading
        extension_data.instance = extension
        extension_data.loaded = True
        self._load_order.append(extension_name)

        # Register dependents
        for dependency in self.get_dependencies_of_extension(extension_name):
            self._extension_data[dependency].dependents.add(extension_name)

        # Send the "loaded" signal
        self.loaded.send(self, name=extension_name, extension=extension)

        # Activate enhancements where this extension is the provider or the
        # target. Note that this is the earliest point where we can do this
        # because the "loaded" signal above is the one that exposes the
        # API of the target so that the provider can call it.
        enhancements_to_activate = [
            enhancement
            for other, enhancement in extension_data.enhances.items()
            if other in self._extension_data
            and self._extension_data[other].loaded
            and not enhancement.active
        ]
        enhancements_to_activate.extend(
            enhancement
            for other, enhancement in extension_data.enhanced_by.items()
            if other in self._extension_data
            and self._extension_data[other].loaded
            and not enhancement.active
        )
        for enhancement in enhancements_to_activate:
            log.debug(
                f"Activating enhancement for {enhancement.target!r}",
                extra={"id": enhancement.provider},
            )

            api = self._extension_data[enhancement.target].api_proxy

            assert api is not None
            enhancement.activate(api, extension)

        # Spin up the extension
        if self._spinning:
            await self._spinup_extension(extension_name)

        return result

    def _on_extension_not_loadable(
        self, extension_name: str, message: Optional[str] = None
    ) -> None:
        """Logs a message that indicates that the extension with the given name
        cannot be loaded.
        """
        if self._silent_mode_entered > 0:
            return

        # Log the exception with a standard message
        extra = {"id": extension_name}
        if message:
            base_log.error(f"Extension cannot be loaded. {message}", extra=extra)
        else:
            base_log.error("Extension cannot be loaded", extra=extra)

    def _on_extension_requested_app_restart(self, extension_name: str) -> None:
        """Handles the event when an extension requests the host application
        to restart itself (typically by throwing ApplicationRestart_ or by
        calling the appropriate method of the extension manager.
        """
        if extension_name in self._app_restart_requested_by:
            return

        ext = self._extension_data[extension_name]
        ext.requested_host_app_restart = True

        self._app_restart_requested_by.add(extension_name)

        self.restart_requested.send(self, name=extension_name)

    async def _on_extension_task_crashed(
        self, extension_name: str, exc: BaseException
    ) -> None:
        """Handles an exception that originated from the extension with the
        given name.
        """
        if isinstance(exc, NotLoadableError):
            enabled_state = self.get_enabled_state_of_extension(extension_name)
            if enabled_state is not EnabledState.PREFER:
                self._on_extension_not_loadable(extension_name, str(exc))
        else:
            base_log.exception(
                f"Unexpected exception caught from extension {extension_name!r}"
            )

        await self.unload(extension_name)

    def _protect_extension_task(
        self, func: Callable[..., Awaitable[T]], extension_name: str
    ) -> Callable[..., Awaitable[Optional[T]]]:
        """Given a main asynchronous task corresponding to an extension,
        returns another async task that handles exceptions coming from the
        task gracefully, without crashing the extension manager.
        """

        # Don't use partial() here -- it's hard to detect whether a partial
        # function is async or not
        async def handler(exc: BaseException) -> None:
            try:
                await self._on_extension_task_crashed(extension_name, exc)
            except ApplicationRestart:
                # Not an error
                self._on_extension_requested_app_restart(extension_name)

        return protected(handler)(func)

    async def _spindown_all_extensions(self) -> None:
        """Iterates over all loaded extensions and spins down each one of
        them.
        """
        for extension_name in self._load_order.reversed():
            await self._spindown_extension(extension_name)

    async def _spinup_all_extensions(self) -> None:
        """Iterates over all loaded extensions and spins up each one of
        them.
        """
        for extension_name in self._load_order.items():
            await self._spinup_extension(extension_name)

    async def _spindown_extension(self, extension_name: str) -> None:
        """Spins down the given extension.

        This is done by calling the ``spindown()`` method or function of
        the extension, if any.

        Arguments:
            extension_name: the name of the extension to spin down.
        """
        extension = self._get_loaded_extension_by_name(extension_name)
        extension_data = self._extension_data[extension_name]

        # Stop the worker associated to the extension if it has one
        worker = extension_data.worker
        if worker:
            extension_data.worker = None
            await worker.cancel()

        # Call the spindown hook of the extension if it has one
        func = getattr(extension, "spindown", None)
        if callable(func):
            try:
                func()
            except Exception:
                base_log.exception(
                    "Error while spinning down extension", extra={"id": extension_name}
                )
                return

    async def _spinup_extension(self, extension_name: str) -> None:
        """Spins up the given extension.

        This is done by calling the ``spinup()`` method or function of
        the extension, if any.

        Arguments:
            extension_name: the name of the extension to spin up.
        """
        extension = self._get_loaded_extension_by_name(extension_name)
        extension_data = self._extension_data[extension_name]

        # Call the spinup hook of the extension if it has one
        func = getattr(extension, "spinup", None)
        if callable(func):
            try:
                func()
            except Exception:
                base_log.exception(
                    "Error while spinning up extension", extra={"id": extension_name}
                )
                return

        # Start the worker associated to the extension if it has one
        task = getattr(extension, "worker", None)
        if iscoroutinefunction(task):
            args = (self.app, extension_data.configuration, extension_data.log)
            task = bind(task, args, partial=True)
            extension_data.worker = await self._run_in_background(
                self._protect_extension_task(cancellable(task), extension_name),
                name=f"extension:{extension_name}/worker",
            )
        elif task is not None:
            base_log.warn(
                "worker() must be an async function", extra={"id": extension_name}
            )

    async def _unload(
        self, extension_name: str, forbidden: list[str], history: list[str]
    ) -> None:
        """Unloads an extension with the given name, ensuring that all its
        reverse dependencies are unloaded before the extension itself.

        This function is internal; use `unload()` instead if you want to unload
        an extension programmatically, and it will take care of unloading all
        the reverse dependencies as well.

        Parameters:
            extension_name: the name of the extension to unload
            forbidden: list of extensions that are _currently_ being unloaded,
                used to detect dependency cycles
            history: list that is used to record which extensions were unloaded
                successfully
        """
        if not self._ensure_no_cycle(forbidden, extension_name):
            return

        await self._ensure_reverse_dependencies_unloaded(
            extension_name, forbidden, history
        )
        if self.is_loaded(extension_name):
            await self._unload_single_extension(extension_name)
            history.append(extension_name)

    async def _unload_single_extension(self, extension_name: str) -> None:
        log = base_log
        extra = {"id": extension_name}

        # Get the extension instance
        try:
            extension = self._get_loaded_extension_by_name(extension_name)
        except KeyError:
            log.warning("Tried to unload extension but it is not loaded", extra=extra)
            return

        # Get the associated internal bookkeeping object of the extension
        extension_data = self._extension_data[extension_name]
        if extension_data.dependents:
            message = (
                f"Failed to unload extension {extension_name!r} because it is "
                f"still in use"
            )
            raise RuntimeError(message)

        # Unload the extension
        clean_unload = True

        # Cancel all enhancements where this extension is the provider or the
        # target
        enhancements = list(reversed(extension_data.enhanced_by.values())) + list(
            reversed(extension_data.enhances.values())
        )
        extension_data.enhances.clear()
        for enhancement in enhancements:
            try:
                if enhancement.active:
                    enhancement.deactivate()
                    log.debug(
                        f"Deactivated enhancement for {enhancement.other(extension_name)!r}",
                        extra=extra,
                    )
            except Exception:
                clean_unload = False
                log.exception(
                    f"Error while cancelling enhancement to extension "
                    f"{enhancement.target!r}; forcing unload",
                    extra={"id": enhancement.provider},
                )

        # Spin down the extension if needed
        if self._spinning:
            await self._spindown_extension(extension_name)

        # Stop the task associated to the extension if it has one
        task = extension_data.task
        if task:
            extension_data.task = None
            await task.cancel()

        args = (self.app,)

        func = getattr(extension, "unload", None)
        if callable(func):
            try:
                bind(func, args, partial=True)()
            except ApplicationExit:
                # Let this exception propagate
                raise
            except ApplicationRestart:
                self._on_extension_requested_app_restart(extension_name)
            except NotSupportedError:
                # Re-raise the exception with a standard message, hiding the
                # origin where it came from. When the entire extension manager
                # is shutting down, ignore the failure and force-unload
                if not self.shutting_down:
                    log.error("This extension cannot be unloaded", extra=extra)
                    raise NotSupportedError(
                        f"Extension {extension_name!r} cannot be unloaded"
                    ) from None
            except Exception:
                clean_unload = False
                log.exception(
                    "Error while unloading extension; forcing unload", extra=extra
                )

        # Update the internal bookkeeping object
        extension_data.loaded = False
        extension_data.instance = None

        # Remove the extension from its dependents
        self._load_order.remove(extension_name)

        for dependency in self.get_dependencies_of_extension(extension_name):
            self._extension_data[dependency].dependents.remove(extension_name)

        # Commit any pending changes to the configuration of the extension
        extension_data.commit_configuration_changes()

        # Send a signal that the extension was unloaded
        self.unloaded.send(self, name=extension_name, extension=extension)

        # Add a log message
        if clean_unload:
            log.debug("Unloaded extension", extra=extra)
        else:
            log.warning("Unloaded extension", extra=extra)

    @contextmanager
    def _use_silent_mode(self) -> Iterator[None]:
        """Context manager that switches the extension manager into silent mode
        when entering the context and resumes normal mode when exiting the context.

        In silent mode, the extension manager will not log errors or warnings that
        indicate that an extension cannot be loaded. This is useful for auto-enabling
        extensions that require a license or some other external condition; the
        extension manager can simply attempt to load them and then hide the error
        message if the preconditions are not met.
        """
        self._silent_mode_entered += 1
        try:
            yield
        finally:
            self._silent_mode_entered -= 1

    async def _ensure_dependencies_loaded(
        self, extension_name: str, forbidden: list[str], history: list[str]
    ) -> None:
        """Ensures that all the dependencies of the given extension are
        loaded.

        When a dependency of the given extension is not loaded yet, it will
        be loaded automatically first.

        Parameters:
            extension_name: the name of the extension
            forbidden: list of extensions that are _currently_ being loaded,
                used to detect dependency cycles
            history: list that is used to record which extensions were loaded
                successfully

        Raises:
            ImportError: if an extension cannot be imported
        """
        dependencies = self.get_dependencies_of_extension(extension_name)
        forbidden.append(extension_name)
        try:
            for dependency in dependencies:
                await self._load(dependency, forbidden, history)
        finally:
            forbidden.pop()

    async def _ensure_reverse_dependencies_unloaded(
        self, extension_name: str, forbidden: list[str], history: list[str]
    ) -> None:
        """Ensures that all the dependencies of the given extension are
        unloaded.

        When a dependency of the given extension is not unloaded yet, it will
        be unloaded automatically.

        Parameters:
            extension_name: the name of the extension
            forbidden: list of extensions that are _currently_ being loaded,
                used to detect dependency cycles
            history: list that is used to record which extensions were loaded
                successfully

        Raises:
            ImportError: if an extension cannot be imported
        """
        dependencies = self.get_reverse_dependencies_of_extension(extension_name)
        forbidden.append(extension_name)
        try:
            for dependency in dependencies:
                await self._unload(dependency, forbidden, history)
        finally:
            forbidden.pop()

    @staticmethod
    def _ensure_no_cycle(forbidden: list[str], extension_name: str) -> bool:
        if extension_name in forbidden:
            cycle = forbidden + [extension_name]
            base_log.error(
                "Dependency cycle detected: {0}".format(" -> ".join(map(str, cycle)))
            )
            return False
        return True

    @staticmethod
    def _get_enabled_state_from_configuration(cfg: Configuration) -> EnabledState:
        enabled = cfg.get("enabled", EnabledState.PREFER)
        try:
            return EnabledState.from_object(enabled)
        except ValueError:
            return EnabledState.NO


class ExtensionAPIProxy:
    """Proxy object that allows controlled access to the exported API of
    an extension.

    By default, the proxy object just forwards attribute retrievals as
    dictionary lookups to the API object of the extension, with the
    exception of the ``loaded`` property, which returns ``True`` if the
    extension corresponding to the proxy is loaded and ``False`` otherwise.
    When the extension is not loaded, any attribute retrieval will fail with
    an ``AttributeError`` except the ``loaded`` property.
    """

    def __init__(self, manager: ExtensionManager, extension_name: str):
        """Constructor.

        Parameters:
            manager: the extension manager that owns the proxy.
            extension_name: the name of the extension that the proxy handles
        """
        self._extension_name = extension_name
        self._manager = manager
        self._manager.loaded.connect(
            self._on_extension_loaded,
            sender=self._manager,  # type: ignore
        )
        self._manager.unloaded.connect(
            self._on_extension_unloaded,
            sender=self._manager,  # type: ignore
        )

        loaded = self._manager.is_loaded(extension_name)
        self._api = self._get_api_of_extension(extension_name) if loaded else {}
        self._loaded = loaded

    def __getattr__(self, name: str):
        try:
            return self._api[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def loaded(self) -> bool:
        """Returns whether the extension represented by the proxy is
        loaded.
        """
        return self._loaded

    def _get_api_of_extension(self, extension_name: str) -> Any:
        """Returns the API of the given extension."""
        extension = self._manager._get_loaded_extension_by_name(extension_name)
        api = getattr(extension, "exports", None)
        if api is None:
            api = {}
        elif callable(api):
            api = api()
        if not hasattr(api, "__getitem__"):
            raise TypeError(
                f"exports of extension {extension_name!r} must support item "
                f"access with the [] operator"
            )
        return api

    def _on_extension_loaded(self, sender, name: str, extension: ExtensionBase) -> None:
        """Handler that is called when some extension is loaded into the
        extension manager.
        """
        if name == self._extension_name:
            self._api = self._get_api_of_extension(name)
            self._loaded = True

    def _on_extension_unloaded(
        self, sender, name: str, extension: ExtensionBase
    ) -> None:
        """Handler that is called when some extension is unloaded from the
        extension manager.
        """
        if name == self._extension_name:
            self._loaded = False
            self._api = {}
