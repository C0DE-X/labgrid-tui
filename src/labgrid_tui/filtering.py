"""Filter and sort helpers (kept separate from the UI so they are testable)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

from .parser import Place, Resource

FREE, MINE, RESERVED, TAKEN = "free", "mine", "reserved", "taken"
_STATUS_ORDER = {MINE: 0, FREE: 1, RESERVED: 2, TAKEN: 3}


def place_status(place: Place, me: str) -> str:
    if place.acquired:
        return MINE if place.acquired == me else TAKEN
    if place.reservation:
        return RESERVED
    return FREE


def matches_filter(place: Place, query: str, exporters: list[str] = ()) -> bool:
    """Space-separated terms, all must match (AND).

    ``key=value``  tag match, value may use shell globs (board=imx8*)
    ``key=``       tag key exists with any value
    ``name=x``     place name or alias (globs allowed)
    ``exporter=x`` exporter name or host (globs allowed)
    other          case-insensitive substring of name, aliases, comment, tags, owner
    """
    for term in query.split():
        if "=" in term:
            key, _, pattern = term.partition("=")
            if key == "name":
                names = [place.name, *place.aliases]
                if not any(fnmatchcase(n, pattern or "*") for n in names):
                    return False
                continue
            if key == "exporter":
                if not any(fnmatchcase(e, pattern or "*") for e in exporters):
                    return False
                continue
            if key not in place.tags:
                return False
            if pattern and not fnmatchcase(place.tags[key], pattern):
                return False
        else:
            hay = " ".join(
                [
                    place.name,
                    *place.aliases,
                    place.comment,
                    place.tags_str,
                    place.acquired or "",
                    *exporters,
                ]
            ).lower()
            if term.lower() not in hay:
                return False
    return True


def sort_places(places: list[Place], me: str, by: str = "status") -> list[Place]:
    if by == "name":
        return sorted(places, key=lambda p: p.name)
    return sorted(places, key=lambda p: (_STATUS_ORDER[place_status(p, me)], p.name))


def humanize_age(ts: datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return ""
    seconds = max(0, int(((now or datetime.now()) - ts).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


# --------------------------------------------------------------------------- exporters

_LOCAL_HOSTS = {"", "localhost", "localhost.localdomain", "127.0.0.1", "::1"}


def _match_parts(pattern: str) -> list[str] | None:
    """Split a place match ('exp/group/cls[/name][ -> rename]') like labgrid does."""
    parts = pattern.split(" -> ")[0].strip().split("/")
    return parts if 3 <= len(parts) <= 4 else None


def _is_match(parts: list[str], res: Resource) -> bool:
    exp, grp, cls = parts[:3]
    name = parts[3] if len(parts) == 4 else None
    return (
        fnmatchcase(res.exporter, exp)
        and fnmatchcase(res.group, grp)
        and fnmatchcase(res.cls, cls)
        and (name is None or not res.name or fnmatchcase(res.name, name))
    )


@dataclass
class ExporterInfo:
    name: str  # exporter name (first part of the resource path)
    fqdn: str = ""  # machine the exporter runs on, if known
    online: bool = True  # False: a match has no resource at all (exporter gone)
    unavailable: int = 0  # resources present but not available

    @property
    def host(self) -> str:
        return self.fqdn.split(".")[0] if self.fqdn else self.name

    def label(self, mode: str = "host") -> str:
        if mode == "name":
            return self.name
        if mode == "fqdn":
            return self.fqdn or self.name
        return self.host

    def search_terms(self) -> list[str]:
        return [t for t in {self.name, self.host, self.fqdn} if t]


def place_exporters(place: Place, resources: list[Resource]) -> list[ExporterInfo]:
    """Exporters serving a place, resolved from its match patterns.

    A match without any resource (exporter offline or misconfigured) yields an
    offline entry named after the pattern's exporter part.
    """
    infos: dict[str, ExporterInfo] = {}
    for pattern in place.matches:
        parts = _match_parts(pattern)
        if not parts:
            continue
        hits = [r for r in resources if _is_match(parts, r)]
        if not hits:
            infos.setdefault(parts[0], ExporterInfo(name=parts[0], online=False))
            continue
        for r in hits:
            info = infos.get(r.exporter)
            if info is None or not info.online:
                info = infos[r.exporter] = ExporterInfo(name=r.exporter)
            if r.host not in _LOCAL_HOSTS and not info.fqdn:
                info.fqdn = r.host
            if not r.avail:
                info.unavailable += 1
    return sorted(infos.values(), key=lambda i: (not i.online, i.name))


# --------------------------------------------------------------------------- exporter overview


@dataclass
class ExporterRow:
    name: str
    fqdn: str = ""  # hostname of the exporter machine ("" if unknown)
    online: bool = True
    hostname_stale: bool = False  # fqdn is from an earlier refresh (exporter offline now)
    resources: int = 0
    unavailable: int = 0
    places: list[str] | None = None
    in_use: int = 0


def build_exporter_rows(
    places: list[Place],
    resources: list[Resource],
    place_infos: dict[str, list[ExporterInfo]],
    last_seen: dict[str, str],
) -> list[ExporterRow]:
    """One row per exporter: online ones from the resource list, offline ones that
    are still named by a place's match pattern (labgrid forgets offline exporters).

    ``last_seen`` maps exporter name -> hostname seen earlier; it is updated here so
    an exporter that goes offline keeps showing its last known hostname.
    """
    rows: dict[str, ExporterRow] = {}
    for r in resources:
        row = rows.setdefault(r.exporter, ExporterRow(name=r.exporter, places=[]))
        row.resources += 1
        row.unavailable += not r.avail
        if r.host not in _LOCAL_HOSTS and not row.fqdn:
            row.fqdn = r.host
    for row in rows.values():
        if row.fqdn:
            last_seen[row.name] = row.fqdn

    by_name = {p.name: p for p in places}
    for place_name, infos in place_infos.items():
        for info in infos:
            if any(c in info.name for c in "*?["):
                continue  # wildcard exporter pattern, not a real name
            row = rows.get(info.name)
            if row is None:
                row = rows[info.name] = ExporterRow(
                    name=info.name,
                    online=False,
                    places=[],
                    fqdn=last_seen.get(info.name, ""),
                    hostname_stale=info.name in last_seen,
                )
            if place_name not in row.places:
                row.places.append(place_name)
                if by_name.get(place_name) and by_name[place_name].acquired:
                    row.in_use += 1
    for row in rows.values():
        row.places.sort()
    return sorted(rows.values(), key=lambda r: (not r.online, r.name))


def is_unlinked(place: Place) -> bool:
    """A place without match patterns isn't linked to any hardware/exporter."""
    return not place.matches
