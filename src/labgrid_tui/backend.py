"""Async wrapper around the ``labgrid-client`` command, with retry handling.

Every labgrid-client call opens its own gRPC connection to the coordinator, so
"losing the connection" shows up as individual calls failing. Errors are split
into two kinds:

* ``ConnectionLost``: the coordinator could not be reached or the connection
  dropped (network blip, VPN reconnect, coordinator restart). Worth retrying.
* ``LabgridError``: a real answer from the coordinator ("already acquired",
  "matches nothing"). Retrying would not help.

Read-only calls are retried with exponential backoff. State-changing calls
(acquire, release, reserve, cancel) are *not* blindly repeated, because the
first attempt may have reached the coordinator before the connection dropped.
After a connection error they check the current state first, and only retry
if the change didn't happen.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .config import ConnectionConfig, Profile
from .parser import Place, Reservation, Resource, parse_places, parse_reservations, parse_resources


class LabgridError(RuntimeError):
    pass


class ConnectionLost(LabgridError):
    """The coordinator was unreachable or the connection dropped."""


# Messages labgrid-client / gRPC print when the coordinator isn't reachable.
_TRANSIENT_PATTERNS = [
    r"could not connect to coordinator",
    r"coordinator is unavailable",
    r"failed to connect to all addresses",
    r"connection refused",
    r"connection reset",
    r"broken pipe",
    r"socket closed",
    r"deadline.?exceeded",
    r"goaway",
    r"stream removed",
    r"network is unreachable",
    r"no route to host",
    r"name or service not known",
    r"temporary failure in name resolution",
    r"timed out",
]
_TRANSIENT_RE = re.compile("|".join(_TRANSIENT_PATTERNS), re.I)


def is_transient(message: str) -> bool:
    return bool(_TRANSIENT_RE.search(message))


# Who triggered a call: "refresh" (background polling) or "action" (user). Set by
# the app per asyncio task, so concurrent workers don't mix it up.
origin: contextvars.ContextVar[str] = contextvars.ContextVar("origin", default="action")


@dataclass
class CommandEvent:
    """One entry for the debug console / log file."""

    kind: str  # "ok" | "error" | "conn" (connection error) | "start" | "retry" | "info"
    command: str = ""  # shell-ready command line, env prefix included
    duration: float | None = None
    message: str = ""
    origin: str = "action"
    when: datetime = field(default_factory=datetime.now)


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


class Labgrid:
    def __init__(
        self,
        profile: Profile,
        executable: str = "labgrid-client",
        conn: ConnectionConfig | None = None,
    ):
        self.profile = profile
        self.executable = executable
        self.conn = conn or ConnectionConfig()
        self.listeners: list[Callable[[CommandEvent], None]] = []

    # -- plumbing -----------------------------------------------------------

    def base_args(self, place: str | None = None) -> list[str]:
        args = [self.executable, "-x", self.profile.effective_coordinator()]
        if self.profile.proxy:
            args += ["-P", self.profile.proxy]
        if place:
            args += ["-p", place]
        return args

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        # labgrid-client is a Python program: writing to a pipe, its output is
        # block-buffered, so `monitor` events would only arrive once ~8 KB piled up.
        # Unbuffered output makes live updates actually live.
        env["PYTHONUNBUFFERED"] = "1"
        if self.profile.username:
            env["LG_USERNAME"] = self.profile.username
        if self.profile.hostname:
            env["LG_HOSTNAME"] = self.profile.hostname
        return env

    @property
    def me(self) -> str:
        return self.profile.identity()

    def command(self, *args: str, place: str | None = None) -> list[str]:
        return self.base_args(place) + list(args)

    def shell_line(self, cmd: list[str]) -> str:
        """The command as you'd type it, including env vars the profile sets."""
        env = []
        if self.profile.username:
            env.append(f"LG_USERNAME={shlex.quote(self.profile.username)}")
        if self.profile.hostname:
            env.append(f"LG_HOSTNAME={shlex.quote(self.profile.hostname)}")
        return " ".join(env + [shlex.join(cmd)])

    def emit(
        self, kind: str, command: str = "", duration: float | None = None, message: str = ""
    ) -> None:
        event = CommandEvent(kind, command, duration, message, origin.get())
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                pass  # a broken listener must never break labgrid calls

    async def run(
        self, *args: str, place: str | None = None, timeout: float | None = None
    ) -> Result:
        """Run labgrid-client once. Raises ConnectionLost or LabgridError."""
        cmd = self.command(*args, place=place)
        line = self.shell_line(cmd)
        started = time.monotonic()
        if (timeout or 0) > self.conn.timeout:  # long-running (wait): log the start too
            self.emit("start", line, message="running…")
        try:
            res = await self._run(cmd, timeout)
        except ConnectionLost as e:
            self.emit("conn", line, time.monotonic() - started, str(e))
            raise
        except LabgridError as e:
            self.emit("error", line, time.monotonic() - started, str(e))
            raise
        except asyncio.CancelledError:
            self.emit("info", line, time.monotonic() - started, "cancelled")
            raise
        self.emit("ok", line, time.monotonic() - started, _summary(res.stdout))
        return res

    async def _run(self, cmd: list[str], timeout: float | None) -> Result:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=self.env(),
            )
        except FileNotFoundError as e:
            raise LabgridError(f"'{self.executable}' not found - is labgrid installed?") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout or self.conn.timeout)
        except TimeoutError:
            await _kill(proc)
            limit = timeout or self.conn.timeout
            raise ConnectionLost(f"timed out after {limit:.0f}s: {' '.join(cmd[1:])}") from None
        except asyncio.CancelledError:
            await _kill(proc)
            raise
        res = Result(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))
        if res.returncode != 0:
            raw = res.stderr or res.stdout
            msg = _clean_error(raw) or f"exit code {res.returncode}"
            raise (ConnectionLost if is_transient(raw) else LabgridError)(msg)
        return res

    def backoff(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (1-based): 0.5, 1, 2, 4 … capped."""
        return min(self.conn.retry_delay * 2 ** (attempt - 1), self.conn.max_retry_delay)

    async def run_retry(
        self, *args: str, place: str | None = None, retries: int | None = None
    ) -> Result:
        """Run a read-only command, retrying on connection errors."""
        retries = self.conn.retries if retries is None else retries
        attempt = 0
        while True:
            try:
                return await self.run(*args, place=place)
            except ConnectionLost:
                attempt += 1
                if attempt > retries:
                    raise
                delay = self.backoff(attempt)
                self.emit("retry", message=f"retry {attempt}/{retries} in {delay:.1f}s")
                await asyncio.sleep(delay)

    async def _change(self, do, done, describe: str):
        """Run a state-changing call safely.

        ``do()`` performs the change, ``done()`` returns True if the change is
        already in effect. After a connection error we first find out whether
        the change happened (the request may have reached the coordinator
        before the connection dropped) and only repeat ``do()`` if it didn't.
        """
        attempt = 0
        while True:
            try:
                return await do()
            except ConnectionLost as e:
                last = e
            while True:  # find out what happened before trying again
                attempt += 1
                if attempt > self.conn.retries:
                    raise ConnectionLost(f"{describe}: {last} (gave up after {attempt} attempts)")
                delay = self.backoff(attempt)
                self.emit(
                    "retry",
                    message=f"{describe}: checking whether it went through "
                    f"(attempt {attempt}/{self.conn.retries}, in {delay:.1f}s)",
                )
                await asyncio.sleep(delay)
                try:
                    if await done():
                        return None
                    break  # definitely not done -> repeat do()
                except ConnectionLost as e:
                    last = e  # still down, check again later

    # -- queries ------------------------------------------------------------

    async def places(self, retries: int | None = None) -> list[Place]:
        return parse_places((await self.run_retry("-v", "places", retries=retries)).stdout)

    async def reservations(self, retries: int | None = None) -> list[Reservation]:
        return parse_reservations((await self.run_retry("reservations", retries=retries)).stdout)

    async def resources(self, retries: int | None = None) -> list[Resource]:
        # -v -v also lists unavailable resources, so offline boards can be flagged
        return parse_resources(
            (await self.run_retry("-v", "-v", "resources", retries=retries)).stdout
        )

    async def show(self, place: str) -> str:
        return (await self.run_retry("show", place=place)).stdout

    async def _place(self, name: str) -> Place | None:
        places = parse_places((await self.run("-v", "places")).stdout)
        return next((p for p in places if p.name == name or name in p.aliases), None)

    async def _reservation(self, token: str) -> Reservation | None:
        res = parse_reservations((await self.run("reservations")).stdout)
        return next((r for r in res if r.token == token), None)

    # -- actions ------------------------------------------------------------

    async def acquire(self, place: str) -> None:
        async def done():
            p = await self._place(place)
            if p and p.acquired and p.acquired != self.me:
                raise LabgridError(f"place {place} was acquired by {p.acquired}")
            return bool(p and p.acquired == self.me)

        await self._change(lambda: self.run("acquire", place=place), done, f"acquire {place}")

    async def acquire_reservation(self, token: str) -> None:
        async def done():
            r = await self._reservation(token)
            return bool(r and r.state == "acquired")

        await self._change(
            lambda: self.run("acquire", place=f"+{token}"), done, f"acquire reservation {token}"
        )

    async def release(self, place: str) -> None:
        async def done():
            p = await self._place(place)
            return not (p and p.acquired == self.me)

        await self._change(lambda: self.run("release", place=place), done, f"release {place}")

    async def cancel_reservation(self, token: str) -> None:
        async def done():
            r = await self._reservation(token)
            return r is None or r.state in ("expired", "invalid")

        await self._change(lambda: self.run("cancel-reservation", token), done, f"cancel {token}")

    async def reserve(self, filters: list[str], prio: int = 0) -> Reservation:
        args = ["reserve", *(["--prio", str(prio)] if prio else []), *filters]
        wanted = " ".join(filters)
        before: set[str] | None = None
        found: list[Reservation] = []

        async def mine() -> list[Reservation]:
            return [
                r
                for r in parse_reservations((await self.run("reservations")).stdout)
                if r.owner == self.me
            ]

        async def do():
            nonlocal before
            if before is None:  # snapshot existing tokens before the first attempt
                before = {r.token for r in await mine()}
            parsed = parse_reservations((await self.run(*args)).stdout)
            if not parsed:
                raise LabgridError("could not parse reservation output")
            return parsed[0]

        async def done():
            # A reservation that wasn't there before means the first attempt
            # got through: adopt it instead of creating a duplicate.
            if before is None:
                return False
            for r in await mine():
                if (
                    r.token not in before
                    and r.filters.get("main") == wanted
                    and r.state not in ("expired", "invalid")
                ):
                    found.append(r)
                    return True
            return False

        result = await self._change(do, done, "reserve")
        return result if result is not None else found[0]

    async def monitor(
        self,
        on_event: Callable[[str, str, str], None],
        on_started: Callable[[], None] | None = None,
    ) -> None:
        """Run ``labgrid-client monitor`` and call ``on_event(kind, name, action)``
        for every change, e.g. ("place", "KIT1", "changed").

        Returns/raises only when the monitor stops. Note: when the coordinator
        goes away, labgrid-client monitor stays silent for ~10 s and then exits
        with code 0, so *any* exit means live updates are gone.
        """
        cmd = self.command("monitor")
        line = self.shell_line(cmd)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=self.env(),
            )
        except FileNotFoundError as e:
            raise LabgridError(f"'{self.executable}' not found - is labgrid installed?") from e
        self.emit("start", line, message="live updates")
        stderr_chunks: list[bytes] = []

        async def drain_stderr():  # keep the pipe from filling up
            while chunk := await proc.stderr.read(4096):
                stderr_chunks.append(chunk)

        drain = asyncio.create_task(drain_stderr())
        confirmed = False
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), 2.0)
                except TimeoutError:
                    raw = None
                if not confirmed and proc.returncode is None and time.monotonic() - started > 1.5:
                    confirmed = True  # survived connecting: live updates are on
                    if on_started:
                        on_started()
                if raw is None:
                    continue
                if not raw:
                    break  # EOF: monitor exited
                m = _EVENT_RE.match(raw.decode(errors="replace"))
                if m:
                    kind, name, action = m[1].lower(), m[2], m[3]
                    self.emit("event", message=f"{m[1]} {name} {action}")
                    on_event(kind, name, action)
        except asyncio.CancelledError:
            await _kill(proc)
            drain.cancel()
            self.emit("info", line, time.monotonic() - started, "live updates stopped")
            raise
        rc = await proc.wait()
        await drain
        err = b"".join(stderr_chunks).decode(errors="replace")
        msg = _clean_error(err) or f"monitor exited (code {rc})"
        self.emit("conn", line, time.monotonic() - started, msg)
        raise ConnectionLost(msg)

    async def wait(self, token: str, on_retry=None) -> str:
        """Block until the reservation leaves 'waiting'; return the final state.

        labgrid-client refreshes the reservation while waiting. If the
        connection drops, labgrid-client exits; we restart it quickly so the
        reservation doesn't time out (the coordinator expires it after about a
        minute without a refresh). Gives up with ConnectionLost once the
        coordinator has been unreachable for longer than ``wait_give_up``.
        Raises LabgridError if the reservation no longer exists.
        """
        failures = 0
        down_since: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                res = await self.run("wait", token, timeout=24 * 3600)
                states = re.findall(r"^state:\s*(\S+)", res.stdout, re.M)
                return states[-1] if states else "allocated"
            except ConnectionLost as e:
                if loop.time() - started > 5:  # it was connected for a while: fresh outage
                    failures, down_since = 0, None
                failures += 1
                down_since = down_since or loop.time()
                if loop.time() - down_since > self.conn.wait_give_up:
                    raise ConnectionLost(
                        f"coordinator unreachable while waiting for {token}: {e}"
                    ) from e
                if on_retry:
                    on_retry(failures, e)
                self.emit("retry", message=f"restarting wait for {token} (connection lost)")
                await asyncio.sleep(min(self.backoff(failures), 5.0))


_EVENT_RE = re.compile(r"^(Place|Resource) (.+?) (created|changed|deleted)\b")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess that may already have exited on its own.

    A timeout can race with the process finishing: kill() then raises
    ProcessLookupError, which must not crash the caller.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    finally:
        # Close the pipes even if this wait is cancelled too (app shutting down):
        # otherwise the transport is left to the garbage collector, which then
        # warns about an unclosed transport on an already closed event loop.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()


def _summary(stdout: str) -> str:
    """Short description of a successful call's output for the log."""
    text = stdout.strip()
    if not text:
        return ""
    places = text.count("Place '")
    if places:
        return f"{places} place(s)"
    if n := text.count("Reservation '"):
        return f"{n} reservation(s)"
    if n := text.count("Resource '"):
        return f"{n} resource(s)"
    first = text.splitlines()[0]
    return first if len(first) <= 80 else first[:77] + "…"


def _clean_error(text: str) -> str:
    """Reduce labgrid tracebacks/log noise to the last meaningful line."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    last = re.sub(r"^(ERROR|WARNING|INFO|DEBUG)\s+\S+:\s*", "", last)  # python logging prefix
    return re.sub(r"^(labgrid-client:\s*)?((server )?error:\s*)*", "", last, flags=re.I)
