"""URL policy — the one place that decides what the server is allowed to launch."""

from __future__ import annotations

import pytest

from iot_mcp.drivers._url import ALLOWED_URL_SCHEMES, validate_url
from iot_mcp.errors import InvalidInputError


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?a=1&b=2#frag",
        "HTTPS://EXAMPLE.COM",
        "mailto:someone@example.com?subject=hi",
        "ftp://example.com/file",
        "ftps://example.com/file",
    ],
)
def test_allowed_schemes_pass_through_normalised(url: str) -> None:
    assert validate_url(url) == url.strip()


def test_surrounding_whitespace_is_stripped_not_rejected() -> None:
    assert validate_url("  https://example.com\n") == "https://example.com"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "FILE:///etc/passwd",
        "javascript:alert(1)",
        "jar:https://example.com!/x",
        "chrome://settings",
        "smb://server/share",
        "ssh://host",
        "ftp+ssh://host",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_dangerous_or_unexpected_schemes_are_refused(url: str) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        validate_url(url)
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.hint


def test_the_allowlist_is_explicit_about_mail_and_files() -> None:
    """``file://`` is the one people ask for; the error must explain why it is refused."""
    assert "file" not in ALLOWED_URL_SCHEMES
    assert "mailto" in ALLOWED_URL_SCHEMES
    with pytest.raises(InvalidInputError) as excinfo:
        validate_url("file:///home/tester/.ssh/id_ed25519")
    assert "file" in excinfo.value.hint


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com",  # no scheme at all
        "://missing-scheme",
        "https://",  # no host
        "https://exa mple.com",
    ],
)
def test_malformed_and_empty_urls_are_refused(url: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_url(url)


def test_credentials_in_a_url_are_refused() -> None:
    """``https://host@evil/`` renders as ``host`` in many UIs; refuse the ambiguity."""
    with pytest.raises(InvalidInputError):
        validate_url("https://example.com@evil.test/")


def test_control_characters_cannot_smuggle_a_second_argument() -> None:
    for url in (
        "https://example.com\n--command",
        "https://example.com\x00evil",
        "https://exa\tmple.com",
    ):
        with pytest.raises(InvalidInputError):
            validate_url(url)


def test_non_string_input_is_refused_before_any_parsing() -> None:
    for value in (None, 42, ["https://example.com"], b"https://example.com"):
        with pytest.raises(InvalidInputError):
            validate_url(value)  # type: ignore[arg-type]


def test_the_error_names_the_capability_being_enforced() -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        validate_url("file:///x", capability="url.open")
    assert excinfo.value.capability == "url.open"
    assert excinfo.value.to_dict()["error"]["details"]["scheme"] == "file"
