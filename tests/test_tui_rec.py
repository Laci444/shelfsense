import asyncio

from shelfsense.tui import ShelfSenseApp


def test_tui_recommend_flow():
    from shelfsense import embeddings
    embeddings.embed(["warm up"])  # same as tui.run() does before the UI starts

    async def go():
        app = ShelfSenseApp()
        async with app.run_test(size=(160, 50)) as pilot:
            for _ in range(100):
                if app.advisor:
                    break
                await pilot.pause(0.1)
            app.action_tab("recommend")
            await pilot.pause(0.3)
            q = app.query_one("#query")
            q.focus()
            q.value = "epic fantasy with dragons"
            await pilot.press("enter")
            for _ in range(300):
                if app.query_one("#recs").row_count:
                    break
                await pilot.pause(0.1)
            assert app.query_one("#recs").row_count > 0
    asyncio.run(go())
