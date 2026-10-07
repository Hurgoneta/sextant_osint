"""Exceptions a transform raises to end a run with an honest, specific status.

The status matters for coverage: "blocked" or "needs credentials" must never be shown as
"nothing found".
"""

from __future__ import annotations


class TransformError(Exception):
    status = "error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class NotFound(TransformError):
    """The source answered authoritatively: no such account/record."""
    status = "not_found"


class NeedsCredentials(TransformError):
    status = "needs_credentials"


class Blocked(TransformError):
    """The source refused automated access (403, CAPTCHA, bot challenge). Never retried around."""
    status = "blocked"


class VerificationRequired(Blocked):
    """The source answered with a CAPTCHA / bot check / browser challenge. Sextant never tries to get past it: the run
    ends as *blocked* and the investigator gets the page to open themselves."""

    def __init__(self, message: str = "", url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class RateLimited(TransformError):
    status = "rate_limited"


class SourceUnavailable(TransformError):
    """Outage, unexpected response, or the source no longer works the way the connector expects."""
    status = "error"


class BudgetExceeded(TransformError):
    status = "budget_exhausted"


class Skipped(TransformError):
    """Not applicable after all (e.g. username format not valid on this platform)."""
    status = "skipped"
