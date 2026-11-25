import asyncio
import json

from typer.testing import CliRunner

from shelfsense import scenario
from shelfsense.cli import app
from shelfsense.tui import ShelfSenseApp

from test_tui import ready, until

S = scenario.books()
runner = CliRunner()


def test_tui_explain_decision_panel_replays_the_stored_decision():
    async def go():
        app_ = ShelfSenseApp()
        async with app_.run_test(size=(160, 60)) as pilot:
            await ready(app_, pilot)
            app_.select_member("S_STD", "")
            app_.select_book(S["none"], "")
            await pilot.pause(0.5)
            await pilot.click("#borrow")
            await until(pilot, lambda: "WAITLIST" in str(app_.query_one("#verdict").render()))
            app_.action_tab("librarian")
            await until(pilot, lambda: app_.query_one("#decisions").row_count)
            await ready(app_, pilot)
            app_.query_one("#explain-decision").press()
            await until(pilot, lambda: "replay" in str(app_.query_one("#why").render()))
            text = str(app_.query_one("#why").render())
            assert "rule version" in text and "reproduces" in text
            app_.advisor.leave_waitlist("S_STD", S["none"])
    asyncio.run(go())


def test_cli_explain_json_for_a_stored_decision_and_a_live_check():
    out = runner.invoke(app, ["--json", "borrow", "S_STD", S["free1"]])
    assert out.exit_code == 0, out.output
    decision_id = json.loads(out.output)["decision_id"]
    stored = runner.invoke(app, ["--json", "explain", str(decision_id)])
    assert stored.exit_code == 0, stored.output
    assert json.loads(stored.output)["replay_ok"] is True
    live = runner.invoke(app, ["--json", "explain", "S_STD", S["free1"]])
    assert live.exit_code == 0 and json.loads(live.output)
    assert runner.invoke(app, ["return", "S_STD", S["free1"]]).exit_code == 0

def test_cli_unknown_decision_fails_cleanly():
    res = runner.invoke(app, ["explain", "999999999"])
    assert res.exit_code == 1


def test_cli_rules_list_json_has_one_active_version():
    res = runner.invoke(app, ["--json", "rules", "list"])
    assert res.exit_code == 0, res.output
    versions = json.loads(res.output)
    assert sum(1 for v in versions if v.get("valid_to") in (None, "None")) == 1
