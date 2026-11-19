from labgrid_tui.filtering import matches_filter, place_exporters
from labgrid_tui.parser import Place, parse_resources

# Shape captured from labgrid-client 26.0.1 `-v -v resources`
RESOURCES = """\
Exporter 'rack2':
  Group 'board1' (rack2/board1/*):
    Resource 'NetworkService' (rack2/board1/NetworkService[/NetworkService]):
      {'acquired': None,
       'avail': True,
       'cls': 'NetworkService',
       'params': {'address': '192.168.1.10',
                  'extra': {'proxy': 'rack2.lab.example.com', 'proxy_required': False},
                  'username': 'root'}}
    Resource 'power' (rack2/board1/NetworkPowerPort[/power]):
      {'acquired': None,
       'avail': False,
       'cls': 'NetworkPowerPort',
       'params': {'extra': {'proxy': 'rack2.lab.example.com', 'proxy_required': False},
                  'host': 'pdu1', 'index': '3', 'model': 'gude'}}
Exporter 'vm':
  Group 'board1' (vm/board1/*):
    Resource 'NetworkService' (vm/board1/NetworkService[/NetworkService]):
      {'acquired': 'p1',
       'avail': True,
       'cls': 'NetworkService',
       'params': {'address': '192.168.1.10',
                  'extra': {'proxy': 'localhost', 'proxy_required': False},
                  'username': 'root'}}
"""


def test_parse_resources():
    res = parse_resources(RESOURCES)
    assert [r.path for r in res] == [
        ("rack2", "board1", "NetworkService", "NetworkService"),
        ("rack2", "board1", "NetworkPowerPort", "power"),
        ("vm", "board1", "NetworkService", "NetworkService"),
    ]
    assert res[0].host == "rack2.lab.example.com"
    assert res[1].avail is False
    assert res[2].acquired == "p1"


def test_parse_resources_unparseable_params():
    text = RESOURCES.replace("'model': 'gude'", "'model': <Model object at 0x1>")
    res = parse_resources(text)
    assert res[1].avail is False and res[1].host == "rack2.lab.example.com"


def _exp(*matches):
    return place_exporters(Place(name="x", matches=list(matches)), parse_resources(RESOURCES))


def test_exporter_resolution():
    (e,) = _exp("rack2/board1/NetworkService")
    assert (e.name, e.fqdn, e.host, e.online, e.unavailable) == (
        "rack2",
        "rack2.lab.example.com",
        "rack2",
        True,
        0,
    )
    assert e.label("fqdn") == "rack2.lab.example.com" and e.label("name") == "rack2"

    # wildcard over the group catches the unavailable power port too
    (e,) = _exp("rack2/board1/*")
    assert e.unavailable == 1

    # wildcard exporter -> both exporters; localhost falls back to the exporter name
    infos = _exp("*/board1/NetworkService")
    assert [(i.name, i.host) for i in infos] == [("rack2", "rack2"), ("vm", "vm")]

    # named match with rename, 4-part pattern
    (e,) = _exp("rack2/board1/NetworkPowerPort/power -> pwr")
    assert e.name == "rack2" and e.unavailable == 1


def test_offline_exporter():
    infos = _exp("vm/board1/*", "gone-host/x/*")
    assert [(i.name, i.online) for i in infos] == [("vm", True), ("gone-host", False)]
    assert _exp() == []


def test_filter_by_exporter():
    p = Place(name="x", matches=["rack2/board1/*"])
    terms = [t for e in place_exporters(p, parse_resources(RESOURCES)) for t in e.search_terms()]
    assert matches_filter(p, "exporter=rack2", terms)
    assert matches_filter(p, "exporter=*.lab.example.com", terms)
    assert not matches_filter(p, "exporter=vm", terms)
    assert matches_filter(p, "rack2", terms)


def test_exporter_rows_and_last_seen():
    from labgrid_tui.filtering import build_exporter_rows, is_unlinked
    from labgrid_tui.parser import parse_places

    places = parse_places("""\
Place 'kit1':
  matches:
    rack2/board1/*
  acquired: a/b
  acquired resources:
Place 'kit2':
  matches:
    rack9/x/*
  acquired: None
  acquired resources:
Place 'kit3':
  matches:
    */board1/NetworkService
  acquired: None
  acquired resources:
Place 'exporter-place-02':
  aliases: board-kit-1
  matches:
  acquired: None
  acquired resources:
""")
    resources = parse_resources(RESOURCES)
    infos = {p.name: place_exporters(p, resources) for p in places}
    seen = {}
    rows = {r.name: r for r in build_exporter_rows(places, resources, infos, seen)}
    assert set(rows) == {"rack2", "vm", "rack9"}  # wildcard "*" is not an exporter
    r2 = rows["rack2"]
    assert (r2.online, r2.fqdn, r2.resources, r2.unavailable) == (
        True,
        "rack2.lab.example.com",
        2,
        1,
    )
    assert r2.places == ["kit1", "kit3"] and r2.in_use == 1
    assert rows["vm"].fqdn == ""  # localhost is not a real hostname
    assert not rows["rack9"].online and rows["rack9"].places == ["kit2"]
    assert seen == {"rack2": "rack2.lab.example.com"}

    # rack2 goes offline: its resources vanish, the last-seen hostname stays
    infos = {p.name: place_exporters(p, []) for p in places}
    rows = {r.name: r for r in build_exporter_rows(places, [], infos, seen)}
    assert not rows["rack2"].online and rows["rack2"].fqdn == "rack2.lab.example.com"
    assert rows["rack2"].hostname_stale

    assert [p.name for p in places if is_unlinked(p)] == ["exporter-place-02"]
