"""Textual application."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .backend import CommandEvent, ConnectionLost, Labgrid, LabgridError, origin
from .config import Config, Profile
from .filtering import (
    FREE,
    MINE,
    RESERVED,
    TAKEN,
    ExporterInfo,
    ExporterRow,
    _is_match,
    _match_parts,
    build_exporter_rows,
    humanize_age,
    is_unlinked,
    matches_filter,
    place_exporters,
    place_status,
    sort_places,
)
from .parser import Place, Reservation, Resource
from .ssh import ssh_command

STATUS_STYLE = {
    MINE: ("●", "bold green"),
    FREE: ("○", "dim"),
    RESERVED: ("◐", "yellow"),
    TAKEN: ("✕", "red"),
}
HIDDEN_RES_STATES = {"expired", "invalid"}


# --------------------------------------------------------------------------- modals


def _pattern_hits(pattern: str, resource: Resource) -> bool:
    parts = _match_parts(pattern)
    return bool(parts) and _is_match(parts, resource)


class ReserveScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, initial: str) -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(
                "[b]Reserve[/b] – tag filter, e.g. [i]board=imx8m[/i] or [i]name=imx8-02[/i]"
            )
            yield Input(value=self.initial, id="reserve-input")
            yield Label("[dim]Enter: reserve · Esc: cancel[/dim]")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class DetailScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,enter", "dismiss(None)", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(f"[b]{self.title_text}[/b]")
            with VerticalScroll():
                yield Static(Text(self.body))
            yield Label("[dim]Esc: close[/dim]")


class ProfileScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, profiles: dict[str, Profile], current: str) -> None:
        super().__init__()
        self.profiles = profiles
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("[b]Switch profile[/b]")
            yield OptionList(
                *[
                    f"{'▸ ' if n == self.current else '  '}{n}  "
                    f"[dim]{p.effective_coordinator()}[/dim]"
                    for n, p in self.profiles.items()
                ],
                id="profiles",
            )

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(list(self.profiles)[event.option_index])


# --------------------------------------------------------------------------- app


class LabgridTUI(App):
    TITLE = "labgrid-tui"
    CSS = """
    #status { height: 1; background: $panel; padding: 0 1; }
    #filter { margin: 0 0; }
    #tabs { height: 1fr; }
    #tabs TabPane { padding: 0; }
    #places, #exporters { height: 1fr; }
    #mine-label { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #mine { height: 8; }
    #debug { height: 12; border-top: solid $accent; background: $surface; }
    .dialog { width: 70; height: auto; max-height: 80%; padding: 1 2;
              border: thick $accent; background: $surface; }
    .dialog.wide { width: 90%; height: 80%; }
    ReserveScreen, DetailScreen, ProfileScreen { align: center middle; }
    #profiles { height: auto; max-height: 15; }
    """

    BINDINGS = [
        Binding("a", "acquire", "Acquire"),
        Binding("u", "release", "Release"),
        Binding("r", "reserve", "Reserve"),
        Binding("x", "cancel_reservation", "Cancel res."),
        Binding("c", "console", "Console"),
        Binding("s", "ssh", "SSH to exporter"),
        Binding("enter", "details", "Details", show=False),
        Binding("slash", "focus_filter", "Filter"),
        Binding("f", "toggle_free", "Free only"),
        Binding("o", "toggle_sort", "Sort", show=False),
        Binding("less_than_sign", "tags_width(-5)", "Tags narrower", show=False),
        Binding("greater_than_sign", "tags_width(5)", "Tags wider", show=False),
        Binding("w", "toggle_wrap", "Wrap", show=False),
        Binding("t", "toggle_tags", "Tags column", show=False),
        Binding("p", "profile", "Profile"),
        Binding("ctrl+r,f5", "refresh", "Refresh", show=False),
        Binding("tab", "focus_next", "Switch pane", show=False),
        Binding("q", "quit", "Quit"),
        Binding("1", "show_tab('tab-places')", "Places", show=False),
        Binding("2", "show_tab('tab-exporters')", "Exporters", show=False),
        Binding("h", "toggle_unlinked", "Unlinked places", show=False),
        Binding("d", "toggle_debug", "Debug"),
        Binding("D", "toggle_debug_refresh", "Debug: hide refresh", show=False),
        Binding("y", "copy_last_command", "Copy last command", show=False),
        *[Binding(f"f{i}", f"saved_filter({i})", show=False) for i in range(1, 10)],
    ]

    # Keys that only make sense on one tab; check_action hides the others from the
    # footer and disables them.
    PLACES_ONLY = {
        "acquire",
        "release",
        "reserve",
        "console",
        "details",
        "focus_filter",
        "toggle_free",
        "toggle_sort",
        "tags_width",
        "toggle_wrap",
        "toggle_tags",
        "toggle_unlinked",
        "saved_filter",
    }
    EXPORTERS_ONLY = {"ssh"}

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in self.PLACES_ONLY or action in self.EXPORTERS_ONLY:
            try:
                tab = self.query_one("#tabs", TabbedContent).active
            except Exception:
                return True
            on_exporters = tab == "tab-exporters"
            return (action in self.EXPORTERS_ONLY) == on_exporters
        return True

    def __init__(
        self, config: Config, profile: str | None = None, coordinator_override: str | None = None
    ) -> None:
        super().__init__()
        self.config = config
        self.coordinator_override = coordinator_override
        self.profile_name = profile or config.default_profile
        self.places: list[Place] = []
        self.reservations: list[Reservation] = []
        self.resources: list[Resource] = []
        self.exporters: dict[str, list[ExporterInfo]] = {}
        self.connected: bool | None = None
        self.last_refresh: datetime | None = None
        self.last_error = ""
        self.down_since: datetime | None = None
        self.refresh_failures = 0
        self.next_refresh_at = 0.0
        self._refreshing = False
        self._pending_parts: set[str] = set()
        self._ticks = 0
        self.live = False
        self._monitor_was_lost = False
        self.next_reservation_poll = 0.0
        self.free_only = False
        self.col_widths = dict(config.ui.column_widths)
        self.wrap = config.ui.wrap
        self.show_tags = config.ui.show_tags
        self._columns_sig: tuple | None = None
        self.show_unlinked = not config.ui.hide_unlinked
        self.last_seen_hosts: dict[str, str] = {}
        self.exporter_rows: list[ExporterRow] = []
        self.debug_events: deque[CommandEvent] = deque(maxlen=2000)
        self.debug_hide_refresh = config.ui.debug_hide_refresh
        self._log_file_failed = False
        self.sort_by = "status"
        self._set_profile(self.profile_name)

    # -- setup ---------------------------------------------------------------

    def _set_profile(self, name: str) -> None:
        profile = self.config.profile(name)
        if self.coordinator_override:
            profile = Profile(**{**profile.__dict__, "coordinator": self.coordinator_override})
        self.profile_name = name
        self.profile = profile
        self.me = profile.identity()
        self.lg = Labgrid(profile, self.config.labgrid_client, self.config.connection)
        self.lg.listeners.append(self._on_command)

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with TabbedContent(id="tabs"):
            with TabPane("Places [1]", id="tab-places"):
                yield Input(
                    value=self.config.ui.default_filter,
                    id="filter",
                    placeholder="/ filter: board=imx8*  name=rpi*  exporter=…  text …",
                )
                yield DataTable(id="places", cursor_type="row", zebra_stripes=True)
            with TabPane("Exporters [2]", id="tab-exporters"):
                yield DataTable(id="exporters", cursor_type="row", zebra_stripes=True)
        yield Label("My places & reservations", id="mine-label")
        yield DataTable(id="mine", cursor_type="row")
        log = RichLog(id="debug", max_lines=2000, wrap=False, markup=False, highlight=False)
        log.border_title = (
            "labgrid-client calls  ·  d: close  ·  D: hide/show refresh  ·  y: copy last command"
        )
        log.display = self.config.ui.debug_console
        yield log
        yield Footer()

    def on_mount(self) -> None:
        places = self.query_one("#places", DataTable)
        self._ensure_columns(places)
        exporters = self.query_one("#exporters", DataTable)
        for label, key in [
            ("", "st"),
            ("Exporter", "name"),
            ("Hostname", "host"),
            ("Resources", "res"),
            ("In use", "use"),
            ("Places", "places"),
        ]:
            exporters.add_column(label, key=key)
        mine = self.query_one("#mine", DataTable)
        for label, key in [
            ("Type", "type"),
            ("Place / Token", "id"),
            ("Filter / Tags", "info"),
            ("State", "state"),
            ("Expires / Since", "time"),
        ]:
            mine.add_column(label, key=key)
        places.focus()
        self.update_status()
        self.action_refresh()
        self.set_interval(1.0, self._tick)
        if self.config.ui.live_updates:
            self.run_monitor()

    # -- data ----------------------------------------------------------------

    ALL_PARTS = frozenset({"places", "reservations", "resources"})

    def _tick(self) -> None:
        """Called every second; decides what needs refreshing."""
        now = time.monotonic()
        self._ticks += 1
        if self.connected is False:
            self.update_status()  # keep the retry countdown ticking
        if self.live and self._ticks % 5 == 0:
            self.render_tables()  # few refreshes in live mode: keep ages/countdowns current
        if self._refreshing:
            return
        if now >= self.next_refresh_at:
            self.request_refresh()  # polling, or the safety-net refresh in live mode
        elif self.live and self._reservations_open() and now >= self.next_reservation_poll:
            # the monitor doesn't report reservations: poll them while we have open ones
            self.next_reservation_poll = now + max(1.0, self.config.ui.refresh_seconds)
            self.request_refresh("reservations")

    def _reservations_open(self) -> bool:
        return any(
            r.owner == self.me and r.state in ("waiting", "allocated", "acquired")
            for r in self.reservations
        )

    def action_refresh(self) -> None:
        """Refresh everything now (also retries immediately while disconnected)."""
        self.next_refresh_at = 0.0
        self.request_refresh()

    def request_refresh(self, *parts: str) -> None:
        """Refresh some or all of places/reservations/resources. Requests arriving
        during a refresh are merged and run right after it (bursts of monitor
        events turn into one follow-up refresh)."""
        self._pending_parts |= set(parts) if parts else set(self.ALL_PARTS)
        if not self._refreshing:
            self.refresh_data()

    @work(group="refresh")
    async def refresh_data(self) -> None:
        origin.set("refresh")  # only affects this worker's task
        if self._refreshing:
            return
        self._refreshing = True
        parts = self._pending_parts or set(self.ALL_PARTS)
        self._pending_parts = set()
        if self.connected is False:
            parts = set(self.ALL_PARTS)  # reconnecting: get everything
        try:
            # Background refresh: one quick retry only, so an outage shows up
            # within seconds; the app's own backoff loop keeps reconnecting.
            places = await self.lg.places(retries=1) if "places" in parts else self.places
            reservations = (
                await self.lg.reservations(retries=1)
                if "reservations" in parts
                else self.reservations
            )
            resources = self.resources
            if "resources" in parts:
                try:
                    resources = await self.lg.resources(retries=1)
                except ConnectionLost:
                    raise
                except LabgridError:
                    pass  # exporter info is optional; keep the last known
        except ConnectionLost as e:
            self._connection_failed(str(e))
        except LabgridError as e:  # coordinator answered, but with an error
            self.connected, self.last_error = True, str(e)
            self._schedule_next(self._full_interval())
        else:
            self.places, self.reservations, self.resources = places, reservations, resources
            if self.connected is False:
                down = humanize_age(self.down_since)
                self.notify(f"Reconnected to coordinator (was down {down})", timeout=5)
            self.connected, self.last_error = True, ""
            self.down_since, self.refresh_failures = None, 0
            self.last_refresh = datetime.now()
            if parts >= self.ALL_PARTS:
                self._schedule_next(self._full_interval())
        finally:
            self._refreshing = False
        self.render_tables()
        self.update_status()
        if self._pending_parts and self.connected:
            self.refresh_data()

    def _full_interval(self) -> float:
        if self.live:
            return max(self.config.ui.refresh_seconds, self.config.ui.fallback_refresh_seconds)
        return self.config.ui.refresh_seconds

    # -- live updates ----------------------------------------------------------

    @work(group="monitor", exclusive=True)
    async def run_monitor(self) -> None:
        """Keep `labgrid-client monitor` running; fall back to polling while it isn't."""
        origin.set("live")
        failures = 0
        while True:
            try:
                await self.lg.monitor(self._on_monitor_event, on_started=self._monitor_started)
            except LabgridError:
                pass
            was_live, self.live = self.live, False
            if was_live:
                failures = 0
                self.notify(
                    "Live updates lost – polling until they're back", severity="warning", timeout=5
                )
                self.action_refresh()  # also detects/report a lost connection right away
            self.update_status()
            failures += 1
            await asyncio.sleep(min(2.0 * 2 ** (failures - 1), 30.0))

    def _monitor_started(self) -> None:
        self.live = True
        if self._monitor_was_lost:
            self.notify("Live updates are back", timeout=3)
        self._monitor_was_lost = True  # from now on, a restart is a "comeback"
        self.action_refresh()  # catch up on anything missed while it was down
        self.update_status()

    def _on_monitor_event(self, kind: str, name: str, action: str) -> None:
        self.request_refresh("places" if kind == "place" else "resources")

    def _connection_failed(self, error: str) -> None:
        if self.connected is not False:
            self.down_since = datetime.now()
            self.notify(
                f"Lost connection to coordinator – retrying in the background.\n{error}",
                severity="warning",
                timeout=8,
            )
        self.connected, self.last_error = False, error
        self.refresh_failures += 1
        # back off: refresh interval, doubling up to 30s, so a dead coordinator isn't hammered
        base = max(1.0, self.config.ui.refresh_seconds)
        self._schedule_next(min(base * 2 ** (self.refresh_failures - 1), max(base, 30.0)))

    def _schedule_next(self, seconds: float) -> None:
        self.next_refresh_at = time.monotonic() + seconds

    def update_status(self) -> None:
        if self.connected is None:
            conn = "[yellow]● connecting…[/]"
        elif self.connected:
            conn = "[green]● connected[/]"
        else:
            wait = max(0, int(self.next_refresh_at - time.monotonic()))
            retry = "retrying…" if self._refreshing else f"retry in {wait}s (Ctrl+R: now)"
            conn = f"[red]● disconnected {humanize_age(self.down_since)} – {retry}[/]"
        parts = [
            f"[b]{self.profile_name}[/b] ({self.profile.effective_coordinator()})",
            conn,
            f"you: {self.me}",
        ]
        if self.connected:
            if self.live:
                parts.append("[green]⚡ live[/]")
            elif self.config.ui.live_updates:
                parts.append(
                    f"[yellow]polling {self.config.ui.refresh_seconds:g}s "
                    "(live updates unavailable)[/]"
                )
        if self.free_only:
            parts.append("[yellow]free only[/]")
        hidden = sum(map(is_unlinked, self.places))
        if hidden and not self.show_unlinked:
            parts.append(f"[dim]{hidden} without matches hidden (h)[/]")
        if self.last_refresh:
            stale = " [red](stale)[/]" if self.connected is False else ""
            parts.append(f"⟳ {self.last_refresh:%H:%M:%S}{stale}")
        self.query_one("#status", Static).update(" · ".join(parts))

    def visible_places(self) -> list[Place]:
        query = self.query_one("#filter", Input).value
        result = [
            p
            for p in self.places
            if (self.show_unlinked or not is_unlinked(p))
            and matches_filter(
                p, query, [t for e in self.exporters.get(p.name, []) for t in e.search_terms()]
            )
        ]
        if self.free_only:
            result = [p for p in result if place_status(p, self.me) == FREE]
        return sort_places(result, self.me, self.sort_by)

    def render_tables(self) -> None:
        self.exporters = {p.name: place_exporters(p, self.resources) for p in self.places}
        now = datetime.now()
        stale = timedelta(hours=self.config.ui.stale_hours)

        table = self.query_one("#places", DataTable)
        selected = self._selected_key(table)
        self._ensure_columns(table)
        table.clear()
        tag_cols = self.config.ui.tag_columns
        for p in self.visible_places():
            status = place_status(p, self.me)
            sym, style = STATUS_STYLE[status]
            since = ""
            if p.acquired:
                since = humanize_age(p.changed, now)
                if p.changed and now - p.changed > stale and status == TAKEN:
                    since = Text(f"{since} ⚠", style="bold yellow")
            name = p.name + (f" ({', '.join(p.aliases)})" if p.aliases else "")
            owner = p.acquired or (f"res {p.reservation}" if p.reservation else "")
            other_tags = ", ".join(
                f"{k}={v}" for k, v in sorted(p.tags.items()) if k not in tag_cols
            )
            cells = [
                Text(sym, style=style),
                self._cell(name, "name", style if status == MINE else ""),
                *[self._cell(p.tags.get(k, ""), f"tag:{k}") for k in tag_cols],
            ]
            if self.show_tags:
                cells.append(self._cell(other_tags, "tags"))
            cells += [
                self._cell(self.exporter_cell(p.name), "exporter"),
                self._cell(owner, "owner"),
                since,
                self._cell(p.comment, "comment"),
            ]
            table.add_row(*cells, key=p.name, height=None if self.wrap else 1)
        self._restore_cursor(table, selected)

        self.render_exporters()

        mine = self.query_one("#mine", DataTable)
        selected = self._selected_key(mine)
        mine.clear()
        for p in self.places:
            if p.acquired == self.me:
                mine.add_row(
                    "place",
                    Text(p.name, style="bold green"),
                    p.tags_str,
                    "acquired",
                    humanize_age(p.changed, now),
                    key=f"place:{p.name}",
                )
        for r in self.reservations:
            if r.owner != self.me or r.state in HIDDEN_RES_STATES:
                continue
            alloc = ", ".join(r.allocations.values())
            state = r.state + (f" → {alloc}" if alloc else "")
            expires = ""
            if r.timeout:
                secs = int((r.timeout - now).total_seconds())
                expires = f"{secs}s" if secs > 0 else "now"
            mine.add_row(
                "reservation",
                r.token,
                " ".join(r.filters.values()),
                state,
                expires,
                key=f"res:{r.token}",
            )
        self._restore_cursor(mine, selected)

    def render_exporters(self) -> None:
        self.exporter_rows = build_exporter_rows(
            self.places, self.resources, self.exporters, self.last_seen_hosts
        )
        table = self.query_one("#exporters", DataTable)
        selected = self._selected_key(table)
        table.clear()
        for r in self.exporter_rows:
            if not r.online:
                st, style = Text("✕", style="bold red"), "red"
                res = Text("offline", style="red")
            elif r.unavailable:
                st, style = Text("!", style="bold yellow"), ""
                res = Text(f"{r.resources} ({r.unavailable} unavailable)", style="yellow")
            else:
                st, style = Text("●", style="green"), ""
                res = str(r.resources)
            host = Text(r.fqdn or "–", style="dim" if r.hostname_stale or not r.fqdn else "")
            if r.hostname_stale:
                host.append("  (last seen)", style="dim italic")
            places = ", ".join(r.places) if r.places else Text("no places", style="dim")
            use = f"{r.in_use}/{len(r.places)}" if r.places else ""
            table.add_row(
                st,
                Text(r.name, style=style),
                host,
                res,
                use,
                self._cell(places, "places"),
                key=r.name,
            )
        self._restore_cursor(table, selected)

    def action_show_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab
        self.query_one("#places" if tab == "tab-places" else "#exporters", DataTable).focus()

    @on(TabbedContent.TabActivated)
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()  # footer: show this tab's keys
        target = "#places" if event.pane.id == "tab-places" else "#exporters"
        self.query_one(target, DataTable).focus()

    def show_exporter_places(self, name: str) -> None:
        """Enter on an exporter: show its places in the Places tab."""
        self.query_one("#filter", Input).value = f"exporter={name}"
        self.action_show_tab("tab-places")

    def action_toggle_unlinked(self) -> None:
        self.show_unlinked = not self.show_unlinked
        self.notify(
            "Showing places without matches"
            if self.show_unlinked
            else "Hiding places without matches",
            timeout=2,
        )
        self.render_tables()
        self.update_status()

    # -- debug console -------------------------------------------------------

    _KIND_STYLE = {
        "ok": ("✓", "green"),
        "error": ("✕", "red"),
        "conn": ("⚡", "bold red"),
        "start": ("▶", "cyan"),
        "retry": ("↻", "yellow"),
        "info": ("·", "dim"),
        "event": ("◆", "magenta"),
    }
    _BACKGROUND = ("refresh", "live")

    def _on_command(self, event: CommandEvent) -> None:
        self.debug_events.append(event)
        self._write_log_file(event)
        if self.debug_hide_refresh and event.origin in self._BACKGROUND:
            return
        try:
            self.query_one("#debug", RichLog).write(self._format_event(event))
        except Exception:
            pass  # not mounted yet

    def _format_event(self, e: CommandEvent) -> Text:
        sym, style = self._KIND_STYLE.get(e.kind, ("·", ""))
        dim = e.origin in self._BACKGROUND
        line = Text(f"{e.when:%H:%M:%S.%f}"[:-3] + " ", style="dim")
        line.append(f"{sym} ", style=style)
        line.append(f"{e.duration:5.2f}s " if e.duration is not None else "       ", style="dim")
        if e.command:
            line.append(e.command, style="dim" if dim else "bold")
        if e.message:
            line.append(
                ("  → " if e.command else "") + e.message,
                style="dim" if e.kind in ("ok", "event", "info") else style,
            )
        if dim:
            line.append(f"  [{e.origin}]", style="dim italic")
        return line

    def _write_log_file(self, e: CommandEvent) -> None:
        path = self.config.ui.log_file
        if not path or self._log_file_failed:
            return
        try:
            dur = f"{e.duration:.2f}s" if e.duration is not None else "-"
            with Path(path).expanduser().open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{e.when.isoformat(timespec='milliseconds')}\t{e.kind}\t{dur}\t"
                    f"{e.origin}\t{e.command}\t{e.message}\n"
                )
        except OSError as err:
            self._log_file_failed = True
            self.notify(f"Can't write log file {path}: {err}", severity="error")

    def action_toggle_debug(self) -> None:
        log = self.query_one("#debug", RichLog)
        log.display = not log.display

    def action_toggle_debug_refresh(self) -> None:
        self.debug_hide_refresh = not self.debug_hide_refresh
        log = self.query_one("#debug", RichLog)
        log.clear()
        for e in self.debug_events:
            if not (self.debug_hide_refresh and e.origin in self._BACKGROUND):
                log.write(self._format_event(e))
        self.notify(
            "Debug console: refresh calls " + ("hidden" if self.debug_hide_refresh else "shown"),
            timeout=2,
        )

    def action_copy_last_command(self) -> None:
        last = next(
            (
                e
                for e in reversed(self.debug_events)
                if e.command and e.origin not in self._BACKGROUND
            ),
            None,
        )
        last = last or next((e for e in reversed(self.debug_events) if e.command), None)
        if last:
            self.copy_to_clipboard(last.command)
            self.notify(f"Copied: {last.command}", timeout=4)

    # -- columns -------------------------------------------------------------

    def _column_spec(self) -> list[tuple[str, str, int | None]]:
        spec = [
            ("", "st"),
            ("Place", "name"),
            *[(k.capitalize(), f"tag:{k}") for k in self.config.ui.tag_columns],
        ]
        if self.show_tags:
            spec.append(("Tags" if not self.config.ui.tag_columns else "Other tags", "tags"))
        spec += [
            ("Exporter", "exporter"),
            ("Owner", "owner"),
            ("Since", "since"),
            ("Comment", "comment"),
        ]
        return [(label, key, self.col_widths.get(key) or None) for label, key in spec]

    def _ensure_columns(self, table: DataTable) -> None:
        """(Re)create the columns when widths or visible columns changed."""
        spec = self._column_spec()
        sig = (tuple(spec), self.wrap)
        if sig == self._columns_sig:
            return
        table.clear(columns=True)
        for label, key, width in spec:
            table.add_column(label, key=key, width=width)
        self._columns_sig = sig

    def _cell(self, value: str | Text, key: str, style: str = "") -> Text:
        text = value.copy() if isinstance(value, Text) else Text(value, style=style)
        if self.col_widths.get(key):
            if self.wrap:
                text.overflow = "fold"
            else:
                text.no_wrap, text.overflow = True, "ellipsis"
        return text

    def action_tags_width(self, delta: int) -> None:
        if not self.show_tags:
            return
        table = self.query_one("#places", DataTable)
        current = self.col_widths.get("tags") or 0
        if not current:  # auto width: start from what is shown now
            col = table.columns.get(next((k for k in table.columns if k.value == "tags"), None))
            current = col.content_width if col else 40
        self.col_widths["tags"] = max(8, min(300, current + delta))
        self.notify(f"Tags column: {self.col_widths['tags']} chars", timeout=1.5)
        self.render_tables()

    def action_toggle_wrap(self) -> None:
        self.wrap = not self.wrap
        self.notify("Long cells wrap" if self.wrap else "Long cells are cut off (…)", timeout=2)
        self._columns_sig = None
        self.render_tables()

    def action_toggle_tags(self) -> None:
        self.show_tags = not self.show_tags
        self.render_tables()

    def exporter_cell(self, place: str) -> Text:
        """'rack2', 'rack2, rack3', offline 'gone ✕' (red), unavailable 'rack2 !' (yellow)."""
        cell = Text()
        infos = self.exporters.get(place, [])
        if not infos:
            p = next((x for x in self.places if x.name == place), None)
            if p is not None and not p.matches:
                return Text("no matches", style="dim italic")
        for i, info in enumerate(infos):
            if i:
                cell.append(", ")
            label = info.label(self.config.ui.exporter_display)
            if not info.online:
                cell.append(f"{label} ✕", style="bold red")
            elif info.unavailable:
                cell.append(f"{label} !", style="yellow")
            else:
                cell.append(label)
        return cell

    def exporter_summary(self, place: str) -> str:
        p = next((x for x in self.places if x.name == place), None)
        if p is None:
            return ""
        if not p.matches:
            return (
                "Exporter: none – this place has no match patterns, so it isn't linked to any\n"
                "exporter. Add one with:\n"
                f"  labgrid-client -p {place} add-match <exporter>/<group>/*"
            )
        lines = ["Exporters (resolved from the place's match patterns):"]
        for pattern in p.matches:
            hits = [r for r in self.resources if _pattern_hits(pattern, r)]
            if not hits:
                lines.append(
                    f"  {pattern}  →  no matching resource "
                    "(exporter offline, or name/group misspelled – case-sensitive)"
                )
                continue
            for exp in sorted({r.exporter for r in hits}):
                rs = [r for r in hits if r.exporter == exp]
                host = next((r.host for r in rs if r.host), "")
                bad = sum(not r.avail for r in rs)
                state = (
                    f"{bad} of {len(rs)} unavailable" if bad else f"{len(rs)} resource(s) online"
                )
                lines.append(
                    f"  {pattern}  →  {exp}" + (f" ({host})" if host else "") + f": {state}"
                )
        return "\n".join(lines)

    @staticmethod
    def _selected_key(table: DataTable) -> str | None:
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
        except Exception:
            return None

    @staticmethod
    def _restore_cursor(table: DataTable, key: str | None) -> None:
        if key is None:
            return
        try:
            table.move_cursor(row=table.get_row_index(key), animate=False)
        except Exception:
            pass

    # -- selection helpers ---------------------------------------------------

    def selected_place(self) -> Place | None:
        mine = self.query_one("#mine", DataTable)
        if self.focused is self.query_one("#exporters", DataTable):
            return None  # place actions only act on the Places tab / my-places pane
        if self.focused is mine:
            key = self._selected_key(mine) or ""
            name = key.removeprefix("place:") if key.startswith("place:") else None
        else:
            name = self._selected_key(self.query_one("#places", DataTable))
        return next((p for p in self.places if p.name == name), None)

    def selected_reservation(self) -> str | None:
        key = self._selected_key(self.query_one("#mine", DataTable)) or ""
        return key.removeprefix("res:") if key.startswith("res:") else None

    # -- filter / view -------------------------------------------------------

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        self.render_tables()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        self.query_one("#places", DataTable).focus()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_toggle_free(self) -> None:
        self.free_only = not self.free_only
        self.render_tables()
        self.update_status()

    def action_toggle_sort(self) -> None:
        self.sort_by = "name" if self.sort_by == "status" else "status"
        self.notify(f"Sorted by {self.sort_by}")
        self.render_tables()

    def action_saved_filter(self, index: int) -> None:
        saved = self.config.ui.saved_filters
        if index <= len(saved):
            self.query_one("#filter", Input).value = saved[index - 1]

    # -- actions -------------------------------------------------------------

    def action_details(self) -> None:
        place = self.selected_place()
        if place:
            self.show_details(place.name)

    @on(DataTable.RowSelected)
    def row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a table row: exporters -> their kits, places/mine -> details
        if event.data_table.id == "exporters":
            if event.row_key.value:
                self.show_exporter_places(event.row_key.value)
            return
        self.action_details()

    @work(group="action")
    async def show_details(self, name: str) -> None:
        try:
            body = await self.lg.show(name)
        except LabgridError as e:
            self.notify(str(e), severity="error")
            return
        summary = self.exporter_summary(name)
        self.push_screen(DetailScreen(f"Place {name}", f"{summary}\n\n{body}" if summary else body))

    def action_acquire(self) -> None:
        place = self.selected_place()
        if not place:
            return
        if place.acquired == self.me:
            self.notify(f"{place.name} is already yours")
        elif place.acquired:
            self.notify(f"{place.name} is held by {place.acquired}", severity="warning")
        else:
            self.perform(f"Acquired {place.name}", self.lg.acquire, place.name)

    def action_release(self) -> None:
        place = self.selected_place()
        if not place:
            return
        if place.acquired != self.me:
            who = place.acquired or "nobody"
            self.notify(f"{place.name} is not yours (held by {who})", severity="warning")
            return
        self.perform(f"Released {place.name}", self.lg.release, place.name)

    @work(group="action")
    async def perform(self, success: str, func, *args) -> None:
        try:
            await func(*args)
            self.notify(success)
        except ConnectionLost as e:
            self.notify(f"Connection problem, not done: {e}", severity="error", timeout=10)
        except LabgridError as e:
            self.notify(str(e), severity="error", timeout=8)
        self.action_refresh()

    def action_reserve(self) -> None:
        place = self.selected_place()
        initial = " ".join(
            t for t in self.query_one("#filter", Input).value.split() if "=" in t and "*" not in t
        )
        if not initial and place:
            initial = f"name={place.name}"
        self.push_screen(ReserveScreen(initial), self._do_reserve)

    def _do_reserve(self, value: str | None) -> None:
        if value:
            self.reserve_and_wait(value.split())

    @work(group="reservations")
    async def reserve_and_wait(self, filters: list[str]) -> None:
        """Reserve, keep the reservation alive while waiting, then acquire.

        Survives connection drops (wait is restarted quickly) and coordinator
        restarts (a lost reservation is recreated if ``rereserve`` is on).
        """
        label = " ".join(filters)
        rereserves = 0
        warned = False

        def on_retry(failures: int, error: Exception) -> None:
            nonlocal warned
            if not warned:
                warned = True
                self.notify(
                    f"Connection lost while waiting for '{label}' – reconnecting…",
                    severity="warning",
                    timeout=6,
                )

        while True:
            try:
                res = await self.lg.reserve(filters)
            except LabgridError as e:
                self.notify(f"Reserve '{label}' failed: {e}", severity="error", timeout=10)
                return
            self.notify(f"Reservation {res.token} ({label}): waiting for allocation…")
            self.action_refresh()
            try:
                state = await self.lg.wait(res.token, on_retry=on_retry)
            except ConnectionLost as e:
                self.notify(f"Gave up waiting for {res.token}: {e}", severity="error", timeout=15)
                return
            except LabgridError as e:
                state = "lost" if "does not exist" in str(e).lower() else None
                if state is None:
                    self.notify(f"Reservation {res.token}: {e}", severity="error", timeout=10)
                    return
            if warned:
                self.notify(f"Reconnected – reservation for '{label}' continues", timeout=5)
                warned = False

            if state in ("lost", "expired", "invalid"):
                if self.config.connection.rereserve and rereserves < 3:
                    rereserves += 1
                    self.notify(
                        f"Reservation {res.token} was {state} (coordinator restart?) – "
                        f"reserving '{label}' again",
                        severity="warning",
                        timeout=8,
                    )
                    continue
                self.notify(f"Reservation {res.token} {state}", severity="error", timeout=10)
                return
            break

        if not self.config.ui.auto_acquire:
            self.notify(
                f"Reservation {res.token} allocated – acquire it within the timeout", timeout=10
            )
            self.action_refresh()
            return
        try:
            await self.lg.acquire_reservation(res.token)
            self.notify(f"Reservation {res.token} allocated and acquired", timeout=8)
        except LabgridError as e:
            self.notify(
                f"Acquiring reservation {res.token} failed: {e}", severity="error", timeout=10
            )
        self.action_refresh()

    def action_cancel_reservation(self) -> None:
        token = self.selected_reservation()
        if not token:
            self.notify("Select a reservation in the bottom pane (Tab to switch)")
            return
        self.perform(f"Cancelled {token}", self.lg.cancel_reservation, token)

    def action_console(self) -> None:
        place = self.selected_place()
        if not place:
            return
        if place.acquired != self.me:
            self.notify(
                f"Acquire {place.name} first (a), then open its console", severity="warning"
            )
            return
        cmd = self.lg.command("console", place=place.name)
        self.lg.emit("start", self.lg.shell_line(cmd), message="interactive console")
        self._run_interactive(
            cmd, env=self.lg.env(), banner="exit the console to return to labgrid-tui"
        )
        self.action_refresh()

    def selected_exporter(self) -> ExporterRow | None:
        name = self._selected_key(self.query_one("#exporters", DataTable))
        return next((r for r in self.exporter_rows if r.name == name), None)

    def action_ssh(self) -> None:
        row = self.selected_exporter()
        if not row:
            return
        cmd = ssh_command(self.profile, row.name, row.fqdn)
        if not shutil.which(cmd[0]):
            self.notify(f"'{cmd[0]}' not found – is an ssh client installed?", severity="error")
            return
        notes = []
        if not row.fqdn:
            notes.append(f"hostname unknown – trying the exporter name '{row.name}'")
        if not row.online:
            notes.append("exporter is offline in labgrid – trying anyway")
        line = shlex.join(cmd)
        self.lg.emit(
            "start", line, message="ssh to exporter" + (f" ({'; '.join(notes)})" if notes else "")
        )
        self._run_interactive(cmd, banner="exit ssh to return to labgrid-tui", notes=notes)

    def _run_interactive(
        self, cmd: list[str], env: dict | None = None, banner: str = "", notes: list[str] = ()
    ) -> None:
        """Hand the terminal to an interactive program (console, ssh) and come back."""
        with self.suspend():
            print(f"$ {shlex.join(cmd)}")
            for note in notes:
                print(f"  note: {note}")
            print(f"({banner})\n" if banner else "")
            try:
                rc = subprocess.run(cmd, env=env).returncode
            except OSError as e:
                print(f"could not start {cmd[0]}: {e}")
                rc = 127
            if rc not in (0, 130):  # keep errors on screen instead of redrawing over them
                input(f"\n{cmd[0]} exited with code {rc}. Press Enter to return to labgrid-tui…")

    def action_profile(self) -> None:
        self.push_screen(ProfileScreen(self.config.profiles, self.profile_name), self._do_profile)

    def _do_profile(self, name: str | None) -> None:
        if name and name != self.profile_name:
            self._set_profile(name)
            self.places, self.reservations, self.connected = [], [], None
            self.resources, self.exporters = [], {}
            self.last_seen_hosts = {}
            self.live, self._monitor_was_lost = False, False
            if self.config.ui.live_updates:
                self.run_monitor()  # exclusive: replaces the old profile's monitor
            self.render_tables()
            self.update_status()
            self.action_refresh()
