"""On-screen narration: spec validation, script escaping, and report recovery."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from iot_mcp.annotation import (
    ANNOTATE_CLEAR_TOOL,
    ANNOTATE_TOOL,
    ANNOTATION_INSTRUCTIONS,
    CARD_ID,
    MAX_NOTE_CHARS,
    MAX_TARGETS,
    RING_ATTR,
    annotation_script,
    annotation_spec,
    page_annotation,
)


def _spec_in(script: str) -> dict[str, Any]:
    """The object literal the script was built with, read back out of the source."""
    marker = "const spec = "
    start = script.index(marker) + len(marker)
    payload, _ = json.JSONDecoder().raw_decode(script[start:])
    return payload


# --- annotation_spec: what a caller is allowed to ask for -------------------------------------


def test_note_whitespace_is_collapsed_not_rejected() -> None:
    spec = annotation_spec(note="  looking   for the\n submit button  ")
    assert spec["note"] == "looking for the submit button"


def test_blank_note_is_rejected_rather_than_drawn_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        annotation_spec(note="   \n ")


def test_overlong_note_is_rejected_with_the_cap_in_the_message() -> None:
    with pytest.raises(ValueError, match=str(MAX_NOTE_CHARS)):
        annotation_spec(note="x" * (MAX_NOTE_CHARS + 1))


def test_a_note_at_the_cap_is_fine() -> None:
    assert annotation_spec(note="x" * MAX_NOTE_CHARS)["note"] == "x" * MAX_NOTE_CHARS


def test_selector_and_text_are_two_ways_to_aim_and_cannot_both_be_used() -> None:
    with pytest.raises(ValueError, match="not both"):
        annotation_spec(note="x", selector="#a", text="a")


@pytest.mark.parametrize("field", ["selector", "text"])
def test_an_empty_aim_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} is empty"):
        annotation_spec(note="x", **{field: "   "})


def test_unknown_tone_is_rejected_with_the_list() -> None:
    with pytest.raises(ValueError, match="info, warn, alert, success"):
        annotation_spec(note="x", tone="confetti")


@pytest.mark.parametrize("hold", [-1, 3601, "soon"])
def test_hold_is_a_bounded_number_of_seconds(hold: Any) -> None:
    with pytest.raises(ValueError, match="hold"):
        annotation_spec(note="x", hold=hold)


def test_spec_defaults_to_untimed_visible_narration() -> None:
    assert annotation_spec(note="hi") == {
        "note": "hi",
        "selector": None,
        "text": None,
        "tone": "info",
        "hold": 0.0,
        "scroll": True,
        "clear": False,
    }


def test_clear_is_a_spec_not_a_note() -> None:
    assert annotation_spec(clear=True)["clear"] is True


# --- annotation_script: only encoded values reach the page ------------------------------------


def test_script_is_an_async_page_function() -> None:
    assert annotation_script(note="hi").lstrip().startswith("async () =>")


def test_request_values_arrive_json_encoded_in_the_source() -> None:
    script = annotation_script(note="ring this", selector="a.btn", tone="warn", hold=4)
    assert _spec_in(script) == {
        "note": "ring this",
        "selector": "a.btn",
        "text": None,
        "tone": "warn",
        "hold": 4.0,
        "scroll": True,
        "clear": False,
    }


def test_a_hostile_note_cannot_escape_into_markup_or_script() -> None:
    hostile = '</script><img src=x onerror="alert(1)">'
    script = annotation_script(note=hostile, text="`a` ${b} 'c' \"d\"")
    # Only the markup-exiting sequence has to be broken up. Template syntax survives verbatim
    # because it lands inside an ordinary string literal and is written to the DOM with
    # textContent, so there is nothing for it to interpolate or execute against.
    assert "</script>" not in script
    assert "</" not in script
    spec = _spec_in(script)
    assert spec["note"] == hostile
    assert spec["text"] == "`a` ${b} 'c' \"d\""


def test_a_hostile_selector_cannot_escape_either() -> None:
    script = annotation_script(note="x", selector='"; alert(1); //')
    assert _spec_in(script)["selector"] == '"; alert(1); //'


@pytest.mark.parametrize("kwargs", [{"note": "hi", "text": "a"}, {"clear": True}])
def test_generated_script_is_valid_javascript(kwargs: dict[str, Any], tmp_path: Any) -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - the check is only possible where node exists
        pytest.skip("node is not installed")
    path = tmp_path / "annotation.js"
    path.write_text(f"const fn = {annotation_script(**kwargs)};\nexport default fn;\n")
    subprocess.run([node, "--check", str(path)], check=True, capture_output=True)


def test_script_carries_the_dom_markers_it_will_inject() -> None:
    script = annotation_script(note="x")
    for marker in (CARD_ID, RING_ATTR, "iot-mcp-card", "iot-mcp-ring", "iot-mcp-annotate-style"):
        assert marker in script


def test_the_cap_on_targets_reaches_the_script() -> None:
    assert _spec_in(annotation_script(note="x"))["scroll"] is True
    assert f"const MAX_TARGETS = {MAX_TARGETS};" in annotation_script(note="x")


# --- page_annotation: reading the page's own report back --------------------------------------


def test_report_is_recovered_from_the_add_ons_prose_wrapper() -> None:
    result = {
        "content": [
            {
                "type": "text",
                "text": (
                    "### Result\n"
                    '{"url":"https://example.org","title":"Example","highlighted":'
                    '[{"tag":"a.btn","text":"Submit"}]}\n'
                    "### Ran Playwright code\nawait page.evaluate(...)"
                ),
            }
        ]
    }
    report = page_annotation(result)
    assert report is not None
    assert report["url"] == "https://example.org"
    assert report["highlighted"] == [{"tag": "a.btn", "text": "Submit"}]


def test_object_shaped_content_blocks_work_too() -> None:
    class Block:
        text = 'x {"cleared": {"rings": 2, "notes": 1}}'

    assert page_annotation({"content": [Block()]}) == {"cleared": {"rings": 2, "notes": 1}}


def test_a_result_with_no_json_object_yields_no_report() -> None:
    assert page_annotation({"content": [{"type": "text", "text": "### Result\nok"}]}) is None
    assert page_annotation({}) is None


# --- the instructions a calling agent reads ----------------------------------------------------


def test_instructions_name_both_tools_and_the_clear_step() -> None:
    assert ANNOTATE_TOOL in ANNOTATION_INSTRUCTIONS
    assert ANNOTATE_CLEAR_TOOL in ANNOTATION_INSTRUCTIONS
    assert "human may be watching" in ANNOTATION_INSTRUCTIONS


def test_instructions_refuse_to_sell_the_card_as_a_user_channel() -> None:
    assert "not a channel to the user" in ANNOTATION_INSTRUCTIONS
