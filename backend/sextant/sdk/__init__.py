"""Connector Development Kit — everything needed to write connector #1 or #1000.

    from sextant.sdk import Connector, Manifest, Credential, Disclosure, RateLimit, register, transform
"""

from .context import TransformContext
from .errors import (
    Blocked, BudgetExceeded, NeedsCredentials, NotFound, RateLimited, Skipped, SourceUnavailable, TransformError,
    VerificationRequired,
)
from .http import FetchError, HttpResponse
from .manifest import CATEGORIES, Credential, Disclosure, Manifest, RateLimit
from .result import TransformResult
from .transform import CatalogConnector, Connector, EntityView, InvestigationInfo, Transform, transform


def register(obj):
    """Registers a connector class (decorator) — see ``sextant.registry.connectors.register``."""
    from ..registry.connectors import register as _register
    return _register(obj)


def register_transform(tcls):
    from ..registry.connectors import register_transform as _register_transform
    return _register_transform(tcls)


__all__ = [
    "Blocked", "BudgetExceeded", "CATEGORIES", "CatalogConnector", "Connector", "Credential", "Disclosure",
    "EntityView", "FetchError", "HttpResponse", "InvestigationInfo", "Manifest", "NeedsCredentials", "NotFound",
    "RateLimit", "RateLimited", "Skipped", "SourceUnavailable", "Transform", "TransformContext", "TransformError",
    "VerificationRequired",
    "TransformResult", "register", "register_transform", "transform",
]
