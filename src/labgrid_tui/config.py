"""Configuration: TOML file with profiles.

Location (first match wins):
  1. --config PATH on the command line
  2. $LABGRID_TUI_CONFIG
  3. $XDG_CONFIG_HOME/labgrid-tui/config.toml (default ~/.config/labgrid-tui/config.toml)

Coordinator precedence:
  --coordinator flag  >  profile "coordinator"  >  $LG_COORDINATOR  >  127.0.0.1:20408

The selected profile wins over $LG_COORDINATOR on purpose: otherwise an
exported LG_COORDINATOR would silently override every profile and make
profile switching useless. A profile without "coordinator" falls back to
the environment.
"""

from __future__ import annotations

import getpass
import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COORDINATOR = "127.0.0.1:20408"


@dataclass
class Profile:
    name: str
    coordinator: str = ""
    proxy: str = ""
    username: str = ""
    hostname: str = ""
    ssh_user: str = ""  # user for ssh into exporter hosts ("" = ssh's default/ssh config)
    ssh_jump: str = ""  # ssh jump host; "" = use `proxy`, "none" = no jump host
    ssh_command: str = ""  # template, default "ssh {jump} {target}"

    def effective_coordinator(self) -> str:
        return self.coordinator or os.environ.get("LG_COORDINATOR") or DEFAULT_COORDINATOR

    def identity(self) -> str:
        """The 'host/user' string labgrid shows as owner of acquired places."""
        host = self.hostname or os.environ.get("LG_HOSTNAME") or socket.gethostname()
        user = self.username or os.environ.get("LG_USERNAME") or getpass.getuser()
        return f"{host}/{user}"


@dataclass
class UIConfig:
    refresh_seconds: float = 5.0  # polling interval (when live updates are off/unavailable)
    live_updates: bool = True  # use `labgrid-client monitor` instead of polling
    fallback_refresh_seconds: float = 60.0  # full refresh as a safety net in live mode
    default_filter: str = ""
    stale_hours: float = 24.0
    auto_acquire: bool = True
    saved_filters: list[str] = field(default_factory=list)
    exporter_display: str = "name"  # Places' exporter column: name | host (short) | fqdn
    hide_unlinked: bool = True  # hide places without match patterns (key: h)
    # Max column widths in characters (0 = as wide as the content). Keys: name, tags,
    # exporter, owner, comment, or a tag column as "tag:<key>".
    column_widths: dict[str, int] = field(default_factory=lambda: {"tags": 40, "comment": 30})
    tag_columns: list[str] = field(default_factory=list)  # tag keys shown as own columns
    wrap: bool = False  # wrap long cells onto several lines instead of "…"
    show_tags: bool = True  # show the combined Tags column
    debug_console: bool = False  # open the debug console at start (key: d)
    debug_hide_refresh: bool = False  # hide background refresh calls in it (key: D)
    log_file: str = ""  # also append every labgrid-client call to this file


@dataclass
class ConnectionConfig:
    timeout: float = 15.0  # seconds per labgrid-client call
    retries: int = 4  # retries after a connection error
    retry_delay: float = 0.5  # first backoff delay, doubles each retry
    max_retry_delay: float = 8.0  # backoff cap
    wait_give_up: float = 120.0  # stop retrying a reservation wait after this long offline
    rereserve: bool = True  # recreate a reservation lost by a coordinator restart


@dataclass
class Config:
    profiles: dict[str, Profile]
    default_profile: str
    ui: UIConfig
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    labgrid_client: str = "labgrid-client"
    path: Path | None = None

    def profile(self, name: str | None = None) -> Profile:
        return self.profiles[name or self.default_profile]


def default_config_path() -> Path:
    if env := os.environ.get("LABGRID_TUI_CONFIG"):
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "labgrid-tui" / "config.toml"


def load_config(path: str | Path | None = None) -> Config:
    explicit = path is not None
    path = Path(path).expanduser() if path else default_config_path()
    data: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    elif explicit:
        raise FileNotFoundError(f"config file not found: {path}")

    profiles = {
        name: Profile(
            name=name,
            coordinator=str(p.get("coordinator", "")),
            proxy=str(p.get("proxy", "")),
            username=str(p.get("username", "")),
            hostname=str(p.get("hostname", "")),
            ssh_user=str(p.get("ssh_user", "")),
            ssh_jump=str(p.get("ssh_jump", "")),
            ssh_command=str(p.get("ssh_command", "")),
        )
        for name, p in data.get("profiles", {}).items()
    }
    if not profiles:
        profiles = {"default": Profile(name="default")}

    default_profile = data.get("default_profile") or next(iter(profiles))
    if default_profile not in profiles:
        raise ValueError(
            f"default_profile '{default_profile}' not found; known: {', '.join(profiles)}"
        )

    ui_data = data.get("ui", {})
    ui = UIConfig(
        refresh_seconds=float(ui_data.get("refresh_seconds", 5)),
        live_updates=bool(ui_data.get("live_updates", True)),
        fallback_refresh_seconds=float(ui_data.get("fallback_refresh_seconds", 60)),
        default_filter=str(ui_data.get("default_filter", "")),
        stale_hours=float(ui_data.get("stale_hours", 24)),
        auto_acquire=bool(ui_data.get("auto_acquire", True)),
        saved_filters=list(ui_data.get("saved_filters", [])),
        exporter_display=str(ui_data.get("exporter_display", "name")),
        hide_unlinked=bool(ui_data.get("hide_unlinked", True)),
        column_widths={
            **UIConfig().column_widths,
            **{str(k): int(v) for k, v in ui_data.get("column_widths", {}).items()},
        },
        tag_columns=[str(t) for t in ui_data.get("tag_columns", [])],
        wrap=bool(ui_data.get("wrap", False)),
        show_tags=bool(ui_data.get("show_tags", True)),
        debug_console=bool(ui_data.get("debug_console", False)),
        debug_hide_refresh=bool(ui_data.get("debug_hide_refresh", False)),
        log_file=str(ui_data.get("log_file", "")),
    )
    if ui.exporter_display not in ("host", "fqdn", "name"):
        raise ValueError(
            f"ui.exporter_display must be host, fqdn or name, not '{ui.exporter_display}'"
        )
    c = data.get("connection", {})
    d = ConnectionConfig()
    connection = ConnectionConfig(
        timeout=float(c.get("timeout", d.timeout)),
        retries=int(c.get("retries", d.retries)),
        retry_delay=float(c.get("retry_delay", d.retry_delay)),
        max_retry_delay=float(c.get("max_retry_delay", d.max_retry_delay)),
        wait_give_up=float(c.get("wait_give_up", d.wait_give_up)),
        rereserve=bool(c.get("rereserve", d.rereserve)),
    )
    return Config(
        profiles=profiles,
        default_profile=default_profile,
        ui=ui,
        connection=connection,
        labgrid_client=str(data.get("labgrid_client", "labgrid-client")),
        path=path if path.exists() else None,
    )
