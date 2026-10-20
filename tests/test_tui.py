import asyncio

from shelfsense import scenario
from shelfsense.tui import ShelfSenseApp

S = scenario.books()


async def ready(app, pilot):
    for _ in range(200):
        if app.advisor and not app.busy:
            return
        await pilot.pause(0.1)
    raise AssertionError("app did not connect")


async def until(pilot, cond, n=200):
    for _ in range(n):
        if cond():
            return
        await pilot.pause(0.1)
    raise AssertionError("condition not reached")


def verdict(app):
    return str(app.query_one("#verdict").render())


def test_tui_advise_flow():
    async def go():
        app = ShelfSenseApp()
        async with app.run_test(size=(160, 50)) as pilot:
            await ready(app, pilot)
            app.select_member("S_STD", "")
            app.select_book(S["none"], "")
            await pilot.pause(0.5)
            await pilot.click("#advise")
            await until(pilot, lambda: app.query_one("#alts").row_count)
            assert "WAITLIST" in verdict(app)
            assert "A copy is available" in str(app.query_one("#trace").render())
    asyncio.run(go())


def test_tui_sidebar_lists_and_scenarios():
    async def go():
        app = ShelfSenseApp()
        async with app.run_test(size=(160, 50)) as pilot:
            await ready(app, pilot)
            assert app.query_one("#members").row_count >= 6      # S_* members first
            assert app.query_one("#books").row_count == 8         # the scenario books
            app.action_tab("scenarios")
            await until(pilot, lambda: app._cases_loaded)
            await pilot.pause(0.3)
            await pilot.click("#run-all")
            await until(pilot, lambda: "correct" in str(app.query_one("#case-summary").render()))
            assert "15/15" in str(app.query_one("#case-summary").render())
    asyncio.run(go())


def test_tui_borrow_return_and_waitlist_promotion():
    """S_STD takes the last copy, S_PREM joins the waitlist, returning the loan serves S_PREM."""
    async def go():
        app = ShelfSenseApp()
        async with app.run_test(size=(160, 50)) as pilot:
            await ready(app, pilot)
            last = S["last"]
            app.select_book(last, "")
            app.select_member("S_STD", "")
            await pilot.pause(0.5)
            await pilot.click("#borrow")
            await until(pilot, lambda: "ALLOW" in verdict(app))
            app.select_member("S_PREM", "")
            await pilot.pause(0.5)
            await pilot.click("#borrow")
            await until(pilot, lambda: "WAITLIST" in verdict(app))
            app.select_member("S_STD", "")
            await ready(app, pilot)
            app.action_tab("librarian")
            await pilot.pause(0.5)
            app.query_one("#lib-return").press()
            await until(pilot, lambda: "S_PREM" in str(app.query_one("#lib-result").render()))
            assert app.advisor.pg.execute("SELECT count(*) FROM loans WHERE member_id='S_PREM' AND book_id=%s "
                                          "AND returned_at IS NULL", (last,)).fetchone()[0] == 1
            assert app.advisor.pg.execute("SELECT count(*) FROM waitlist WHERE book_id=%s", (last,)).fetchone()[0] == 0
    asyncio.run(go())
