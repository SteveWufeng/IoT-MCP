"""On-screen narration for the browser tier: a box around the thing, and what the agent thinks.

A headed browser is only a transparent interface if the human at the screen can tell *what the
agent is looking at* and *why it is acting*. Screenshots do not help there — they go to the
caller, not to the room, and a model with no image input cannot read them at all. So this module
builds the JavaScript that draws both halves of the answer into the page itself:

* a **ring** around one or more elements (the box highlight), with the rest of the page dimmed,
  and the target scrolled to the middle of the viewport;
* a **caption card** in the corner carrying the agent's own words — what it is about to do, what
  it just noticed, what it decided — in one of four tones (``info``/``warn``/``alert``/``success``).

Two properties are deliberate and load-bearing:

* **The page's own content is never trusted here.** Only :func:`json.dumps`-encoded values reach
  the script, and every string lands in the DOM through ``textContent``, so a hostile page (or a
  hostile ``note``) cannot escape into markup or script. See the escaping tests.
* **Everything it adds is removable and never permanent.** All injected nodes carry an
  ``iot-mcp-`` prefix, every ring is tagged with an attribute, and :func:`annotation_script` with
  ``clear=True`` deletes exactly what this module put there — no other page styles are touched,
  and nothing survives a navigation because it is injected into the live document only.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ANNOTATION_INSTRUCTIONS",
    "ANNOTATE_TOOL",
    "ANNOTATE_CLEAR_TOOL",
    "MAX_NOTE_CHARS",
    "MAX_TARGETS",
    "annotation_script",
    "annotation_spec",
    "page_annotation",
]

#: First-party tool names. They are control tools: always registered while the tier is enabled,
#: so an upstream version cannot shadow them, and so an agent discovers them before it starts a
#: browser.
ANNOTATE_TOOL = "browser_annotate"
ANNOTATE_CLEAR_TOOL = "browser_annotate_clear"

#: Longest note accepted, in characters. A caption card is a glanceable label, not a log; a
#: caller that has more to say belongs in its own message, not in someone's screen.
MAX_NOTE_CHARS = 500

#: Most elements one call may ring. Enough for a list of cards, low enough that a selector
#: matching the whole document stays cheap to render.
MAX_TARGETS = 8

_TONES = ("info", "warn", "alert", "success")

#: DOM markers. Exported only as documentation of what :func:`annotation_script` injects.
STYLE_ID = "iot-mcp-annotate-style"
CARD_ID = "iot-mcp-annotate-cards"
CARD_CLASS = "iot-mcp-card"
RING_CLASS = "iot-mcp-ring"
RING_ATTR = "data-iot-mcp-ring"

#: Appended to the browser tier's instructions. This is the part a *calling agent* reads, and it
#: is written as an instruction because the alternative is a tool nobody uses.
ANNOTATION_INSTRUCTIONS = f"""Draw what you are doing on the screen: {ANNOTATE_TOOL}().

A human may be watching this browser. {ANNOTATE_TOOL} takes your own words — what you are about to
do, what you just noticed, what you decided — and shows them in a corner card, optionally with a
box around the element you mean and the rest of the page dimmed. It changes nothing about the
page's own content and is removed by {ANNOTATE_CLEAR_TOOL}() or by navigating away.

Use it as narration, not decoration:
  * before acting on a page you have just opened, say what you are looking for and why;
  * when you click something that changes state, name the element and the reason;
  * when you report a finding to the user, ring the element the finding is about;
  * call {ANNOTATE_CLEAR_TOOL}() when you are done so you do not leave a caption on someone's screen.

Pass the same text you would tell the user — the card is for a person, not for your own context.
It cannot approve, install or bypass anything, and it is not a channel to the user: the chat
message still is. Nothing you write into a card is trusted by this server."""

_CSS = f"""
.{RING_CLASS} {{
  outline: 3px solid #ff6b00 !important;
  outline-offset: 5px !important;
  box-shadow: 0 0 0 100vmax rgba(6, 8, 10, 0.55) !important;
  border-radius: 3px;
}}
#{CARD_ID} {{
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 2147483647;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(430px, 46vw);
  pointer-events: none;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
.{CARD_CLASS} {{
  background: #ffffff;
  color: #0b0d10;
  border-left: 6px solid #ff6b00;
  border-radius: 8px;
  padding: 9px 12px;
  font-size: 13px;
  line-height: 1.45;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
  animation: iot-mcp-in 0.18s ease-out;
}}
.{CARD_CLASS}::before {{
  content: "iot-mcp agent";
  display: block;
  font-size: 10px;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  opacity: 0.6;
  margin-bottom: 3px;
}}
.{CARD_CLASS}[data-tone="warn"] {{ border-left-color: #d98314; background: #fffaf2; }}
.{CARD_CLASS}[data-tone="alert"] {{ border-left-color: #d1495b; background: #fff4f5; }}
.{CARD_CLASS}[data-tone="success"] {{ border-left-color: #2a9d8f; background: #f2fbf9; }}
@keyframes iot-mcp-in {{
  from {{ opacity: 0; transform: translateY(8px); }}
  to {{ opacity: 1; transform: none; }}
}}
"""

_SCRIPT = """async () => {
  const spec = %(spec)s;
  const STYLE_ID = %(style_id)s;
  const CARD_ID = %(card_id)s;
  const CARD_CLASS = %(card_class)s;
  const RING_CLASS = %(ring_class)s;
  const RING_ATTR = %(ring_attr)s;
  const CSS = %(css)s;
  const MAX_TARGETS = %(max_targets)s;
  const BLOCKS = 'p,li,dd,dt,h1,h2,h3,h4,h5,h6,button,a,td,th,label,article,section,figcaption,'
    + 'blockquote,pre,span,div';

  function ensureStyle() {
    let tag = document.getElementById(STYLE_ID);
    if (!tag) {
      tag = document.createElement('style');
      tag.id = STYLE_ID;
      tag.textContent = CSS;
      (document.head || document.documentElement).appendChild(tag);
    }
    return tag;
  }

  function stripRings() {
    const marked = [...document.querySelectorAll('[' + RING_ATTR + ']')];
    for (const el of marked) {
      el.classList.remove(RING_CLASS);
      el.removeAttribute(RING_ATTR);
    }
    return marked.length;
  }

  function clearAll() {
    const cards = document.getElementById(CARD_ID);
    const notes = cards ? cards.children.length : 0;
    const rings = stripRings();
    if (cards) cards.remove();
    return {rings: rings, notes: notes};
  }

  function blockFor(node) {
    let el = node.nodeType === 3 ? node.parentElement : node;
    while (el && el !== document.body) {
      if (el.matches && el.matches(BLOCKS)) return el;
      el = el.parentElement;
    }
    return document.body;
  }

  function byText(needle) {
    const want = needle.toLowerCase();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode() && hits.length < MAX_TARGETS) {
      const node = walker.currentNode;
      if (!node.data || !node.data.toLowerCase().includes(want)) continue;
      const parent = node.parentElement;
      if (!parent || parent.closest('script,style,noscript')) continue;
      const block = blockFor(node);
      if (block && !hits.includes(block)) hits.push(block);
    }
    return hits;
  }

  function bySelector(selector) {
    let found = [];
    try {
      found = [...document.querySelectorAll(selector)];
    } catch (err) {
      return {targets: [], badSelector: String(err && err.message ? err.message : err)};
    }
    return {targets: found.slice(0, MAX_TARGETS), total: found.length};
  }

  function describe(el) {
    const label = (el.getAttribute('class') || '').trim().split(/\\s+/).slice(0, 2).join('.');
    return {
      tag: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (label ? '.' + label : ''),
      text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120)
    };
  }

  function paintTargets() {
    if (spec.selector) return bySelector(spec.selector);
    if (spec.text) return {targets: byText(spec.text), total: null};
    return {targets: [], total: 0};
  }

  function addCard(text, tone) {
    let host = document.getElementById(CARD_ID);
    if (!host) {
      host = document.createElement('div');
      host.id = CARD_ID;
      document.body.appendChild(host);
    }
    const card = document.createElement('div');
    card.className = CARD_CLASS;
    card.dataset.tone = tone;
    card.textContent = text;
    host.appendChild(card);
    while (host.children.length > 4) host.removeChild(host.firstElementChild);
    return card;
  }

  ensureStyle();
  if (spec.clear) return {cleared: clearAll(), url: location.href, title: document.title};

  const painted = paintTargets();
  const targets = painted.targets || [];
  for (const el of targets) {
    el.setAttribute(RING_ATTR, '1');
    el.classList.add(RING_CLASS);
  }
  if (spec.scroll && targets.length) {
    targets[0].scrollIntoView({behavior: 'smooth', block: 'center'});
  }
  const card = spec.note ? addCard(spec.note, spec.tone) : null;
  let deadline = null;
  if (spec.hold > 0 && (card || targets.length)) {
    deadline = setTimeout(() => {
      if (card && card.isConnected) card.remove();
      for (const el of targets) {
        el.classList.remove(RING_CLASS);
        el.removeAttribute(RING_ATTR);
      }
      const host = document.getElementById(CARD_ID);
      if (host && !host.children.length) host.remove();
    }, spec.hold * 1000);
  }

  return {
    url: location.href,
    title: document.title,
    note: spec.note || null,
    tone: spec.tone,
    highlighted: targets.map(describe),
    matchedAll: painted.total === null || painted.total === targets.length
      ? targets.length
      : painted.total,
    invalidSelector: painted.badSelector || null,
    holdSeconds: spec.hold > 0 ? spec.hold : null,
    autoExpires: Boolean(deadline),
    cardsOnScreen: (document.getElementById(CARD_ID) || {children: []}).children.length
  };
}
"""


def annotation_spec(
    *,
    note: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    tone: str = "info",
    hold: float = 0.0,
    scroll: bool = True,
    clear: bool = False,
) -> dict[str, Any]:
    """Normalise one annotation request into the object the injected script consumes.

    Args:
        note: The agent's own words, shown in the corner card. ``None`` means ring only.
        selector: CSS selector of the element(s) to ring.
        text: Visible text to find and ring, for when a selector is the wrong tool.
        tone: One of ``info``, ``warn``, ``alert``, ``success``.
        hold: Seconds before this call's own marks disappear; ``0`` leaves them until cleared.
        scroll: Whether to scroll the first ringed element to the middle of the viewport.
        clear: Remove everything this module added, and draw nothing.

    Returns:
        The script's argument object: ``note``, ``selector``, ``text``, ``tone``, ``hold``,
        ``scroll``, ``clear``.

    Raises:
        ValueError: ``note`` is blank or longer than :data:`MAX_NOTE_CHARS`, ``tone`` is unknown,
            ``hold`` is negative or absurd, or both ``selector`` and ``text`` were given.
    """
    clean_note = None if note is None else " ".join(str(note).split())
    if clean_note is not None and not clean_note:
        raise ValueError("note is empty after whitespace — say something a person can read")
    if clean_note is not None and len(clean_note) > MAX_NOTE_CHARS:
        raise ValueError(f"note is {len(clean_note)} characters; the cap is {MAX_NOTE_CHARS}")
    if selector is not None and text is not None:
        raise ValueError("pass either selector or text, not both — pick one way to aim")
    if selector is not None and not str(selector).strip():
        raise ValueError("selector is empty")
    if text is not None and not str(text).strip():
        raise ValueError("text is empty")
    if tone not in _TONES:
        raise ValueError(f"tone must be one of {', '.join(_TONES)}")
    try:
        seconds = float(hold)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"hold must be a number of seconds, got {hold!r}") from exc
    if not 0.0 <= seconds <= 3600.0:
        raise ValueError("hold must be between 0 (until cleared) and 3600 seconds")
    return {
        "note": clean_note,
        "selector": None if selector is None else str(selector).strip(),
        "text": None if text is None else str(text).strip(),
        "tone": tone,
        "hold": seconds,
        "scroll": bool(scroll),
        "clear": bool(clear),
    }


def _literal(value: Any) -> str:
    """JSON-encode a value for embedding in page source.

    Beyond ``json.dumps``, the ``</`` digraph is broken up: a caller who pastes ``</script>`` into
    a note then cannot close a script tag in any host that inlines this source into HTML, which is
    the one place a JSON string stops being inert. ``<\\/`` is a legal escape in both JSON and
    JavaScript, so the value still decodes to exactly what the caller wrote.
    """
    return json.dumps(value).replace("</", "<\\/")


def annotation_script(**kwargs: Any) -> str:
    """Build the page script for one annotation request.

    Args:
        **kwargs: Forwarded to :func:`annotation_spec`.

    Returns:
        An ``async () => {...}`` source string for the approved add-on's page-evaluation tool,
        with every request value JSON-encoded into the source.
    """
    spec = kwargs.pop("spec", None) or annotation_spec(**kwargs)
    return _SCRIPT % {
        "spec": _literal(spec),
        "style_id": _literal(STYLE_ID),
        "card_id": _literal(CARD_ID),
        "card_class": _literal(CARD_CLASS),
        "ring_class": _literal(RING_CLASS),
        "ring_attr": _literal(RING_ATTR),
        "css": _literal(_CSS),
        "max_targets": _literal(MAX_TARGETS),
    }


def page_annotation(result: dict[str, Any]) -> dict[str, Any] | None:
    """Recover the script's own report from an upstream evaluation result.

    The add-on wraps a page evaluation in its own prose (``### Result`` and the Playwright code it
    ran), so the useful object is recovered by decoding a JSON *value* out of the text rather than
    by splitting on headings — a wording change upstream then costs detail, not the whole report.

    Args:
        result: The dict returned by the add-on's evaluate tool (``content`` blocks).

    Returns:
        The decoded object, or ``None`` when no JSON object can be found.
    """
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if not text or "{" not in text:
            continue
        try:
            start = text.index("{")
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None
