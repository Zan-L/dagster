import importlib.util
import itertools
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Dict,
    Final,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Type,
    cast,
)

from pydantic import BaseModel, TypeAdapter
from typing_extensions import Self

import dagster._check as check
from dagster._core.errors import DagsterError
from dagster._utils import snakecase
from dagster._utils.pydantic_yaml import parse_yaml_file_to_pydantic

if TYPE_CHECKING:
    from dagster._core.definitions.definitions_class import Definitions


class Component(ABC):
    name: ClassVar[Optional[str]] = None

    @classmethod
    def registered_name(cls):
        return cls.name or snakecase(cls.__name__)

    @classmethod
    def generate_files(cls) -> None:
        raise NotImplementedError()

    @abstractmethod
    def build_defs(self, context: "ComponentLoadContext") -> "Definitions": ...


class LoadableComponent(Component):
    params_schema: ClassVar = None

    @classmethod
    @abstractmethod
    def from_component_params(
        cls, path: Path, component_params: object, context: "ComponentInitContext"
    ) -> Self: ...

    @classmethod
    def loadable_paths(cls, path: Path) -> Sequence[Path]:
        return [path]


class ComponentCollectionModel(BaseModel):
    component_type: str
    components: Mapping[str, Any] = {}


class ComponentCollection(LoadableComponent):
    params_schema: ClassVar[Type[ComponentCollectionModel]] = ComponentCollectionModel

    def __init__(
        self, component_type: Type[LoadableComponent], components: Sequence[LoadableComponent]
    ):
        self.component_type = component_type
        self.components = check.list_param(components, "components", of_type=component_type)

    def build_defs(self, load_context: "ComponentLoadContext") -> "Definitions":
        from dagster._core.definitions.definitions_class import Definitions

        return Definitions.merge(
            *(component.build_defs(load_context) for component in self.components)
        )

    @classmethod
    def from_component_params(
        cls, path: Path, component_params: object, context: "ComponentInitContext"
    ) -> "ComponentCollection":
        loaded_params = TypeAdapter(cls.params_schema).validate_python(component_params)
        component_type = cast(
            Type[LoadableComponent], context.registry.get(loaded_params.component_type)
        )
        check.invariant(issubclass(component_type, LoadableComponent))
        return cls(
            component_type=component_type,
            components=[
                component_type.from_component_params(
                    p, loaded_params.components.get(p.stem), context
                )
                for p in component_type.loadable_paths(path)
            ],
        )


def is_inside_deployment_project(path: str = ".") -> bool:
    try:
        _resolve_deployment_root_path(path)
        return True
    except DagsterError:
        return False


def _resolve_deployment_root_path(path: str) -> str:
    current_path = os.path.abspath(path)
    while not _is_deployment_root(current_path):
        current_path = os.path.dirname(current_path)
        if current_path == "/":
            raise DagsterError("Cannot find deployment root")
    return current_path


def is_inside_code_location_project(path: str = ".") -> bool:
    try:
        _resolve_code_location_root_path(path)
        return True
    except DagsterError:
        return False


def _resolve_code_location_root_path(path: str) -> str:
    current_path = os.path.abspath(path)
    while not _is_code_location_root(current_path):
        current_path = os.path.dirname(current_path)
        if current_path == "/":
            raise DagsterError("Cannot find code location root")
    return current_path


def _is_deployment_root(path: str) -> bool:
    return os.path.exists(os.path.join(path, "code_locations"))


def _is_code_location_root(path: str) -> bool:
    return os.path.basename(os.path.dirname(path)) == "code_locations"


# Deployment
_DEPLOYMENT_CODE_LOCATIONS_DIR: Final = "code_locations"

# Code location
_CODE_LOCATION_CUSTOM_COMPONENTS_DIR: Final = "lib"
_CODE_LOCATION_COMPONENT_INSTANCES_DIR: Final = "components"


class DeploymentProjectContext:
    @classmethod
    def from_path(cls, path: str) -> Self:
        return cls(root_path=_resolve_deployment_root_path(path))

    def __init__(self, root_path: str):
        self._root_path = root_path

    @property
    def deployment_root(self) -> str:
        return self._root_path

    @property
    def code_location_root_path(self) -> str:
        return os.path.join(self._root_path, _DEPLOYMENT_CODE_LOCATIONS_DIR)

    def has_code_location(self, name: str) -> bool:
        return os.path.exists(os.path.join(self._root_path, "code_locations", name))


class CodeLocationProjectContext:
    @classmethod
    def from_path(cls, path: str) -> Self:
        root_path = _resolve_code_location_root_path(path)
        name = os.path.basename(root_path)
        component_registry = ComponentRegistry()

        # TODO: Rm when a more robust solution is implemented
        # Make sure we can import from the cwd
        if sys.path[0] != "":
            sys.path.insert(0, "")

        components_lib_module = f"{name}.{_CODE_LOCATION_CUSTOM_COMPONENTS_DIR}"
        module = importlib.import_module(components_lib_module)
        register_components_in_module(component_registry, module)

        return cls(
            deployment_context=DeploymentProjectContext.from_path(path),
            root_path=root_path,
            name=os.path.basename(root_path),
            component_registry=component_registry,
        )

    def __init__(
        self,
        deployment_context: DeploymentProjectContext,
        root_path: str,
        name: str,
        component_registry: "ComponentRegistry",
    ):
        self._deployment_context = deployment_context
        self._root_path = root_path
        self._name = name
        self._component_registry = component_registry

    @property
    def deployment_context(self) -> DeploymentProjectContext:
        return self._deployment_context

    @property
    def component_types_root_path(self) -> str:
        return os.path.join(self._root_path, self._name, _CODE_LOCATION_CUSTOM_COMPONENTS_DIR)

    @property
    def component_types_root_module(self) -> str:
        return f"{self._name}.{_CODE_LOCATION_CUSTOM_COMPONENTS_DIR}"

    def has_component_type(self, name: str) -> bool:
        return self._component_registry.has(name)

    def get_component_type(self, name: str) -> Type[Component]:
        if not self.has_component_type(name):
            raise DagsterError(f"No component type named {name}")
        return self._component_registry.get(name)

    @property
    def component_instances_root_path(self) -> str:
        return os.path.join(self._root_path, self._name, _CODE_LOCATION_COMPONENT_INSTANCES_DIR)

    @property
    def component_instances(self) -> Iterable[str]:
        return os.listdir(
            os.path.join(self._root_path, self._name, _CODE_LOCATION_COMPONENT_INSTANCES_DIR)
        )

    def has_component_instance(self, name: str) -> bool:
        return os.path.exists(
            os.path.join(self._root_path, self._name, _CODE_LOCATION_COMPONENT_INSTANCES_DIR, name)
        )


class ComponentRegistry:
    def __init__(self):
        self._components: Dict[str, Type[Component]] = {}

    def register(self, name: str, component: Type[Component]) -> None:
        self._components[name] = component

    def has(self, name: str) -> bool:
        return name in self._components

    def get(self, name: str) -> Type[Component]:
        return self._components[name]

    def keys(self) -> Iterable[str]:
        return self._components.keys()

    def __repr__(self):
        return f"<ComponentRegistry {list(self._components.keys())}>"


class ComponentLoadContext:
    def __init__(self, resources: Mapping[str, object] = {}):
        self.resources = resources


class DefsFileModel(BaseModel):
    component_type: str
    component_params: Optional[Mapping[str, Any]] = None


@dataclass
class ComponentInitContext:
    registry: ComponentRegistry = field(default_factory=lambda: ComponentRegistry())
    active_type: Optional[Type[LoadableComponent]] = None
    params: Optional[Mapping[str, Any]] = None

    def with_data_from_path(self, path: Path) -> "ComponentInitContext":
        defs_path = path / "defs.yml"
        if defs_path.exists():
            parsed_config = parse_yaml_file_to_pydantic(
                DefsFileModel, defs_path.read_text(), str(path)
            )
            return ComponentInitContext(
                active_type=cast(
                    Type[LoadableComponent], self.registry.get(parsed_config.component_type)
                ),
                registry=self.registry,
                params=parsed_config.component_params,
            )
        else:
            return self

    def load(self, path: Path) -> Sequence[Component]:
        context = self.with_data_from_path(path)
        if context.active_type:
            return [
                context.active_type.from_component_params(p, context.params, self)
                for p in context.active_type.loadable_paths(path)
            ]
        else:
            return list(itertools.chain(*(context.load(p) for p in path.iterdir() if p.is_dir())))


def build_defs_from_path(
    path: Path,
    registry: ComponentRegistry,
    resources: Mapping[str, object],
) -> "Definitions":
    from dagster._core.definitions.definitions_class import Definitions

    init_context = ComponentInitContext(registry=registry)
    components = init_context.load(path)
    load_context = ComponentLoadContext(resources=resources)
    return Definitions.merge(*[c.build_defs(load_context) for c in components])


def register_components_in_module(registry: ComponentRegistry, root_module: ModuleType) -> None:
    from dagster._core.definitions.load_assets_from_modules import (
        find_modules_in_package,
        find_subclasses_in_module,
    )

    for module in find_modules_in_package(root_module):
        for component in find_subclasses_in_module(module, (Component,)):
            if component is Component:
                continue
            name = f"{module.__name__}[{component.registered_name()}]"
            registry.register(name, component)
