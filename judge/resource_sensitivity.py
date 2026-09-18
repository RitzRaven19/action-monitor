"""Rule-based resource sensitivity scoring (v5).

detect_persistent_scope_creep (v4) counts distinct out-of-scope resources with
every resource weighted equally -- which is exactly why it fires as a false
positive on a long, entirely legitimate history just as readily as on a real
attack (see docs/EVASION_FINDINGS.md). This changes what gets counted: instead
of "how many different things," weight by "how sensitive are the things,"
so an agent that reads a dozen mundane files over its lifetime doesn't
accumulate the same signal as one that touches even a couple of resources
that look like credentials or personal data.

Deliberately simple keyword matching, not a classifier -- consistent with the
project's stated preference for inspectable, explainable rules over another
opaque layer (Section 4.3 of the original design doc). Its own honest limit:
anything sensitive whose name doesn't match a keyword here is invisible to it.
"""
from __future__ import annotations

HIGH_SENSITIVITY_KEYWORDS = ("credential", "password", "passwd", "secret", "api_key", "apikey", "token", "private_key")
MEDIUM_SENSITIVITY_KEYWORDS = ("roster", "personal", "pii", "salary", "ssn", "social_security", "employee", "medical", "payroll")

HIGH_SENSITIVITY_SCORE = 3.0
MEDIUM_SENSITIVITY_SCORE = 1.5
DEFAULT_SCORE = 1.0


def sensitivity(resource: str) -> float:
    """Score a resource path/URL by how sensitive it looks, purely from its name."""
    resource_lower = resource.lower()
    if any(kw in resource_lower for kw in HIGH_SENSITIVITY_KEYWORDS):
        return HIGH_SENSITIVITY_SCORE
    if any(kw in resource_lower for kw in MEDIUM_SENSITIVITY_KEYWORDS):
        return MEDIUM_SENSITIVITY_SCORE
    return DEFAULT_SCORE
