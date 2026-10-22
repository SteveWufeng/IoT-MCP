"""Linux media control via ``playerctl`` (MPRIS over D-Bus).

The subprocess layer is faked with canned ``playerctl`` output, so these tests pin the
argument vectors, the player-selection rule, and — most importantly — the wording of the
errors an agent sees when there is no player to talk to.
"""

from __future__ import annotations

from typing import Any

import pytest

import iot_mcp.drivers.linux as linux_module
from iot_mcp.drivers.linux import LinuxDriver
from iot_mcp.errors import CommandFailedError, DependencyMissingError, NotFoundError

US = "\x1f"
METADATA_FORMAT = US.join(
    [
        "{xesam:title}",
        "{xesam:artist}",
        "{xesam:album}",
        "{xesam:url}",
        "{mpris:length}",
        "{xesam:artUrl}",
    ]
)


def driver_with(fake: Any, tools: tuple[str, ...] = ("playerctl",)) -> LinuxDriver:
    from conftest import make_which

    return LinuxDriver(
        environ={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "XDG_DATA_DIRS": "/none",
            "XDG_DATA_HOME": "/none",
        },
        which_fn=make_which(tools),
    )


# --- transport commands --------------------------------------------------------


async def test_play_pause_targets_the_playing_player(fake_run) -> None:
    fake = fake_run(
        linux_module,
        rules={
            "playerctl -l": "firefox\nspotify",
            "-p spotify -f {{status}} status": "Paused",
            "-p firefox -f {{status}} status": "Playing",
            "-p firefox play-pause": "",
        },
    )
    result = await driver_with(fake).media_play_pause()

    assert result["ok"] is True
    assert result["player"] == "firefox" and result["action"] == "play-pause"
    assert result["status"] == "Playing"
    assert fake.calls_for("-p firefox play-pause")[0].argv[-2:] == ("firefox", "play-pause")
    assert fake.calls_for("-p spotify play-pause") == []


async def test_next_and_previous_use_the_named_player(fake_run) -> None:
    fake = fake_run(
        linux_module,
        rules={
            "playerctl -l": "vlc",
            "-p vlc -f {{status}} status": "Playing",
            "-p vlc next": "",
            "-p vlc previous": "",
        },
    )
    driver = driver_with(fake)
    assert (await driver.media_next())["action"] == "next"
    assert (await driver.media_previous())["action"] == "previous"
    assert set(fake.programs()) == {"playerctl"}, "no helper other than playerctl may run"


async def test_the_precedence_is_playing_then_paused_then_anything(fake_run) -> None:
    """Broadcasting to every player would double-toggle a machine with two apps open."""
    fake = fake_run(
        linux_module,
        rules={
            "playerctl -l": "a\nb\nc",
            "-p a -f {{status}} status": "Stopped",
            "-p b -f {{status}} status": "Paused",
            "-p c -f {{status}} status": "Playing",
            "-p c play-pause": "",
        },
    )
    assert (await driver_with(fake).media_play_pause())["player"] == "c"

    fake2 = fake_run(
        linux_module,
        rules={
            "playerctl -l": "a\nb",
            "-p a -f {{status}} status": "Stopped",
            "-p b -f {{status}} status": "Paused",
            "-p b play-pause": "",
        },
    )
    assert (await driver_with(fake2).media_play_pause())["player"] == "b"


async def test_a_command_that_the_player_refuses_is_reported(fake_run) -> None:
    fake = fake_run(
        linux_module,
        rules={
            "playerctl -l": "vlc",
            "-p vlc -f {{status}} status": "Playing",
        },
    )
    fake.on("-p vlc next", "", code=1, stderr="Player doesn't support this action.")
    with pytest.raises(CommandFailedError) as excinfo:
        await driver_with(fake).media_next()
    assert excinfo.value.to_dict()["error"]["details"]["action"] == "next"


# --- no player / no tool -------------------------------------------------------


async def test_no_running_player_explains_what_to_start(fake_run) -> None:
    fake_run(linux_module, rules={"playerctl -l": ""})
    with pytest.raises(NotFoundError) as excinfo:
        await driver_with(None).media_next()
    hint = excinfo.value.hint
    assert "media player" in hint and "Spotify" in hint
    assert excinfo.value.to_dict()["error"]["details"]["players"] == []


async def test_media_status_without_a_player_is_a_normal_empty_answer(fake_run) -> None:
    fake_run(linux_module, rules={"playerctl -l": ""})
    result = await driver_with(None).media_status()
    assert result["ok"] is True
    assert result["playing"] is None and result["players"] == []
    assert "No MPRIS player" in result["note"]


async def test_missing_playerctl_is_a_dependency_error_with_a_package_hint(fake_run) -> None:
    fake_run(linux_module)
    driver = driver_with(None, tools=())
    matrix = driver.capability_matrix()
    for name in ("media.play_pause", "media.next", "media.previous", "media.status"):
        assert matrix["capabilities"][name]["supported"] is False
        assert matrix["capabilities"][name]["details"]["dependency"] == "playerctl"
    with pytest.raises(DependencyMissingError) as excinfo:
        await driver.media_next()
    assert "playerctl" in excinfo.value.hint


async def test_a_failing_playerctl_listing_is_not_silently_swallowed(fake_run) -> None:
    fake_run(linux_module, default=("", 1, "D-Bus not running"))
    with pytest.raises(CommandFailedError) as excinfo:
        await driver_with(None).media_status()
    assert "D-Bus" in str(excinfo.value)


# --- status --------------------------------------------------------------------


async def test_media_status_splits_metadata_on_the_unit_separator(fake_run) -> None:
    metadata = US.join(["MONTAGEM HIKARI", "DJ", "Singles", "file:///music.mp3", "214000000", ""])
    fake_run(
        linux_module,
        rules={
            "playerctl -l": "plasma-browser-integration",
            "-p plasma-browser-integration status": "Playing",
            "metadata": metadata,
        },
    )
    result = await driver_with(None).media_status()

    assert result["playing"]["player"] == "plasma-browser-integration"
    assert result["playing"]["title"] == "MONTAGEM HIKARI"
    assert result["playing"]["artist"] == "DJ"
    assert result["playing"]["length"] == "214000000"
    assert result["playing"]["status"] == "Playing"


async def test_a_title_containing_a_dash_survives_parsing(fake_run) -> None:
    """The default playerctl format would be ambiguous here; the explicit format is not."""
    metadata = US.join(["A - B - C", "Artist - Name", "", "", "", ""])
    fake_run(
        linux_module,
        rules={
            "playerctl -l": "mpv",
            "-p mpv status": "Playing",
            "metadata": metadata,
        },
    )
    entry = (await driver_with(None).media_status())["playing"]
    assert entry["title"] == "A - B - C" and entry["artist"] == "Artist - Name"


async def test_an_idle_player_is_still_reported(fake_run) -> None:
    """``playerctl status`` exits non-zero for a present-but-idle player; that is data."""
    fake = fake_run(linux_module, rules={"playerctl -l": "mpv"}, default=("", 1, ""))
    fake.on("-p mpv status", "", code=1)
    result = await driver_with(None).media_status()
    assert result["players"][0]["status"] == "Unknown"
    assert result["playing"] is None


async def test_every_player_up_to_a_bound_is_listed(fake_run) -> None:
    players = [f"player{i}" for i in range(20)]
    fake_run(
        linux_module,
        rules={"playerctl -l": "\n".join(players), "status": "Playing", "metadata": ""},
    )
    result = await driver_with(None).media_status()
    assert len(result["players"]) == 8, "status must stay bounded on a busy machine"


async def test_the_media_commands_never_route_through_a_shell(fake_run) -> None:
    fake = fake_run(
        linux_module,
        rules={
            "playerctl -l": "vlc",
            "-p vlc -f {{status}} status": "Playing",
            "-p vlc play-pause": "",
        },
    )
    await driver_with(fake).media_play_pause()
    assert set(fake.programs()) == {"playerctl"}
    for call in fake.calls:
        assert call.argv[0] == "/usr/bin/playerctl"
        assert "-c" not in call.argv
        assert call.timeout is not None, "every subprocess must carry a deadline"
