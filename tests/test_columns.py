"""Column layout: widths, tag columns, wrap, resizing (no coordinator needed)."""

from labgrid_tui.app import LabgridTUI
from labgrid_tui.config import Config, Profile, UIConfig, load_config
from labgrid_tui.parser import parse_places

PLACES = """\
Place 'imx8-01':
  tags: board=imx8m, location=rack2-shelf3, power=gude3, ram=4g, rev=b, team=kernel, usb=ok
  matches:
    rack2/board1/*
  acquired: None
  acquired resources:
"""


def _app(**ui):
    cfg = Config(
        {"t": Profile("t", coordinator="127.0.0.1:1")},
        "t",
        UIConfig(**{"live_updates": False, **ui}),
    )
    app = LabgridTUI(cfg)
    app.refresh_data = lambda: None  # offline: we inject data ourselves
    return app


def _cols(app):
    t = app.query_one("#places")
    return {k.value: c for k, c in t.columns.items()}


async def test_tag_columns_and_widths():
    app = _app(tag_columns=["board"], column_widths={"tags": 20})
    async with app.run_test(size=(140, 30)) as pilot:
        app.places = parse_places(PLACES)
        app.render_tables()
        await pilot.pause()
        cols = _cols(app)
        assert "tag:board" in cols and str(cols["tags"].label) == "Other tags"
        assert cols["tags"].width == 20
        row = app.query_one("#places").get_row("imx8-01")
        keys = list(cols)
        assert str(row[keys.index("tag:board")]) == "imx8m"
        other = row[keys.index("tags")]
        assert "board=" not in str(other) and other.overflow == "ellipsis"


async def test_resize_wrap_hide():
    app = _app(column_widths={"tags": 30})
    async with app.run_test(size=(140, 30)) as pilot:
        app.places = parse_places(PLACES)
        app.render_tables()
        await pilot.press("greater_than_sign", "greater_than_sign")
        assert _cols(app)["tags"].width == 40
        await pilot.press("less_than_sign")
        assert _cols(app)["tags"].width == 35
        await pilot.press("w")
        await pilot.pause()
        table = app.query_one("#places")
        assert next(iter(table.rows.values())).height > 1
        await pilot.press("t")
        assert "tags" not in _cols(app)
        await pilot.press("t")
        assert "tags" in _cols(app)


def test_column_config(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text(
        '[ui]\ntag_columns = ["board"]\nwrap = true\n[ui.column_widths]\ntags = 25\nname = 12\n'
    )
    ui = load_config(f).ui
    assert ui.tag_columns == ["board"] and ui.wrap
    assert ui.column_widths == {"tags": 25, "comment": 30, "name": 12}


async def test_unlinked_hidden_and_tabs():
    app = _app()
    async with app.run_test(size=(140, 30)) as pilot:
        app.places = parse_places(
            PLACES + "Place 'exporter-place-02':\n  matches:\n  acquired: None\n"
        )
        app.render_tables()
        assert [p.name for p in app.visible_places()] == ["imx8-01"]
        await pilot.press("h")
        assert len(app.visible_places()) == 2
        await pilot.press("2")
        assert app.query_one("#tabs").active == "tab-exporters"
        assert app.selected_place() is None  # place actions don't act from here
        assert app.query_one("#exporters").row_count == 1  # rack2 (offline, from the match)
        await pilot.press("enter")
        assert app.query_one("#tabs").active == "tab-places"
        assert app.query_one("#filter").value == "exporter=rack2"


async def test_debug_console(tmp_path):
    from labgrid_tui.backend import CommandEvent

    log_file = tmp_path / "calls.log"
    app = _app(log_file=str(log_file))
    async with app.run_test(size=(140, 30)) as pilot:
        debug = app.query_one("#debug")
        assert not debug.display
        await pilot.press("d")
        assert debug.display
        app._on_command(
            CommandEvent("ok", "labgrid-client -v places", 0.4, "3 place(s)", "refresh")
        )
        app._on_command(
            CommandEvent("error", "labgrid-client -p k1 acquire", 0.5, "already acquired", "action")
        )
        app._on_command(CommandEvent("ok", "labgrid-client reservations", 0.3, "", "refresh"))
        assert len(debug.lines) == 3
        await pilot.press("D")
        assert len(debug.lines) == 1  # refresh calls hidden
        copied = []
        app.copy_to_clipboard = copied.append
        await pilot.press("y")
        assert copied == ["labgrid-client -p k1 acquire"]  # last *action*, not refresh
        lines = log_file.read_text().splitlines()
        assert (
            len(lines) == 3 and "\terror\t0.50s\taction\tlabgrid-client -p k1 acquire\t" in lines[1]
        )
