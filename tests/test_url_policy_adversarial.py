"""Adversarial cases for the single URL policy behind ``open_url``.

The server is unauthenticated: the URL is whatever an agent (possibly steered by
prompt-injected page content) asks for. :func:`iot_mcp.drivers._url.validate_url` is
the one gate, so it gets the classic scheme-smuggling, control-character, credential
and size corpus — plus proof that whatever survives the gate reaches ``xdg-open`` as
exactly ONE argv token (no shell, no splitting).

Existing ``test_url_policy.py`` covers the policy's sunny day; this module attacks it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import iot_mcp.drivers.linux as linux_module
from iot_mcp.drivers._url import validate_url
from iot_mcp.drivers.linux import LinuxDriver
from iot_mcp.errors import InvalidInputError

# ------------------------------------------------------------- refused corpus

REFUSED = [
    pytest.param("file:///etc/passwd", id="file-absolute"),
    pytest.param("FILE:///C:/Windows/win.ini", id="file-uppercase"),
    pytest.param("file:/etc/passwd", id="file-single-slash"),
    pytest.param("javascript:alert(1)", id="javascript"),
    pytest.param("JaVaScRiPt:alert(1)", id="javascript-mixed-case"),
    pytest.param("data:text/html,<script>alert(1)</script>", id="data-html"),
    pytest.param("jar:http://evil/x.jar!/entry", id="jar"),
    pytest.param("vbscript:msgbox(1)", id="vbscript"),
    pytest.param("about:config", id="about"),
    pytest.param("chrome://settings", id="chrome"),
    pytest.param("sftp://host/file", id="sftp"),
    pytest.param("gopher://host:70/x", id="gopher"),
    pytest.param("afp://share/vol", id="afp"),
    pytest.param("//etc/passwd", id="scheme-relative"),
    pytest.param("http:\\\\evil.example\\x", id="backslash-authority"),
    pytest.param("http://", id="http-no-host"),
    pytest.param("ftp://", id="ftp-no-host"),
    pytest.param("", id="empty"),
    pytest.param("   \t ", id="whitespace-only"),
    pytest.param("http://evil.example\r\nX-Injected: 1", id="crlf-injection"),
    pytest.param("http://evil.example\t/tab", id="tab-inside"),
    pytest.param("http://evil.example\x00nul", id="nul-inside"),
    pytest.param("http://evil.example\x0bvt", id="vertical-tab-inside"),
    pytest.param("http://evil.example\x1b[31m", id="ansi-escape"),
    pytest.param("http://evil.example\u2028line-sep", id="unicode-line-separator"),
    pytest.param("http://user:pw@evil.example/steal", id="credentials"),
    pytest.param("http://" + "a" * 4096 + ".example", id="oversized"),
]


@pytest.mark.parametrize("url", REFUSED)
def test_refused_corpus(url: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_url(url)


# -------------------------------------------------------------- accepted corpus
# Pinned deliberately: each is by-design or measured-inert. The report discusses
# the two judgement calls (link-local, IDN).

ACCEPTED = [
    pytest.param("http://example.com", "http://example.com", id="plain"),
    pytest.param("HTTPS://EXAMPLE.COM", "HTTPS://EXAMPLE.COM", id="uppercase-scheme-kept"),
    pytest.param("mailto:someone@example.com", "mailto:someone@example.com", id="mailto"),
    pytest.param("ftp://files.example/pub", "ftp://files.example/pub", id="ftp"),
    pytest.param(
        "https://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        id="link-local-pinned-allowed",
    ),
    pytest.param("http://[::1]:8080/x", "http://[::1]:8080/x", id="ipv6-loopback"),
    pytest.param(
        "http://127.0.0.1:631/printers", "http://127.0.0.1:631/printers", id="cups-loopback"
    ),
    pytest.param("http://ex\u0430mple.com", "http://ex\u0430mple.com", id="idn-confusable-pinned"),
    pytest.param(
        "http://example.com/%00", "http://example.com/%00", id="percent-nul-is-literal-data"
    ),
    pytest.param(
        "http://example.com/a?b=c&d=%26", "http://example.com/a?b=c&d=%26", id="query-ampersand"
    ),
    pytest.param("http://example.com/x#frag ment"[:26], "http://example.com/x#frag", id="fragment"),
]


@pytest.mark.parametrize("url,expected", ACCEPTED)
def test_accepted_corpus(url: str, expected: str) -> None:
    assert validate_url(url) == expected


def test_surrounding_control_and_space_is_stripped_not_smuggled() -> None:
    """Leading/trailing whitespace AND \\x0b are stripped by str.strip(); what is
    left is a clean URL. Inert — but noted so a future refactor keeps the strip."""
    assert validate_url(" \t\x0bhttp://example.com\x0b \n") == "http://example.com"


def test_size_boundary_is_exact() -> None:
    ok_url = "http://example.com/" + "a" * (2048 - len("http://example.com/"))
    assert validate_url(ok_url) == ok_url
    with pytest.raises(InvalidInputError):
        validate_url(ok_url + "a")


def test_non_string_is_refused_clearly() -> None:
    """Direct callers bypass the MCP schema; the gate still refuses typed garbage."""
    for hostile in ({"url": "http://x"}, ["http://x"], 42, None, b"http://x"):
        with pytest.raises(InvalidInputError):
            validate_url(hostile)  # type: ignore[arg-type]


# ------------------------------------------------------------ argv integrity


def url_driver(known: tuple[str, ...] = ("xdg-open",)) -> LinuxDriver:
    return LinuxDriver(
        environ={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "XDG_DATA_HOME": "/none",
            "XDG_DATA_DIRS": "/none",
        },
        which_fn=lambda name: f"/usr/bin/{name}" if name in known else None,
        proc_root=Path("/nonexistent-proc"),
    )


async def test_an_accepted_url_reaches_xdg_open_as_exactly_one_token(
    fake_run,
) -> None:
    """The policy's output is only ever one argv element: query characters like
    ``&`` or ``;`` never split a token and never introduce a shell."""
    fake = fake_run(linux_module)
    driver = url_driver()
    hostile = "http://evil.example/path?x=1;rm%20-rf%20/&y=$(id)#`tick`"
    await driver.open_url(hostile)
    spawn_call, _ = fake.spawns[0]
    assert spawn_call.argv == ("/usr/bin/xdg-open", hostile)
    assert "/bin/sh" not in spawn_call.argv and "-c" not in spawn_call.argv


async def test_a_mailto_reaches_the_launcher_untouched(fake_run) -> None:
    fake = fake_run(linux_module)
    await url_driver().open_url("mailto:victim@example.com?subject=hi")
    spawn_call, _ = fake.spawns[0]
    assert spawn_call.argv == ("/usr/bin/xdg-open", "mailto:victim@example.com?subject=hi")


async def test_refused_urls_spawn_nothing(fake_run) -> None:
    """Defense order: policy first, launcher second — a refusal cannot touch the
    process layer at all."""
    fake = fake_run(linux_module)
    driver = url_driver()
    for refused in ("file:///etc/shadow", "javascript:evil()", "http://a\x00b"):
        with pytest.raises(InvalidInputError):
            await driver.open_url(refused)
    assert fake.spawns == [] and fake.calls == []
