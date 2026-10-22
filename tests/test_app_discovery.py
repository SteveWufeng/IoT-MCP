"""Desktop-entry discovery and app matching (Linux's app backend)."""

from __future__ import annotations

from pathlib import Path

import pytest

from iot_mcp.drivers._xdg import (
    DesktopApp,
    discover_apps,
    match_app,
    parse_desktop_entry,
    strip_field_codes,
    xdg_app_dirs,
)
from iot_mcp.errors import InvalidInputError, NotFoundError

FIREFOX = """
[Desktop Entry]
Type=Application
Name=Firefox Web Browser
GenericName=Web Browser
Comment=Browse the World Wide Web
Exec=firefox %u
Icon=firefox
TryExec=firefox
Terminal=false
StartupWMClass=firefox
Categories=Network;WebBrowser;
MimeType=text/html;
Name[de]=Firefox Webbrowser
"""


# --- parsing -------------------------------------------------------------------


def test_a_well_formed_entry_becomes_a_typed_app() -> None:
    app = parse_desktop_entry(
        FIREFOX, app_id="firefox.desktop", path="/usr/share/applications/firefox.desktop"
    )
    assert app is not None
    assert app.id == "firefox.desktop"
    assert app.name == "Firefox Web Browser"
    assert app.exec_line == "firefox %u"
    assert app.try_exec == "firefox"
    assert app.desktop_file_id == "firefox"
    assert app.categories == ("Network", "WebBrowser")


def test_localised_names_do_not_overwrite_the_default_name() -> None:
    """``Name[de]`` must not win over ``Name`` just because it sorts first in the file."""
    text = FIREFOX.replace("Name=Firefox Web Browser", "Name[de]=Deutsch\nName=Firefox Web Browser")
    app = parse_desktop_entry(text, app_id="firefox")
    assert app is not None and app.name == "Firefox Web Browser"


def test_entries_without_a_name_are_dropped_rather_than_invented() -> None:
    assert parse_desktop_entry("[Desktop Entry]\nType=Application\nExec=x\n") is None


def test_no_display_and_hidden_entries_are_dropped() -> None:
    for key in ("NoDisplay", "Hidden"):
        assert parse_desktop_entry(f"[Desktop Entry]\nName=X\n{key}=true\n") is None
    assert parse_desktop_entry("[Desktop Entry]\nName=X\nNoDisplay=false\n") is not None


def test_only_the_desktop_entry_group_is_read() -> None:
    text = "[Desktop Entry]\nName=Real\nExec=real\n\n[Desktop Action New]\nName=New Window\n"
    app = parse_desktop_entry(text, app_id="real")
    assert app is not None and app.name == "Real" and app.exec_line == "real"


def test_a_file_with_no_group_at_all_is_not_an_app() -> None:
    assert parse_desktop_entry("Name=Impostor\nExec=x\n") is None


def test_junk_input_never_raises() -> None:
    for text in ("", "\n\n", "[]", "=", "Name", "[Desktop Entry", "\x00\x01\x02"):
        assert parse_desktop_entry(text) is None or isinstance(
            parse_desktop_entry(text), DesktopApp
        )


# --- Exec handling (the no-shell guarantee) ------------------------------------


@pytest.mark.parametrize(
    "exec_line,expected",
    [
        ("firefox %u", ["firefox"]),
        ("firefox --new-window %U", ["firefox", "--new-window"]),
        ("code --disable-gpu %F %f", ["code", "--disable-gpu"]),
        ('sh -c "echo hello world"', ["sh", "-c", "echo hello world"]),
        ('"/opt/My App/bin/run" --flag %U', ["/opt/My App/bin/run", "--flag"]),
        ("app %% literal", ["app", "%", "literal"]),
        ("viewer file:%U", ["viewer", "file:"]),
    ],
)
def test_field_codes_are_dropped_and_quoting_is_respected(
    exec_line: str, expected: list[str]
) -> None:
    assert strip_field_codes(exec_line) == expected


def test_an_unquoted_space_in_an_exec_path_splits_like_any_other_argv() -> None:
    """Documented shlex behaviour: we never re-join or shell-interpret, a bad entry is
    simply a bad entry, and the failure is an exec-level FileNotFoundError with the argv
    in its details."""
    assert strip_field_codes("/opt/My App/bin/run") == ["/opt/My", "App/bin/run"]


def test_field_codes_never_reach_the_argument_vector() -> None:
    """A ``%U`` passed through literally would be a filename argument; dropped is correct."""
    argv = strip_field_codes("editor %U %F %u %f %d %D %n %N %i %c %k")
    assert argv == ["editor"]


def test_an_executable_that_is_only_a_field_code_yields_nothing() -> None:
    assert strip_field_codes("%U") == []
    assert strip_field_codes("   ") == []


def test_unbalanced_quotes_are_reported_not_silently_mangled() -> None:
    with pytest.raises(ValueError):
        strip_field_codes('sh -c "unterminated')


def test_shell_metacharacters_stay_inside_one_argument() -> None:
    """Nothing here is interpreted: the vector goes to execvp, so this is inert text."""
    argv = strip_field_codes("app --msg='; rm -rf /'")
    assert argv == ["app", "--msg=; rm -rf /"]


# --- search path ---------------------------------------------------------------


def test_xdg_dirs_follow_the_spec_priority_order() -> None:
    dirs = xdg_app_dirs({"HOME": "/home/tester", "XDG_DATA_DIRS": "/opt/share:/usr/share"})
    assert [str(path) for path in dirs] == [
        "/home/tester/.local/share/applications",
        "/opt/share/applications",
        "/usr/share/applications",
    ]


def test_xdg_data_home_overrides_the_home_fallback() -> None:
    dirs = xdg_app_dirs(
        {"HOME": "/home/tester", "XDG_DATA_HOME": "/custom", "XDG_DATA_DIRS": "/usr/share"}
    )
    assert str(dirs[0]) == "/custom/applications"


def test_duplicate_and_empty_path_entries_are_collapsed() -> None:
    dirs = xdg_app_dirs(
        {
            "HOME": "/home/tester",
            "XDG_DATA_HOME": "/usr/share",
            "XDG_DATA_DIRS": ":/usr/share::/usr/share",
        }
    )
    assert [str(path) for path in dirs] == ["/usr/share/applications"]


# --- discovery -----------------------------------------------------------------


def test_discovery_finds_nested_entries_and_sorts_by_name(desktop_entries, tmp_path: Path) -> None:
    root = desktop_entries(
        {
            "zeta": {"Name": "Zeta", "Exec": "zeta"},
            "alpha": {"Name": "Alpha", "Exec": "alpha"},
        },
        subdir="applications/extras",
    )
    apps = discover_apps([root / "applications"])
    assert [app.name for app in apps] == ["Alpha", "Zeta"]


def test_a_higher_priority_directory_shadows_the_same_id(desktop_entries) -> None:
    home = desktop_entries(
        {"firefox": {"Name": "Home Firefox", "Exec": "home-firefox"}}, subdir="applications"
    )
    other_root = home.parent / "other"
    other = other_root / "applications"
    other.mkdir(parents=True)
    (other / "firefox.desktop").write_text(
        "[Desktop Entry]\nName=System Firefox\nExec=system-firefox\n"
    )
    apps = discover_apps([home / "applications", other])
    assert [app.name for app in apps] == ["Home Firefox"]


def test_malformed_files_are_skipped_without_failing_the_scan(desktop_entries) -> None:
    root = desktop_entries(
        {"good": {"Name": "Good", "Exec": "good"}},
        subdir="applications",
    )
    apps_dir = root / "applications"
    (apps_dir / "broken.desktop").write_text("this is not an entry at all")
    (apps_dir / "nameless.desktop").write_text("[Desktop Entry]\nExec=x\n")
    (apps_dir / "not-an-entry.txt").write_text("[Desktop Entry]\nName=Wrong Extension\n")
    assert [app.name for app in discover_apps([apps_dir])] == ["Good"]


def test_the_limit_caps_the_result(desktop_entries) -> None:
    root = desktop_entries(
        {f"app{i:02d}": {"Name": f"App {i:02d}", "Exec": f"app{i:02d}"} for i in range(30)},
        subdir="applications",
    )
    assert len(discover_apps([root / "applications"], limit=10)) == 10


def test_missing_and_unreadable_directories_are_not_errors(tmp_path: Path) -> None:
    assert discover_apps([tmp_path / "nope", Path("/proc/1/root/forbidden")]) == []


def test_recursion_depth_is_bounded(desktop_entries) -> None:
    root = desktop_entries({"deep": {"Name": "Deep", "Exec": "deep"}}, subdir="applications/a/b/c")
    assert discover_apps([root / "applications"], recurse=1) == []
    assert len(discover_apps([root / "applications"], recurse=4)) == 1


# --- matching ------------------------------------------------------------------

APPS = [
    DesktopApp(
        id="org.mozilla.firefox",
        name="Firefox Web Browser",
        exec_line="firefox",
        desktop_file_id="firefox",
    ),
    DesktopApp(
        id="firefoxdeveloperdition", name="Firefox Developer Edition", exec_line="firefox-dev"
    ),
    DesktopApp(id="thunderbird", name="Thunderbird", exec_line="thunderbird"),
]


def test_exact_id_beats_everything() -> None:
    assert match_app("org.mozilla.firefox", APPS).id == "org.mozilla.firefox"


def test_exact_name_wins_over_a_prefix_rival() -> None:
    assert match_app("Thunderbird", APPS).id == "thunderbird"


def test_punctuation_insensitive_id_match() -> None:
    assert match_app("orgmozilla firefox".replace(" ", "."), APPS).id == "org.mozilla.firefox"


def test_prefix_and_substring_ranking() -> None:
    assert match_app("firefox", APPS).id == "org.mozilla.firefox"  # exact wm class / prefix
    assert match_app("developer", APPS).id == "firefoxdeveloperdition"
    assert match_app("thunder", APPS).id == "thunderbird"


def test_a_sentence_containing_the_full_name_still_finds_the_app() -> None:
    """The weakest tier: the whole name has to appear in the caller's sentence."""
    assert match_app("please open the Firefox Web Browser for me", APPS).id == "org.mozilla.firefox"


def test_no_match_explains_itself_with_real_candidates() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        match_app("gimp-does-not-exist", APPS)
    details = excinfo.value.to_dict()["error"]["details"]
    assert details["candidates"] == [
        "Firefox Web Browser",
        "Firefox Developer Edition",
        "Thunderbird",
    ]
    assert details["scanned_apps"] == 3
    assert "list_apps()" in excinfo.value.hint


@pytest.mark.parametrize("query", ["", "   ", "\t"])
def test_an_empty_query_is_an_input_error_not_a_search(query: str) -> None:
    with pytest.raises(InvalidInputError):
        match_app(query, APPS)
