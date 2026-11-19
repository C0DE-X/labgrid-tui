"""Retry behaviour, tested with a scripted fake labgrid-client."""

import json
import stat
import sys
import textwrap

import pytest

from labgrid_tui.backend import ConnectionLost, Labgrid, LabgridError, is_transient
from labgrid_tui.config import ConnectionConfig, Profile

# Real messages captured from labgrid-client 26.0.1
REFUSED = (
    "ERROR              root: coordinator is unavailable: failed to connect to all addresses; "
    "last error: UNKNOWN: ipv4:127.0.0.1:20555: Failed to connect to remote host: "
    "Connection refused\nServer error: Error: Could not connect to coordinator"
)
DROPPED = (
    "Server error: Error: failed to connect to all addresses; last error: UNKNOWN: "
    "ipv4:127.0.0.1:20555: Failed to connect to remote host: Connection refused"
)


def test_classification():
    assert is_transient(REFUSED)
    assert is_transient(DROPPED)
    assert is_transient("StatusCode.UNAVAILABLE: Socket closed")
    assert not is_transient("labgrid-client: Error: Place imx8-01 is already acquired by vm/root.")
    assert not is_transient("labgrid-client: Error: place pattern nope matches nothing")
    assert not is_transient("Server error: Error: Reservation W92Y32QKUS does not exist")


PLACES_FREE = "Place 'b1':\n  tags: board=x\n  matches:\n  acquired: None\n  acquired resources:\n"
PLACES_MINE = "Place 'b1':\n  tags: board=x\n  matches:\n  acquired: h/u\n  acquired resources:\n"


@pytest.fixture
def fake(tmp_path):
    """A fake labgrid-client driven by a JSON script of responses per command.

    script = {"acquire": [["fail", REFUSED], ["ok", "acquired place b1"]], ...}
    Each call pops the next response for its command (the last one repeats) and
    appends the command to calls.json.
    """
    state = tmp_path / "state.json"
    calls = tmp_path / "calls.json"
    exe = tmp_path / "labgrid-client"
    exe.write_text(
        textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        args = sys.argv[1:]
        while args and args[0] in ("-x", "-P", "-p"):
            args = args[2:]
        while args and args[0] == "-v":
            args = args[1:]
        cmd = args[0]
        st = json.load(open({str(state)!r}))
        calls = json.load(open({str(calls)!r}))
        calls.append(cmd)
        json.dump(calls, open({str(calls)!r}, "w"))
        queue = st.setdefault(cmd, [["ok", ""]])
        kind, text = queue.pop(0) if len(queue) > 1 else queue[0]
        json.dump(st, open({str(state)!r}, "w"))
        if kind == "ok":
            print(text)
        else:
            print(text, file=sys.stderr)
            sys.exit(1)
        """)
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    calls.write_text("[]")

    def make(script):
        state.write_text(json.dumps(script))
        conn = ConnectionConfig(retries=3, retry_delay=0.01, max_retry_delay=0.02, wait_give_up=0.3)
        lg = Labgrid(Profile("t", coordinator="x:1", username="u", hostname="h"), str(exe), conn)
        lg.calls = lambda: json.loads(calls.read_text())
        return lg

    return make


async def test_read_retries_then_succeeds(fake):
    lg = fake({"places": [["fail", REFUSED], ["fail", REFUSED], ["ok", PLACES_FREE]]})
    places = await lg.places()
    assert places[0].name == "b1"
    assert lg.calls() == ["places"] * 3


async def test_read_gives_up(fake):
    lg = fake({"places": [["fail", REFUSED]]})
    with pytest.raises(ConnectionLost):
        await lg.places()
    assert lg.calls() == ["places"] * 4  # 1 + 3 retries


async def test_real_error_not_retried(fake):
    lg = fake(
        {"acquire": [["fail", "labgrid-client: Error: Place b1 is already acquired by x/y."]]}
    )
    with pytest.raises(LabgridError) as e:
        await lg.acquire("b1")
    assert not isinstance(e.value, ConnectionLost)
    assert "already acquired" in str(e.value)
    assert lg.calls() == ["acquire"]


async def test_acquire_not_repeated_if_it_went_through(fake):
    # connection dropped after the coordinator processed the acquire
    lg = fake({"acquire": [["fail", DROPPED]], "places": [["ok", PLACES_MINE]]})
    await lg.acquire("b1")
    assert lg.calls() == ["acquire", "places"]


async def test_acquire_repeated_if_it_did_not_go_through(fake):
    lg = fake(
        {
            "acquire": [["fail", REFUSED], ["ok", "acquired place b1"]],
            "places": [["fail", REFUSED], ["ok", PLACES_FREE]],
        }
    )
    await lg.acquire("b1")
    # first check fails (still down), second says "not acquired" -> acquire again
    assert lg.calls() == ["acquire", "places", "places", "acquire"]


async def test_reserve_adopts_reservation_instead_of_duplicating(fake):
    res = (
        "Reservation 'NEWTOKEN':\n  owner: h/u\n  token: NEWTOKEN\n  state: waiting\n"
        "  filters:\n    main: board=x\n"
    )
    lg = fake(
        {
            "reservations": [["ok", ""], ["ok", res]],  # snapshot: none, then: new one
            "reserve": [["fail", DROPPED]],
        }
    )
    r = await lg.reserve(["board=x"])
    assert r.token == "NEWTOKEN"
    assert lg.calls().count("reserve") == 1


async def test_wait_survives_drop(fake):
    lg = fake(
        {"wait": [["fail", DROPPED], ["fail", REFUSED], ["ok", "token: T\nstate: allocated"]]}
    )
    retries = []
    assert await lg.wait("T", on_retry=lambda n, e: retries.append(n)) == "allocated"
    assert retries == [1, 2]


async def test_wait_reports_expired_and_gives_up(fake):
    lg = fake({"wait": [["ok", "state: waiting\nstate: expired"]]})
    assert await lg.wait("T") == "expired"
    lg = fake({"wait": [["fail", REFUSED]]})
    with pytest.raises(ConnectionLost):
        await lg.wait("T")


async def test_command_events(fake):
    from labgrid_tui.backend import origin

    lg = fake(
        {
            "places": [["fail", REFUSED], ["ok", PLACES_FREE]],
            "acquire": [["fail", "labgrid-client: Error: Place b1 is already acquired by x/y."]],
        }
    )
    events = []
    lg.listeners.append(events.append)
    await lg.places()
    with pytest.raises(LabgridError):
        await lg.acquire("b1")
    assert [e.kind for e in events] == ["conn", "retry", "ok", "error"]
    assert events[0].command.startswith("LG_USERNAME=u LG_HOSTNAME=h ")
    assert events[0].command.endswith("-x x:1 -v places")
    assert events[2].message == "1 place(s)" and events[2].duration is not None
    assert "already acquired" in events[3].message and "-p b1 acquire" in events[3].command
    assert all(e.origin == "action" for e in events)

    token = origin.set("refresh")
    try:
        await lg.places()
    finally:
        origin.reset(token)
    assert events[-1].origin == "refresh"


async def test_broken_listener_does_not_break_calls(fake):
    lg = fake({"places": [["ok", PLACES_FREE]]})
    lg.listeners.append(lambda e: 1 / 0)
    assert len(await lg.places()) == 1


async def test_monitor_events_then_exit(fake):
    out = (
        'Place KIT-A created: name: "KIT-A"\n  acquired: ""\n'
        "Place KIT-A changed:\n"
        "Resource rack1/board1/NetworkService/NetworkService changed:\n"
        "Resource rack2/board2/???/NetworkService deleted\n"
    )
    lg = fake({"monitor": [["ok", out]]})
    events, started = [], []
    with pytest.raises(ConnectionLost):  # any exit = live updates gone (labgrid exits 0!)
        await lg.monitor(lambda *e: events.append(e), on_started=lambda: started.append(1))
    assert events == [
        ("place", "KIT-A", "created"),
        ("place", "KIT-A", "changed"),
        ("resource", "rack1/board1/NetworkService/NetworkService", "changed"),
        ("resource", "rack2/board2/???/NetworkService", "deleted"),
    ]
    assert started == []  # exited right away: never counted as live


def test_clean_error_strips_log_prefix():
    from labgrid_tui.backend import _clean_error

    assert (
        _clean_error("ERROR              root: coordinator became unavailable: Cancelling")
        == "coordinator became unavailable: Cancelling"
    )


async def test_kill_already_exited_process():
    """Timeout racing with the process exiting must not raise ProcessLookupError."""
    from labgrid_tui.backend import _kill

    class ExitedProc:
        waited = False

        def kill(self):
            raise ProcessLookupError  # what asyncio raises when the process is gone

        async def wait(self):
            self.waited = True
            return 0

    proc = ExitedProc()
    await _kill(proc)  # must not raise
    assert proc.waited


def test_subprocess_output_is_unbuffered(monkeypatch):
    """labgrid-client must flush every line, or monitor events arrive minutes late."""
    from labgrid_tui.backend import Labgrid
    from labgrid_tui.config import Profile

    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    assert Labgrid(Profile("t")).env()["PYTHONUNBUFFERED"] == "1"
