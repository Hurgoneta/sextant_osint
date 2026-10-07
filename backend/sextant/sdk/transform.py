"""Transforms and connectors — the Connector Development Kit.

A **connector** integrates one data source: manifest (metadata, access, cost, legal, disclosure),
credentials, health check. A **transform** is one operation offered by a connector: it takes
one entity of an accepted type and may produce other entities, facts and relationships.

Two equivalent ways to write transforms:

```python
class MyTransform(Transform):                       # standalone class
    name = "example.username_lookup"
    connector = "example"
    title = "Find Example account by username"
    input_types = ("username",)
    output_types = ("account",)

    async def transform(self, entity, ctx):
        resp = await ctx.get(f"https://api.example.org/users/{entity.display}")
        ...
        src = ctx.out.source(resp.url, title="Example API")
        acc = ctx.out.entity("account", f"example:{entity.display}", source=src)
        ctx.out.link(acc, entity.key, "USES", source=src)


@register
class Example(Connector):                           # or: methods on the connector
    manifest = Manifest(name="example", ...)

    @transform(inputs=("username",), outputs=("account",), title="Find Example account by username")
    async def username_lookup(self, entity, ctx):
        ...
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar

from ..model import Entity
from ..ontology import ENTITY_TYPES
from .manifest import Manifest


# --------------------------------------------------------------------------- read-only views

@dataclass(frozen=True)
class EntityView:
    """Immutable snapshot of an entity handed to a transform. Local-only properties (e.g. year of
    birth) are removed here, so no connector can ever send them anywhere."""

    key: str
    type: str
    value: str
    label: str
    status: str = "candidate"
    depth: int = 0
    confidence: float = 0.0
    props: dict[str, tuple] = field(default_factory=dict)

    @classmethod
    def of(cls, e: Entity) -> "EntityView":
        etype = ENTITY_TYPES[e.type]
        local = {p.name for p in etype.properties if p.local_only}
        props = {k: tuple(pv["v"] for pv in vals) for k, vals in e.props.items() if k not in local}
        return cls(e.key, e.type, e.value, e.label, e.status, e.depth, e.confidence, props)

    def get(self, prop: str, default: Any = None) -> Any:
        vals = self.props.get(prop)
        return vals[0] if vals else default

    def values(self, prop: str) -> list[Any]:
        return list(self.props.get(prop, ()))

    def all_list(self, prop: str) -> list[Any]:
        out: list[Any] = []
        for v in self.values(prop):
            for item in v if isinstance(v, list) else [v]:
                if item not in out:
                    out.append(item)
        return out

    @property
    def display(self) -> str:
        """The value as a person would type it (usernames keep their original case)."""
        if self.type == "username":
            return str(self.get("username") or self.label or self.value)
        if self.type == "account":
            return str(self.get("username") or self.value.split(":", 1)[-1])
        if self.type == "person":
            return str(self.get("full_name") or self.label)
        return self.label or self.value

    @property
    def is_seed(self) -> bool:
        return self.status == "seed"


@dataclass(frozen=True)
class InvestigationInfo:
    """What a transform may know about the investigation (never local-only data)."""

    id: str = ""
    target_type: str = "person"
    target_key: str | None = None
    target_names: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()            # ISO-2, inferred locally from phones/locations/domains
    default_region: str | None = None
    seeds: dict[str, tuple[str, ...]] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        return self.target_names[0] if self.target_names else None

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


# --------------------------------------------------------------------------- transforms

class Transform:
    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    input_types: ClassVar[tuple[str, ...]] = ()
    output_types: ClassVar[tuple[str, ...]] = ()
    connector: ClassVar[str] = ""
    auto: ClassVar[bool] = True                  # may the autonomous investigator run it?
    seeds_only: ClassVar[bool] = False           # only on what the investigator typed in
    min_value: ClassVar[float] = 0.0             # automatic runs need input investigative value ≥ this (seeds exempt)
    estimated_requests: ClassVar[int] = 1        # for budget planning
    priority: ClassVar[float] = 0.5              # inherent usefulness 0..1 (ranking)
    sends: ClassVar[str] = ""                    # what is disclosed; default derived from the input

    # set on bound instances
    manifest: Manifest | None = None

    def accepts(self, entity: EntityView, info: InvestigationInfo) -> bool:
        return True

    def disclosure(self, entity: EntityView) -> str:
        if self.sends:
            return self.sends.format(value=entity.display, label=entity.label)
        noun = ENTITY_TYPES[entity.type].title.lower() if entity.type in ENTITY_TYPES else entity.type
        return f"{noun} “{entity.display}”"

    async def transform(self, entity: EntityView, ctx: "Any") -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def spec(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "input_types": list(self.input_types), "output_types": list(self.output_types),
                "connector": self.connector, "auto": self.auto, "seeds_only": self.seeds_only,
                "min_value": self.min_value, "estimated_requests": self.estimated_requests, "priority": self.priority}


TransformFn = Callable[[Any, EntityView, Any], Awaitable[None]]
AcceptFn = Callable[[Any, EntityView, InvestigationInfo], bool]


def transform(*, inputs: tuple[str, ...], title: str, outputs: tuple[str, ...] = (), name: str | None = None,
              description: str = "", auto: bool = True, seeds_only: bool = False, min_value: float = 0.0,
              estimated_requests: int = 1, priority: float = 0.5, accepts: AcceptFn | None = None,
              sends: str = "") -> Callable[[TransformFn], TransformFn]:
    """Decorator turning a connector method into a transform."""
    def deco(fn: TransformFn) -> TransformFn:
        fn.__transform_spec__ = dict(inputs=tuple(inputs), outputs=tuple(outputs), title=title, name=name or fn.__name__,
                                     description=description or (fn.__doc__ or "").strip().split("\n")[0],
                                     auto=auto, seeds_only=seeds_only, min_value=min_value,
                                     estimated_requests=estimated_requests, priority=priority, accepts=accepts,
                                     sends=sends)
        return fn
    return deco


class BoundTransform(Transform):
    """A transform defined as a decorated connector method."""

    def __init__(self, owner: "Connector", fn: TransformFn, spec: dict) -> None:
        self.owner = owner
        self.fn = fn
        self._accepts = spec["accepts"]
        m = owner.manifest
        self.manifest = m
        # instance attributes shadow the ClassVar defaults
        self.name = f"{m.name}.{spec['name']}"                      # type: ignore[misc]
        self.title = spec["title"]                                   # type: ignore[misc]
        self.description = spec["description"]                       # type: ignore[misc]
        self.input_types = spec["inputs"]                            # type: ignore[misc]
        self.output_types = spec["outputs"]                          # type: ignore[misc]
        self.connector = m.name                                      # type: ignore[misc]
        self.auto = spec["auto"]                                     # type: ignore[misc]
        self.seeds_only = spec["seeds_only"]                         # type: ignore[misc]
        self.min_value = spec["min_value"]                           # type: ignore[misc]
        self.estimated_requests = spec["estimated_requests"]         # type: ignore[misc]
        self.priority = spec["priority"]                             # type: ignore[misc]
        self.sends = spec["sends"]                                   # type: ignore[misc]

    def accepts(self, entity: EntityView, info: InvestigationInfo) -> bool:
        return self._accepts(self.owner, entity, info) if self._accepts else True

    async def transform(self, entity: EntityView, ctx: Any) -> None:
        await self.fn(self.owner, entity, ctx)


class Connector:
    """Base class for data-source integrations."""

    manifest: ClassVar[Manifest]
    extra_transforms: ClassVar[tuple[type[Transform], ...]] = ()

    def __init__(self) -> None:
        self._transforms: list[Transform] | None = None

    def transforms(self) -> list[Transform]:
        if self._transforms is None:
            out: list[Transform] = []
            seen: set[str] = set()
            for klass in type(self).__mro__:
                for attr, fn in vars(klass).items():
                    spec = getattr(fn, "__transform_spec__", None)
                    if spec and attr not in seen:
                        seen.add(attr)
                        out.append(BoundTransform(self, fn, spec))
            for tcls in self.extra_transforms:
                t = tcls()
                t.manifest = self.manifest
                if not t.connector:
                    t.connector = self.manifest.name  # type: ignore[misc]
                out.append(t)
            self._transforms = out
        return self._transforms

    def installed(self, cfg: Any = None) -> tuple[bool, str]:
        """Adapters override this (is the external tool present?)."""
        return True, ""

    async def health_check(self, ctx: Any) -> dict:
        """Optional live check. Default: configuration only (no request is sent)."""
        return {"ok": True, "checked": False, "message": "Configuration looks complete (no live request made)."}

    def input_types(self) -> list[str]:
        out: list[str] = []
        for t in self.transforms():
            for it in t.input_types:
                if it not in out:
                    out.append(it)
        return out or list(self.manifest.input_entities)

    def output_types(self) -> list[str]:
        out: list[str] = []
        for t in self.transforms():
            for ot in t.output_types:
                if ot not in out:
                    out.append(ot)
        return out or list(self.manifest.output_entities)


class CatalogConnector(Connector):
    """A documented source without an implementation (enterprise/contact-provider sources)."""

    def __init__(self, manifest: Manifest) -> None:
        super().__init__()
        self.manifest = manifest  # type: ignore[misc]

    def transforms(self) -> list[Transform]:
        return []
