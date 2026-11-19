import contextlib

from labgrid_tui.config import Profile
from labgrid_tui.ssh import ssh_command


def test_ssh_command_defaults():
    p = Profile("t")
    assert ssh_command(p, "exporter-01", "e01.example.com") == ["ssh", "e01.example.com"]
    assert ssh_command(p, "exporter-01") == ["ssh", "exporter-01"]


def test_ssh_command_user_and_jump():
    p = Profile("t", proxy="jump.lab", ssh_user="labadmin")
    assert ssh_command(p, "e1", "e1.lab") == ["ssh", "-J", "jump.lab", "labadmin@e1.lab"]
    p = Profile("t", proxy="jump.lab", ssh_jump="none")
    assert ssh_command(p, "e1", "e1.lab") == ["ssh", "e1.lab"]
    p = Profile("t", ssh_jump="other.jump")
    assert ssh_command(p, "e1", "e1.lab") == ["ssh", "-J", "other.jump", "e1.lab"]


def test_ssh_command_template():
    p = Profile(
        "t", ssh_user="root", ssh_command="ssh -t {target} 'journalctl -fu labgrid-exporter@{name}'"
    )
    assert ssh_command(p, "e1", "e1.lab") == [
        "ssh",
        "-t",
        "root@e1.lab",
        "journalctl -fu labgrid-exporter@e1",
    ]


async def test_keys_depend_on_tab(monkeypatch):
    import labgrid_tui.app as appmod
    from labgrid_tui.app import LabgridTUI
    from labgrid_tui.config import Config, UIConfig
    from labgrid_tui.parser import parse_places, parse_resources

    cfg = Config(
        {"t": Profile("t", coordinator="127.0.0.1:1", ssh_user="me")},
        "t",
        UIConfig(live_updates=False),
    )
    app = LabgridTUI(cfg)
    app.refresh_data = lambda: None
    ran = []
    monkeypatch.setattr(appmod.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(
        appmod.subprocess,
        "run",
        lambda cmd, env=None: ran.append(cmd) or type("R", (), {"returncode": 0}),
    )
    async with app.run_test(size=(140, 30)) as pilot:
        app.suspend = contextlib.nullcontext
        app.places = parse_places(
            "Place 'KIT1':\n  matches:\n    rack2/board1/*\n  acquired: None\n"
        )
        app.resources = parse_resources(
            "Exporter 'rack2':\n  Group 'board1' (rack2/board1/*):\n"
            "    Resource 'NetworkService' (rack2/board1/NetworkService[/NetworkService]):\n"
            "      {'avail': True, 'params': {'extra': {'proxy': 'rack2.example.com'}}}\n"
        )
        app.render_tables()

        def footer_keys():
            return {b.key for (_, b, enabled, _) in app.active_bindings.values() if enabled}

        assert "c" in footer_keys() and "s" not in footer_keys()
        await pilot.press("s")  # does nothing on the Places tab
        assert ran == []

        await pilot.press("2")
        await pilot.pause()
        keys = footer_keys()
        assert "s" in keys and "c" not in keys and "a" not in keys
        await pilot.press("c")  # console disabled here
        await pilot.press("s")
        assert ran == [["ssh", "me@rack2.example.com"]]
        assert any(e.command == "ssh me@rack2.example.com" for e in app.debug_events)

        await pilot.press("1")
        await pilot.pause()
        assert "c" in footer_keys()
