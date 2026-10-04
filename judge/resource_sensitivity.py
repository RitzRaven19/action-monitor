"""Scores a resource by how sensitive its name looks.

Used by the weighted history check: a couple of credential files should count
for more than a dozen meeting notes. Plain keyword matching, so anything
sensitive with an ordinary-looking name scores like everything else.
"""
from __future__ import annotations

HIGH_SENSITIVITY_KEYWORDS = (
    "credential", "password", "passwd", "secret", "api_key", "apikey", "token", "private_key",
    # what a coding agent can actually reach on a developer machine
    ".ssh/", "id_rsa", "id_ed25519", "/.env", ".aws/", ".netrc", ".pgpass", ".npmrc", ".pypirc",
)
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
