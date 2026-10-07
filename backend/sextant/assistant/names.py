"""Names → candidate usernames. Hypotheses, never evidence.

People rarely use the same handle everywhere, and nobody's handle can be derived from their name. What can be derived is
a short, ranked list of *common patterns* (``first.last``, ``flast``, ``lastfirstm``…) to test against platforms that
answer whether an account exists. Every candidate says which pattern and which reading of the name produced it, and is
labelled *Generated candidate · Not verified* until a source reports an account. Even then, an account under a generated
handle starts with no identity evidence at all: the resolver scores it like any stranger's account.

Deliberate limits:

* Name order is ambiguous (``Mira Fictus Exempla`` may be first-last-middle or last-first-middle), so both readings
  are generated, each labelled with the reading it assumes.
* No numbers are invented. The year of birth is local-only and is never put into a handle, because testing a handle sends
  it to every platform checked.
* No nickname dictionary: nicknames are culture-specific guesses. Aliases the investigator types are used as given.
* Single first or last names alone are not candidates: thousands of people use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..registry import platforms as P
from ..util.normalize import fold, name_tokens, username_uniqueness

# (pattern id, title, builder(first, last, middle) -> handle, prior)
PATTERNS: tuple[tuple[str, str, object, float], ...] = (
    ("first.last", "first.last", lambda f, l, m: f"{f}.{l}", 1.0),
    ("firstlast", "firstlast", lambda f, l, m: f"{f}{l}", 0.95),
    ("first_last", "first_last", lambda f, l, m: f"{f}_{l}", 0.9),
    ("flast", "first initial + last", lambda f, l, m: f"{f[0]}{l}", 0.85),
    ("lastfirst", "lastfirst", lambda f, l, m: f"{l}{f}", 0.7),
    ("first-last", "first-last", lambda f, l, m: f"{f}-{l}", 0.6),
    ("last.first", "last.first", lambda f, l, m: f"{l}.{f}", 0.55),
    ("last_first", "last_first", lambda f, l, m: f"{l}_{f}", 0.5),
    ("firstl", "first + last initial", lambda f, l, m: f"{f}{l[0]}", 0.45),
    ("fmlast", "initials + last", lambda f, l, m: f"{f[0]}{m[0]}{l}" if m else "", 0.45),
    ("firstmlast", "first + middle initial + last", lambda f, l, m: f"{f}{m[0]}{l}" if m else "", 0.45),
    ("lastfirstm", "last + first + middle initial", lambda f, l, m: f"{l}{f}{m[0]}" if m else "", 0.4),
    ("lastf", "last + first initial", lambda f, l, m: f"{l}{f[0]}", 0.35),
)
MIN_LEN, MAX_LEN = 5, 30
STATUS = "Generated candidate · Not verified"


@dataclass
class Reading:
    """One interpretation of which token is the first name, the last name and the middle name."""
    first: str
    last: str
    middle: str = ""
    title: str = ""
    prior: float = 1.0


@dataclass
class Candidate:
    handle: str
    pattern: str
    pattern_title: str
    reading: str
    source_name: str
    score: float
    uniqueness: float
    platforms_valid: list[str] = field(default_factory=list)
    status: str = STATUS

    def to_dict(self) -> dict:
        return {"handle": self.handle, "pattern": self.pattern, "pattern_title": self.pattern_title,
                "reading": self.reading, "from_name": self.source_name, "score": round(self.score, 3),
                "uniqueness": round(self.uniqueness, 2), "platforms_valid": self.platforms_valid[:12],
                "platforms_valid_count": len(self.platforms_valid), "status": self.status, "kind": "hypothesis",
                "explanation": f"Pattern “{self.pattern_title}” applied to {self.reading} of “{self.source_name}”. "
                               "A guess to test, not evidence."}


def readings(name: str) -> list[Reading]:
    """Plausible token-role readings of a full name, most common first."""
    toks = [t for t in name_tokens(fold(name)) if t.isalpha()]
    if len(toks) < 2:
        return []
    if len(toks) == 2:
        a, b = toks
        return [Reading(a, b, "", f"first “{a}”, last “{b}”", 1.0),
                Reading(b, a, "", f"first “{b}”, last “{a}” (family name first)", 0.6)]
    a, b, c = toks[0], toks[1], toks[-1]
    return [Reading(a, c, b, f"first “{a}”, middle “{b}”, last “{c}”", 1.0),
            Reading(a, b, c, f"first “{a}”, last “{b}”, middle “{c}”", 0.85),
            Reading(b, a, c, f"first “{b}”, last “{a}” (family name first)", 0.6)]


def candidates(names: list[str], limit: int = 12) -> list[Candidate]:
    """Ranked candidate handles for the given full names (the target's name and aliases the investigator typed)."""
    best: dict[str, Candidate] = {}
    for n_idx, name in enumerate(names):
        for r in readings(name):
            for pid, title, build, prior in PATTERNS:
                h = build(r.first, r.last, r.middle)  # type: ignore[operator]
                if not h or not MIN_LEN <= len(h) <= MAX_LEN:
                    continue
                uniq = username_uniqueness(h, [r.first, r.last, r.middle])
                score = prior * r.prior * (0.8 ** n_idx) * (0.6 + 0.4 * uniq)
                cur = best.get(h)
                if cur is None or score > cur.score:
                    best[h] = Candidate(h, pid, title, r.title, name, score, uniq)
    ranked = sorted(best.values(), key=lambda c: (-c.score, c.handle))[:limit]
    for c in ranked:
        c.platforms_valid = platforms_accepting(c.handle)
    return ranked


def platforms_accepting(handle: str) -> list[str]:
    """Curated platforms whose username rules accept this handle and that Sextant can check automatically."""
    out = []
    for p in P.curated():
        if p.automated and p.status != "defunct" and p.valid_username(handle):
            out.append(p.name)
    return out
