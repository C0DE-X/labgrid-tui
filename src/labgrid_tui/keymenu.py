"""Space menu: a popup listing every key that works right now (like Helix's space menu).

The entries are built from the app's BINDINGS, so new keys show up automatically.
GROUPS only decides where an action is listed and can give it a clearer label.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

# action name -> (group, label). Actions not listed here land in "Other" with the
# binding's own description.
GROUPS: dict[str, tuple[str, str]] = {
    "acquire": ("Place", "Acquire"),
    "release": ("Place", "Release"),
    "console": ("Place", "Serial console"),
    "details": ("Place", "Details"),
    "reserve": ("Reservations", "Reserve by tags"),
    "cancel_reservation": ("Reservations", "Cancel reservation"),
    "ssh": ("Exporter", "SSH to exporter host"),
    "focus_filter": ("Filter & view", "Filter"),
    "saved_filter": ("Filter & view", "Saved filter"),
    "toggle_free": ("Filter & view", "Free places only"),
    "toggle_unlinked": ("Filter & view", "Places without matches"),
    "toggle_sort": ("Filter & view", "Sort"),
    "tags_width": ("Columns", ""),  # keeps "Tags narrower/wider" from the binding
    "toggle_wrap": ("Columns", "Wrap long cells"),
    "toggle_tags": ("Columns", "Tags column"),
    "show_tab": ("Navigation", ""),
    "focus_next": ("Navigation", "Switch pane"),
    "profile": ("Navigation", "Switch profile"),
    "refresh": ("Navigation", "Refresh now"),
    "quit": ("Navigation", "Quit"),
    "toggle_debug": ("Debug", "Debug console"),
    "toggle_debug_refresh": ("Debug", "Hide refresh calls"),
    "copy_last_command": ("Debug", "Copy last command"),
}
GROUP_ORDER = [
    "Place",
    "Exporter",
    "Reservations",
    "Filter & view",
    "Columns",
    "Navigation",
    "Debug",
    "Other",
]

_KEY_NAMES = {
    "less_than_sign": "<",
    "greater_than_sign": ">",
    "slash": "/",
    "question_mark": "?",
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Esc",
    "space": "Space",
}


def key_label(key: str) -> str:
    """Textual key name -> what's printed on the keyboard ('less_than_sign' -> '<')."""
    if key in _KEY_NAMES:
        return _KEY_NAMES[key]
    if key.startswith("ctrl+"):
        return "Ctrl+" + key_label(key[5:]).upper()
    if len(key) >= 2 and key[0] == "f" and key[1:].isdigit():
        return key.upper()
    return key


@dataclass
class MenuEntry:
    keys: list[str]  # Textual key names; pressing any of them runs the action
    action: str  # full action string, e.g. "tags_width(5)"
    group: str
    label: str
    state: str | None = None  # "on" / "off" / "by name" for toggles


class KeyMenu(ModalScreen[str | None]):
    """Popup in the bottom-right corner. Returns the pressed key, or None."""

    DEFAULT_CSS = """
    KeyMenu {
        align: right bottom;
        background: transparent;
    }
    KeyMenu #menu {
        width: auto;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        margin: 0 1 1 0;
        padding: 0 1;
        border: round $accent;
        background: $surface;
    }
    KeyMenu #menu-body {
        width: auto;
        height: auto;
    }
    """

    def __init__(self, entries: list[MenuEntry], title: str) -> None:
        super().__init__()
        self.entries = entries
        self.menu_title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="menu") as box:
            box.border_title = self.menu_title
            box.border_subtitle = "press a key · Esc/Space: close"
            yield Static(self._render_groups(), id="menu-body")

    GROUPS_PER_ROW = 4
    COLUMN_GAP = 4

    def _group_lines(self, group: str, rows: list[MenuEntry]) -> list[Text]:
        keys = [", ".join(key_label(k) for k in e.keys) for e in rows]
        key_width = max(len(k) for k in keys)
        lines = [Text(" " * (key_width + 2)) + Text(group, style="bold underline")]
        for key, entry in zip(keys, rows, strict=True):
            line = Text(key.rjust(key_width), style="bold") + Text("  " + entry.label)
            if entry.state:
                line.append(f"  {entry.state}", style="green" if entry.state == "on" else "dim")
            lines.append(line)
        return lines

    def _render_groups(self) -> Text:
        """Groups side by side, GROUPS_PER_ROW per row, as plain text.

        (A rich Table here can't be measured by an auto-sized Static: it renders
        with width 0. Text can.)
        """
        groups = [
            self._group_lines(g, rows)
            for g in GROUP_ORDER
            if (rows := [e for e in self.entries if e.group == g])
        ]
        out: list[Text] = []
        for i in range(0, len(groups), self.GROUPS_PER_ROW):
            chunk = groups[i : i + self.GROUPS_PER_ROW]
            widths = [max(line.cell_len for line in g) for g in chunk]
            if out:
                out.append(Text())
            for n in range(max(len(g) for g in chunk)):
                line = Text()
                for col, (g, width) in enumerate(zip(chunk, widths, strict=True)):
                    cell = g[n] if n < len(g) else Text()
                    line.append_text(cell)
                    if col < len(chunk) - 1:
                        line.append(" " * (width - cell.cell_len + self.COLUMN_GAP))
                out.append(line)
        return Text("\n").join(out)

    def on_key(self, event: events.Key) -> None:
        # Every key belongs to the menu while it's open: close, or run that key.
        event.stop()
        event.prevent_default()
        if event.key in ("escape", "space"):
            self.dismiss(None)
        else:
            self.dismiss(event.key)
