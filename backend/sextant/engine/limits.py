"""Investigation depth presets and budgets.

Depth = how far the engine expands from what the investigator typed:

    1  Direct results       — transforms run on the input only
    2  Related entities     — and on what they directly revealed (accounts → their websites, emails…)
    3  Deep correlation     — two hops out, lower expansion threshold
    4  Aggressive expansion — five hops out, low threshold, username guesses from names

Whatever the depth, these always hold: per-source rate limits, deduplication of (transform, entity),
maximum entities, maximum requests, maximum time, maximum transform runs, and per-connector call caps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class Limits:
    depth: str = "2"
    max_depth: int = 2               # entities at depth < max_depth are expanded (seeds are depth 0)
    expand_threshold: float = 0.5   # investigative value needed to expand a non-seed entity automatically
    max_entities: int = 1500
    max_requests: int = 5000
    max_minutes: float = 15.0
    max_runs: int = 800
    connector_call_cap: int = 60     # default cap of runs per connector per investigation
    search_query_cap: int = 25       # queries per search-engine connector per investigation
    username_guesses: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Limits":
        fields = cls.__dataclass_fields__
        kw = {}
        for k, v in (d or {}).items():
            if k not in fields or v is None:
                continue
            typ = fields[k].type
            try:
                if typ == "bool":
                    kw[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
                else:
                    kw[k] = str(v) if typ == "str" else float(v) if typ == "float" else int(v)
            except (TypeError, ValueError):
                continue
        return cls(**kw)


PRESETS: dict[str, Limits] = {
    "1": Limits("1", max_depth=1, expand_threshold=1.01, max_entities=600, max_requests=2500, max_minutes=8,
                max_runs=200, connector_call_cap=30, search_query_cap=12),
    "2": Limits("2", max_depth=2, expand_threshold=0.5, max_entities=1500, max_requests=6000, max_minutes=15,
                max_runs=700, connector_call_cap=60, search_query_cap=25),
    "3": Limits("3", max_depth=3, expand_threshold=0.4, max_entities=3500, max_requests=12000, max_minutes=30,
                max_runs=1600, connector_call_cap=120, search_query_cap=40),
    "4": Limits("4", max_depth=5, expand_threshold=0.25, max_entities=8000, max_requests=25000, max_minutes=60,
                max_runs=4000, connector_call_cap=250, search_query_cap=60, username_guesses=True),
}

PRESET_INFO = {
    "1": ("Direct results", "Only what the input itself reveals."),
    "2": ("Direct + related entities", "Also follows promising accounts, websites and identifiers they reveal."),
    "3": ("Deep correlation", "Two hops out with a lower threshold; slower, more noise to review."),
    "4": ("Aggressive expansion", "Full recursive investigation within the hard limits; includes clearly-labelled "
                                  "username guesses."),
}

HARD_CEILING = Limits("custom", max_depth=8, expand_threshold=0.0, max_entities=20000, max_requests=60000,
                      max_minutes=240, max_runs=10000, connector_call_cap=1000, search_query_cap=200, username_guesses=True)


def resolve_limits(depth: str | int | None, overrides: dict | None = None) -> Limits:
    depth = str(depth or "2")
    base = PRESETS.get(depth, PRESETS["2"])
    if overrides:
        custom = Limits.from_dict({**base.to_dict(), **overrides, "depth": "custom" if depth not in PRESETS else depth})
        base = replace(custom,
                       max_depth=min(custom.max_depth, HARD_CEILING.max_depth),
                       max_entities=min(custom.max_entities, HARD_CEILING.max_entities),
                       max_requests=min(custom.max_requests, HARD_CEILING.max_requests),
                       max_minutes=min(custom.max_minutes, HARD_CEILING.max_minutes),
                       max_runs=min(custom.max_runs, HARD_CEILING.max_runs),
                       connector_call_cap=min(custom.connector_call_cap, HARD_CEILING.connector_call_cap),
                       search_query_cap=min(custom.search_query_cap, HARD_CEILING.search_query_cap))
    return base


def presets_payload() -> list[dict]:
    return [{"id": k, "title": PRESET_INFO[k][0], "description": PRESET_INFO[k][1], **v.to_dict()} for k, v in PRESETS.items()]
