"""Space menu: lists every key for the current tab and runs the one pressed."""

from labgrid_tui.app import LabgridTUI
from labgrid_tui.config import Config, Profile, UIConfig
from labgrid_tui.keymenu import KeyMenu, key_label
from labgrid_tui.parser import parse_places

PLACES = "Place 'KIT1':\n  tags: board=x\n  matches:\n    rack2/board1/*\n  acquired: None\n"


def _app(**ui):
    cfg = Config(
        {"t": Profile("t", coordinator="127.0.0.1:1")},
        "t",
        UIConfig(**{"live_updates": False, **ui}),
    )
    app = LabgridTUI(cfg)
    app.refresh_data = lambda: None
    return app


def _keys(app):
    return {k for e in app.menu_entries() for k in e.keys}


def test_key_labels():
    assert key_label("less_than_sign") == "<"
    assert key_label("greater_than_sign") == ">"
    assert key_label("ctrl+r") == "Ctrl+R"
    assert key_label("f5") == "F5"
    assert key_label("D") == "D"


async def test_menu_lists_hidden_keys_per_tab():
    app = _app(saved_filters=["board=x"])
    async with app.run_test(size=(140, 40)) as pilot:
        keys = _keys(app)
        # keys hidden from the footer are in the menu
        assert {"less_than_sign", "greater_than_sign", "h", "w", "t", "o", "D", "y"} <= keys
        assert {"a", "u", "c"} <= keys and "s" not in keys
        assert "f1" in keys and "f2" not in keys  # only configured saved filters
        labels = {e.label for e in app.menu_entries()}
        assert "Filter: board=x" in labels

        await pilot.press("2")
        await pilot.pause()
        keys = _keys(app)
        assert "s" in keys and not {"a", "c", "less_than_sign"} & keys


async def test_menu_runs_pressed_key_and_closes():
    app = _app(column_widths={"tags": 30})
    async with app.run_test(size=(140, 40)) as pilot:
        app.places = parse_places(PLACES)
        app.render_tables()
        await pilot.press("space")
        await pilot.pause()
        assert isinstance(app.screen, KeyMenu)
        await pilot.press("greater_than_sign")  # "Tags wider", hidden from the footer
        await pilot.pause()
        assert not isinstance(app.screen, KeyMenu)
        assert app.col_widths["tags"] == 35

        await pilot.press("question_mark")  # alias
        await pilot.pause()
        entry = next(e for e in app.screen.entries if e.action == "toggle_free")
        assert entry.state == "off"
        await pilot.press("f")
        await pilot.pause()
        assert app.free_only

        # Esc and Space just close
        for close in ("escape", "space"):
            await pilot.press("space")
            await pilot.pause()
            await pilot.press(close)
            await pilot.pause()
            assert not isinstance(app.screen, KeyMenu)
        assert app.free_only and app.col_widths["tags"] == 35  # nothing else ran


async def test_menu_key_disabled_on_tab_does_nothing():
    ran = []
    app = _app()
    async with app.run_test(size=(140, 40)) as pilot:
        app.action_console = lambda: ran.append("console")
        await pilot.press("2", "space")
        await pilot.pause()
        await pilot.press("c")  # console isn't available on the Exporters tab
        await pilot.pause()
        assert not isinstance(app.screen, KeyMenu) and ran == []


async def test_space_in_filter_is_typed_not_menu():
    app = _app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", "a", "space", "b")
        assert not isinstance(app.screen, KeyMenu)
        assert app.query_one("#filter").value == "a b"
