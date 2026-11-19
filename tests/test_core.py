from datetime import datetime

from labgrid_tui.config import load_config
from labgrid_tui.filtering import (
    FREE,
    MINE,
    RESERVED,
    TAKEN,
    humanize_age,
    matches_filter,
    place_status,
    sort_places,
)
from labgrid_tui.parser import parse_places, parse_reservations

# Captured from labgrid-client 26.0.1
PLACES = """\
Place 'imx8-01':
  aliases: lab-a
  comment: front rack
  tags: board=imx8m, rev=b
  matches:
    rl-test/Testport1/NetworkSerialPort
    rl-test/*/NetworkPowerPort -> power
  acquired: vm/root
  acquired resources:
  created: 2026-11-17 12:47:26.334763
  changed: 2026-11-17 12:47:31.204099
Place 'imx8-02':
  tags: board=imx8m, rev=a
  matches:
  acquired: None
  acquired resources:
  created: 2026-11-17 12:47:26.768121
  changed: 2026-11-17 12:47:28.794821
Place 'imx8-03':
  tags: board=imx8m, usb=fail
  matches:
  acquired: None
  acquired resources:
  created: 2026-11-17 12:47:27.326231
  changed: 2026-11-17 12:47:29.313263
  reservation: 3D6CWU3BM6
Place 'rpi4-01':
  tags: board=rpi4
  matches:
  acquired: ci/runner
  acquired resources:
  allowed: vm/bob, vm/alice
  created: 2026-11-17 12:47:27.840703
  changed: 2026-11-17 12:47:29.700739
"""

RESERVATIONS = """\
Reservation '3D6CWU3BM6':
  owner: vm/root
  token: 3D6CWU3BM6
  state: allocated
  prio: 5
  filters:
    main: board=imx8m
  allocations:
    main: imx8-03
  created: 2026-11-17 12:47:31.704216
  timeout: 2026-11-17 12:48:31.704310
"""


def test_parse_places():
    places = parse_places(PLACES)
    assert [p.name for p in places] == ["imx8-01", "imx8-02", "imx8-03", "rpi4-01"]
    p = places[0]
    assert p.aliases == ["lab-a"]
    assert p.comment == "front rack"
    assert p.tags == {"board": "imx8m", "rev": "b"}
    assert p.matches == [
        "rl-test/Testport1/NetworkSerialPort",
        "rl-test/*/NetworkPowerPort -> power",
    ]
    assert p.acquired == "vm/root"
    assert p.changed == datetime(2026, 11, 17, 12, 47, 31, 204099)
    assert places[1].acquired is None
    assert places[2].reservation == "3D6CWU3BM6"
    assert places[3].allowed == ["vm/bob", "vm/alice"]


def test_parse_reservations():
    (r,) = parse_reservations(RESERVATIONS)
    assert r.token == "3D6CWU3BM6"
    assert r.state == "allocated"
    assert r.prio == 5
    assert r.filters == {"main": "board=imx8m"}
    assert r.allocations == {"main": "imx8-03"}


def test_parse_ignores_noise():
    assert parse_places("WARNING something\n" + PLACES)[0].name == "imx8-01"
    assert parse_places("") == []


def test_status_and_sort():
    places = parse_places(PLACES)
    me = "vm/root"
    assert [place_status(p, me) for p in places] == [MINE, FREE, RESERVED, TAKEN]
    assert [p.name for p in sort_places(places[::-1], me)] == [
        "imx8-01",
        "imx8-02",
        "imx8-03",
        "rpi4-01",
    ]


def test_filter():
    places = {p.name: p for p in parse_places(PLACES)}

    def names(q):
        return sorted(n for n, p in places.items() if matches_filter(p, q))

    assert names("board=imx8m") == ["imx8-01", "imx8-02", "imx8-03"]
    assert names("board=imx8*  rev=") == ["imx8-01", "imx8-02"]
    assert names("name=lab-*") == ["imx8-01"]
    assert names("rack") == ["imx8-01"]
    assert names("ci/runner") == ["rpi4-01"]
    assert names("usb=fail board=rpi4") == []
    assert names("") == sorted(places)


def test_humanize():
    now = datetime(2026, 1, 2, 12, 0, 0)
    assert humanize_age(datetime(2026, 1, 2, 11, 59, 30), now) == "30s"
    assert humanize_age(datetime(2026, 1, 2, 10, 30, 0), now) == "1h30m"
    assert humanize_age(datetime(2025, 12, 30, 9, 0, 0), now) == "3d3h"


def test_config(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'default_profile = "b"\n'
        '[profiles.a]\ncoordinator = "a:1"\n'
        '[profiles.b]\nusername = "bob"\nhostname = "h"\n'
        '[ui]\nrefresh_seconds = 2\nsaved_filters = ["board=x"]\n'
    )
    monkeypatch.setenv("LG_COORDINATOR", "env:2")
    c = load_config(cfg)
    assert c.profile().name == "b"
    assert c.profile().effective_coordinator() == "env:2"  # profile without coordinator -> env
    assert c.profile("a").effective_coordinator() == "a:1"  # profile wins over env
    assert c.profile().identity() == "h/bob"
    assert c.ui.refresh_seconds == 2 and c.ui.saved_filters == ["board=x"]


def test_config_missing_file_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LABGRID_TUI_CONFIG", raising=False)
    monkeypatch.delenv("LG_COORDINATOR", raising=False)
    c = load_config()
    assert list(c.profiles) == ["default"]
    assert c.profile().effective_coordinator() == "127.0.0.1:20408"
