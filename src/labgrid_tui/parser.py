"""Parsers for the human-readable output of ``labgrid-client``.

labgrid-client has no JSON output, so we parse ``-v places`` and
``reservations``. The format comes from ``Place.show()`` and
``Reservation.show()`` in ``labgrid/remote/common.py``:

    Place 'imx8-01':
      aliases: lab-a
      comment: front rack
      tags: board=imx8m, rev=b
      matches:
        rl-test/Testport1/NetworkSerialPort
      acquired: host/user          (or "None")
      acquired resources:
        exporter/group/Class/name
      allowed: host/user, ...
      created: 2026-11-17 12:47:26.334763
      changed: 2026-11-17 12:47:31.204099
      reservation: TOKEN

    Reservation 'TOKEN':
      owner: host/user
      token: TOKEN
      state: waiting|allocated|acquired|expired|invalid
      prio: 0
      filters:
        main: board=imx8m
      allocations:
        main: imx8-03
      created: ...
      timeout: ...
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import datetime

_HEADER_RE = re.compile(r"^(Place|Reservation) '(.+)':\s*$")
_LIST_KEYS = {"matches", "acquired resources"}
_DICT_KEYS = {"filters", "allocations"}


@dataclass
class Place:
    name: str
    aliases: list[str] = field(default_factory=list)
    comment: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    matches: list[str] = field(default_factory=list)
    acquired: str | None = None
    acquired_resources: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    created: datetime | None = None
    changed: datetime | None = None
    reservation: str | None = None

    @property
    def tags_str(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(self.tags.items()))


@dataclass
class Reservation:
    token: str
    owner: str = ""
    state: str = ""
    prio: int = 0
    filters: dict[str, str] = field(default_factory=dict)
    allocations: dict[str, str] = field(default_factory=dict)
    created: datetime | None = None
    timeout: datetime | None = None


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_tags(value: str) -> dict[str, str]:
    tags = {}
    for item in _split_list(value):
        key, _, val = item.partition("=")
        tags[key.strip()] = val.strip()
    return tags


def _blocks(text: str):
    """Yield (kind, name, fields) for each top-level block.

    ``fields`` maps key -> str for scalar lines, list[str] for list keys and
    dict[str, str] for dict keys.
    """
    kind = name = None
    fields: dict = {}
    current_key = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _HEADER_RE.match(raw)
        if m:
            if kind:
                yield kind, name, fields
            kind, name, fields, current_key = m.group(1), m.group(2), {}, None
            continue
        if kind is None:
            continue  # noise before first block
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent >= 4 and current_key:
            if current_key in _DICT_KEYS:
                k, _, v = line.partition(":")
                fields[current_key][k.strip()] = v.strip()
            else:
                fields[current_key].append(line)
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key in _LIST_KEYS:
            fields[key] = []
            current_key = key
        elif key in _DICT_KEYS:
            fields[key] = {}
            current_key = key
        else:
            fields[key] = value
            current_key = None
    if kind:
        yield kind, name, fields


def parse_places(text: str) -> list[Place]:
    places = []
    for kind, name, f in _blocks(text):
        if kind != "Place":
            continue
        acquired = f.get("acquired")
        places.append(
            Place(
                name=name,
                aliases=_split_list(f.get("aliases", "")),
                comment=f.get("comment", ""),
                tags=parse_tags(f.get("tags", "")),
                matches=f.get("matches", []),
                acquired=None if acquired in (None, "", "None") else acquired,
                acquired_resources=f.get("acquired resources", []),
                allowed=_split_list(f.get("allowed", "")),
                created=_parse_dt(f.get("created", "")),
                changed=_parse_dt(f.get("changed", "")),
                reservation=f.get("reservation") or None,
            )
        )
    return places


def parse_reservations(text: str) -> list[Reservation]:
    result = []
    for kind, name, f in _blocks(text):
        if kind != "Reservation":
            continue
        try:
            prio = int(f.get("prio", 0))
        except ValueError:
            prio = 0
        result.append(
            Reservation(
                token=f.get("token", name),
                owner=f.get("owner", ""),
                state=f.get("state", ""),
                prio=prio,
                filters=f.get("filters", {}),
                allocations=f.get("allocations", {}),
                created=_parse_dt(f.get("created", "")),
                timeout=_parse_dt(f.get("timeout", "")),
            )
        )
    return result


# --------------------------------------------------------------------------- resources
#
# ``labgrid-client -v -v resources`` (the second -v includes unavailable ones):
#
#   Exporter 'rack2':
#     Group 'board1' (rack2/board1/*):
#       Resource 'NetworkService' (rack2/board1/NetworkService[/NetworkService]):
#         {'acquired': None,
#          'avail': True,
#          'cls': 'NetworkService',
#          'params': {'address': '192.168.1.10',
#                     'extra': {'proxy': 'rack2.lab.example.com', ...},
#                     'username': 'root'}}

_RES_HEADER_RE = re.compile(
    r"^\s*Resource '(?P<name>.+)' \((?P<exp>[^/]+)/(?P<grp>[^/]+)/"
    r"(?P<cls>[^/\[]+)\[/[^\]]*\]\):\s*$"
)
_EXPORTER_RE = re.compile(r"^Exporter '(.+)':\s*$")


@dataclass
class Resource:
    exporter: str
    group: str
    cls: str
    name: str
    avail: bool = True
    acquired: str | None = None
    params: dict = field(default_factory=dict)

    @property
    def path(self) -> tuple[str, str, str, str]:
        return (self.exporter, self.group, self.cls, self.name)

    @property
    def host(self) -> str:
        """FQDN of the machine the exporter runs on (labgrid's SSH proxy host)."""
        extra = self.params.get("extra") or {}
        return str(extra.get("proxy") or "")


def _finish_resource(res: Resource | None, body: list[str]) -> Resource | None:
    if res is None:
        return None
    text = "\n".join(body)
    try:
        data = ast.literal_eval(text)
        res.avail = bool(data.get("avail", True))
        res.acquired = data.get("acquired")
        res.params = data.get("params") or {}
    except (ValueError, SyntaxError):
        # Unknown repr inside params: fall back to the fields we need.
        if m := re.search(r"'avail':\s*(True|False)", text):
            res.avail = m.group(1) == "True"
        if m := re.search(r"'proxy':\s*'([^']*)'", text):
            res.params = {"extra": {"proxy": m.group(1)}}
    return res


def parse_resources(text: str) -> list[Resource]:
    resources: list[Resource] = []
    current: Resource | None = None
    body: list[str] = []
    for line in text.splitlines():
        m = _RES_HEADER_RE.match(line)
        if m or _EXPORTER_RE.match(line) or line.lstrip().startswith("Group '"):
            if done := _finish_resource(current, body):
                resources.append(done)
            current, body = None, []
            if m:
                current = Resource(exporter=m["exp"], group=m["grp"], cls=m["cls"], name=m["name"])
            continue
        if current is not None:
            body.append(line)
    if done := _finish_resource(current, body):
        resources.append(done)
    return resources
