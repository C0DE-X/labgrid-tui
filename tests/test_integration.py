"""End-to-end test: real labgrid-coordinator + headless Textual app."""

import asyncio
import os
import shutil
import socket
import subprocess
import time

import pytest

from labgrid_tui.app import LabgridTUI
from labgrid_tui.config import Config, Profile, UIConfig

pytestmark = pytest.mark.skipif(
    not (shutil.which("labgrid-coordinator") and shutil.which("labgrid-client")),
    reason="labgrid not installed",
)


def _stop(proc) -> None:
    """Stop a coordinator; with a live monitor attached it may not exit on SIGTERM quickly."""
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def coordinator(tmp_path):
    port = _free_port()
    addr = f"127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["labgrid-coordinator", "--listen", addr],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = {**os.environ, "LG_COORDINATOR": addr}

    def lg(*args, user="tester"):
        return subprocess.run(
            ["labgrid-client", *args],
            env={**env, "LG_USERNAME": user, "LG_HOSTNAME": "testhost"},
            check=True,
            capture_output=True,
            text=True,
        )

    for _ in range(50):
        try:
            lg("places")
            break
        except subprocess.CalledProcessError:
            time.sleep(0.2)
    for name, tags in [
        ("imx8-01", ["board=imx8m"]),
        ("imx8-02", ["board=imx8m"]),
        ("rpi4-01", ["board=rpi4"]),
    ]:
        lg("-p", name, "create")
        lg("-p", name, "set-tags", *tags)
    lg("-p", "rpi4-01", "acquire", user="someoneelse")
    yield addr, lg
    _stop(proc)


def _app(addr):
    profile = Profile(name="test", coordinator=addr, username="tester", hostname="testhost")
    cfg = Config(
        profiles={"test": profile},
        default_profile="test",
        ui=UIConfig(
            refresh_seconds=60,
            saved_filters=["board=rpi4"],
            hide_unlinked=False,
            live_updates=False,
        ),
    )  # test places have no exporters
    return LabgridTUI(cfg)


async def _wait_for(pilot, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        await pilot.pause(0.1)
        if cond():
            return
    raise AssertionError("condition not reached")


async def test_full_flow(coordinator):
    addr, lg = coordinator
    app = _app(addr)
    async with app.run_test(size=(140, 40)) as pilot:
        table = app.query_one("#places")
        await _wait_for(pilot, lambda: table.row_count == 3)
        assert app.connected

        # filter by tag via saved filter F1
        await pilot.press("f1")
        await _wait_for(pilot, lambda: table.row_count == 1)
        assert app.selected_place().name == "rpi4-01"

        # acquiring someone else's place is refused locally
        await pilot.press("a")
        await pilot.pause(0.3)
        assert app.selected_place().acquired == "testhost/someoneelse"

        # typing in the filter box must not trigger single-letter bindings
        await pilot.press("slash")
        app.query_one("#filter").value = ""
        await pilot.press(*"name=imx8-01")
        await pilot.press("enter")
        await _wait_for(pilot, lambda: table.row_count == 1)
        assert app.selected_place().name == "imx8-01"
        assert app.selected_place().acquired is None

        # acquire -> shows up as mine
        await pilot.press("a")
        await _wait_for(
            pilot, lambda: (p := app.selected_place()) and p.acquired == "testhost/tester"
        )
        mine = app.query_one("#mine")
        await _wait_for(pilot, lambda: mine.row_count == 1)

        # release
        await pilot.press("u")
        await _wait_for(pilot, lambda: app.selected_place().acquired is None)

        # reserve by tag -> waits, allocates and auto-acquires an imx8 board
        app.query_one("#filter").value = "board=imx8m"
        await pilot.pause(0.2)
        await pilot.press("r")
        await pilot.pause(0.3)
        assert app.screen.query_one("#reserve-input").value == "board=imx8m"
        await pilot.press("enter")

        def got_imx8():
            return any(
                p.acquired == "testhost/tester" and p.tags.get("board") == "imx8m"
                for p in app.places
            )

        await _wait_for(pilot, got_imx8, timeout=20)

        # details modal
        app.query_one("#places").focus()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: app.screen.__class__.__name__ == "DetailScreen")
        await pilot.press("escape")


async def test_disconnected_shows_error():
    app = _app(f"127.0.0.1:{_free_port()}")
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.connected is False, timeout=20)
        assert app.last_error


async def test_survives_coordinator_restart(tmp_path):
    """Coordinator dies while a reservation is waiting, then comes back."""
    from labgrid_tui.config import ConnectionConfig

    addr = f"127.0.0.1:{_free_port()}"

    def start():
        return subprocess.Popen(
            ["labgrid-coordinator", "--listen", addr],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def lg(*args, user="tester", check=True):
        return subprocess.run(
            ["labgrid-client", "-x", addr, *args],
            env={**os.environ, "LG_USERNAME": user, "LG_HOSTNAME": "testhost"},
            check=check,
            capture_output=True,
            text=True,
        )

    def setup_lab():
        for _ in range(50):
            if lg("places", check=False).returncode == 0:
                break
            time.sleep(0.2)
        lg("-p", "b1", "create")
        lg("-p", "b1", "set-tags", "board=x")
        lg("-p", "b1", "acquire", user="other")  # board busy -> our reservation waits

    coord = start()
    setup_lab()
    profile = Profile(name="t", coordinator=addr, username="tester", hostname="testhost")
    cfg = Config(
        profiles={"t": profile},
        default_profile="t",
        ui=UIConfig(refresh_seconds=1),
        connection=ConnectionConfig(
            timeout=5, retries=2, retry_delay=0.2, max_retry_delay=1, wait_give_up=60
        ),
    )
    app = LabgridTUI(cfg)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await _wait_for(pilot, lambda: app.connected is True)
            app.reserve_and_wait(["board=x"])
            await _wait_for(pilot, lambda: any(r.state == "waiting" for r in app.reservations))

            _stop(coord)
            await _wait_for(pilot, lambda: app.connected is False, timeout=20)
            status = str(app.query_one("#status").render())
            assert "disconnected" in status and "stale" in status

            await pilot.pause(3)  # stay down for a bit
            coord = start()
            setup_lab()  # coordinator is back (state restored)
            await _wait_for(pilot, lambda: app.connected is True, timeout=30)

            # the old reservation died with the coordinator; a new one must be waiting
            await _wait_for(
                pilot, lambda: any(r.state == "waiting" for r in app.reservations), timeout=30
            )
            lg("-p", "b1", "release", user="other")
            await _wait_for(
                pilot, lambda: any(p.acquired == "testhost/tester" for p in app.places), timeout=30
            )
    finally:
        _stop(coord)


class _FlakyProxy:
    """TCP proxy that can drop all connections and refuse new ones (simulated network blip)."""

    def __init__(self, target_port: int):
        self.target_port = target_port
        self.port = _free_port()
        self.server = None
        self.writers = set()

    async def _pipe(self, reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def _handle(self, r1, w1):
        try:
            r2, w2 = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            w1.close()
            return
        self.writers |= {w1, w2}
        await asyncio.gather(self._pipe(r1, w2), self._pipe(r2, w1))

    async def up(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def down(self):
        self.server.close()
        for w in self.writers:
            w.transport.abort()
        self.writers.clear()
        await self.server.wait_closed()


async def test_survives_network_blip(tmp_path):
    from labgrid_tui.config import ConnectionConfig

    cport = _free_port()
    coord = subprocess.Popen(
        ["labgrid-coordinator", "--listen", f"127.0.0.1:{cport}"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def lg(*args, user="tester", check=True):
        return subprocess.run(
            ["labgrid-client", "-x", f"127.0.0.1:{cport}", *args],
            env={**os.environ, "LG_USERNAME": user, "LG_HOSTNAME": "testhost"},
            check=check,
            capture_output=True,
            text=True,
        )

    for _ in range(50):
        if lg("places", check=False).returncode == 0:
            break
        time.sleep(0.2)
    lg("-p", "b1", "create")
    lg("-p", "b1", "set-tags", "board=x")
    lg("-p", "b1", "acquire", user="other")

    proxy = _FlakyProxy(cport)
    await proxy.up()
    profile = Profile(
        name="t", coordinator=f"127.0.0.1:{proxy.port}", username="tester", hostname="testhost"
    )
    cfg = Config(
        profiles={"t": profile},
        default_profile="t",
        ui=UIConfig(refresh_seconds=1),
        connection=ConnectionConfig(
            timeout=5, retries=2, retry_delay=0.2, max_retry_delay=1, wait_give_up=60
        ),
    )
    app = LabgridTUI(cfg)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await _wait_for(pilot, lambda: app.connected is True)
            app.reserve_and_wait(["board=x"])
            await _wait_for(pilot, lambda: any(r.state == "waiting" for r in app.reservations))
            token = next(r.token for r in app.reservations if r.state == "waiting")

            await proxy.down()  # network gone (open calls are cut)
            await _wait_for(pilot, lambda: app.connected is False, timeout=20)
            await pilot.pause(5)
            await proxy.up()  # network back
            await _wait_for(pilot, lambda: app.connected is True, timeout=30)

            lg("-p", "b1", "release", user="other")
            await _wait_for(
                pilot, lambda: any(p.acquired == "testhost/tester" for p in app.places), timeout=30
            )
            # the same reservation survived the blip – no duplicate was created
            tokens = {r.token for r in app.reservations if r.state not in ("expired", "invalid")}
            assert tokens == {token}
    finally:
        await proxy.down()
        _stop(coord)


async def test_live_updates(coordinator):
    """With live updates, a change by someone else shows up without polling."""
    addr, lg = coordinator
    profile = Profile(name="t", coordinator=addr, username="tester", hostname="testhost")
    cfg = Config(
        profiles={"t": profile},
        default_profile="t",
        ui=UIConfig(
            refresh_seconds=300,
            fallback_refresh_seconds=300,
            hide_unlinked=False,
            live_updates=True,
        ),
    )
    app = LabgridTUI(cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await _wait_for(pilot, lambda: app.live and app.connected, timeout=15)

        def calls():
            return sum(1 for e in app.debug_events if e.command and e.kind == "ok")

        await pilot.pause(3)  # let the catch-up refresh after going live finish
        before = calls()
        await pilot.pause(3)
        assert calls() == before  # idle: no polling at all
        lg("-p", "imx8-02", "acquire", user="colleague")
        await _wait_for(
            pilot, lambda: any(p.name == "imx8-02" and p.acquired for p in app.places), timeout=10
        )
        assert "live" in str(app.query_one("#status").render())
