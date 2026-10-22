"""URL validation shared by every backend.

``open_url`` is deliberately unauthenticated in this MVP, so its argument originates with a
model that may have been steered by prompt injection in page content. The check lives here,
once, rather than in each driver, so no backend can forget it.

Policy: allow only schemes whose worst realistic outcome is "a document was opened in the
user's browser". ``file://``, ``jar:``, ``autofs:``, custom app schemes and
``javascript:`` are refused, and credentials embedded in a URL are refused.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..capabilities import Capability
from ..errors import InvalidInputError

__all__ = ["ALLOWED_URL_SCHEMES", "validate_url"]

#: Schemes ``open_url`` will hand to the desktop's default handler.
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto", "ftp", "ftps"})

#: Schemes that are explicitly named in the rejection hint because they are the ones
#: injection attempts actually reach for.
_BLOCKED_EXAMPLES = ("file", "javascript", "data", "jar")


def validate_url(url: str, *, capability: Capability | str = Capability.URL_OPEN) -> str:
    """Validate ``url`` and return it ready to pass to ``open``/``xdg-open``.

    Args:
        url: Caller-supplied URL.
        capability: Capability recorded on any raised error.

    Returns:
        The URL, stripped of surrounding whitespace. Always absolute with a scheme.

    Raises:
        InvalidInputError: Empty, oversized, unparsable, credential-bearing, hostless for a
            host-bearing scheme, or outside :data:`ALLOWED_URL_SCHEMES`.
    """
    capability_value = str(capability)
    if not isinstance(url, str):
        # The MCP layer's JSON schema already guarantees a string, but a driver can also be
        # called directly (tests, future in-process callers); a clear rejection beats an
        # AttributeError escaping as a generic internal error.
        raise InvalidInputError(
            f"url must be a string, got {type(url).__name__}",
            capability=capability_value,
            hint="Pass a URL string such as https://example.com",
        )
    candidate = url.strip()
    if not candidate:
        raise InvalidInputError(
            "url must not be empty",
            capability=capability_value,
            hint="Pass an absolute URL such as https://example.com",
        )
    if len(candidate) > 2048:
        raise InvalidInputError(
            f"url is {len(candidate)} characters; the limit is 2048",
            capability=capability_value,
        )
    if any(char.isspace() for char in candidate):
        raise InvalidInputError(
            "url contains whitespace",
            capability=capability_value,
            hint="Percent-encode spaces as %20 instead.",
        )
    if any(not char.isprintable() for char in candidate):
        # Control characters never appear in a real URL but are a classic way to smuggle a
        # second token or terminate an argument past a launcher's quoting.
        raise InvalidInputError(
            "url contains control characters",
            capability=capability_value,
            hint="Remove the control bytes; percent-encode any character you actually need.",
        )
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidInputError(
            f"url is not parseable: {exc}", capability=capability_value
        ) from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise InvalidInputError(
            f"scheme {scheme or '(none)'!r} is not allowed",
            capability=capability_value,
            hint=(
                f"Allowed: {', '.join(sorted(ALLOWED_URL_SCHEMES))}. Local or executable "
                f"schemes ({', '.join(_BLOCKED_EXAMPLES)}) are refused on purpose."
            ),
            details={"scheme": scheme},
        )
    if parts.username or parts.password:
        raise InvalidInputError(
            "credentials embedded in a URL are refused",
            capability=capability_value,
            hint="Use a plain https:// URL; the desktop launcher cannot authenticate.",
        )
    if scheme in {"http", "https", "ftp", "ftps"} and not parts.netloc:
        raise InvalidInputError(f"{candidate!r} has no host", capability=capability_value)
    return candidate
