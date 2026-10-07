"""Username guesses derived from a name — a deliberately weak, clearly labelled hypothesis generator.

Runs at depth 4 (aggressive) or when the investigator approves "test name-derived handles" in the assistant, and only
for names with at least two tokens. The patterns live in ``assistant/names.py``. Guesses are marked ``hypothesis`` and
capped at a low investigative value: an account found under a guessed handle starts with no evidence at all, and the
resolver treats it accordingly.
"""

from __future__ import annotations

from ...assistant.names import candidates
from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from .._common import person_names

MAX_GUESSES = 4


def guesses(name: str, limit: int = MAX_GUESSES) -> list[str]:
    return [c.handle for c in candidates([name], limit)]


@register
class UsernameGuesses(Connector):
    manifest = Manifest(
        name="username_guesses", title="Username guesses from a name", category="usernames",
        description="Generates a few common handle patterns from a full name (firstlast, first.last, flast…). Used only at "
                    "depth 4; guesses are hypotheses, never evidence.",
        access="local", rate_limit=RateLimit(100, 100), website="https://en.wikipedia.org/wiki/User_(computing)#Username_format",
        reliability="low", official_api=False, access_method="local",
        capabilities=("candidate usernames (hypotheses)",), disclosure=Disclosure((), "nothing — runs locally", "none"),
    )

    @transform(inputs=("person",), outputs=("username",), title="Generate username guesses from the name", priority=0.2,
               accepts=lambda self, e, info: bool(info.option("username_guesses")) and e.is_seed and bool(person_names(e, info)),
               sends="nothing (local)")
    async def generate(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        src = ctx.out.source(None, title="Local username pattern generator", kind="local")
        limit = max(1, min(int(ctx.info.option("username_guess_count") or MAX_GUESSES), 12))
        for g in guesses(name, limit):
            u = ctx.out.entity("username", g, source=src, props={"username": g, "hypothesis": True, "derived_from": name},
                               evidence="Generated from the name — a guess, not evidence", confidence=0.3)
            ctx.out.link(entity.key, u, "ASSOCIATED_WITH", source=src, confidence=0.3,
                         evidence=f"Handle pattern generated from “{name}” (hypothesis)")
