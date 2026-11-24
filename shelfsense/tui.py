"""Full-screen terminal UI (Textual) on top of the Advisor.

Sidebar: pick a member and a book (live search). Tabs: Advise, Recommend, Book, Member, Librarian, Stores,
Scenarios, Demos. All database calls run in background threads (one at a time) so the UI never freezes.
Press F1 inside the app for the key guide.
"""
import contextlib
import json
import threading
import time
from pathlib import Path

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, RichLog, Select, Static,
                             TabbedContent, TabPane)

from shelfsense import scenario
from shelfsense.config import DATA_DIR
from shelfsense.stores import postgres


def find_books(adv, text, limit=20):
    return postgres.search_books(adv.pg, text, limit)


COLOURS = {"allow": "green", "waitlist": "yellow", "reject": "red", "needs_info": "cyan"}
ICONS = {"allow": "✔", "waitlist": "⏳", "reject": "✘", "needs_info": "?"}
TABS = [("advise", "Advise"), ("recommend", "Recommend"), ("book", "Book"), ("member", "Member"),
        ("librarian", "Librarian"), ("stores", "Stores"), ("scenarios", "Scenarios"), ("demos", "Demos")]
DEMOS = {"join": ("eval.demo_join", "run"), "concurrency": ("eval.demo_concurrency", "run"),
         "stale": ("eval.demo_stale_cache", "run"), "update": ("eval.demo_update_across", "run"),
         "vectors": ("eval.eval_vectors", "main")}
THEMES = ["tokyo-night", "nord", "gruvbox", "dracula", "catppuccin-mocha", "textual-dark", "textual-light"]

HELP = """[b]ShelfSense - key guide[/b]

[b]Workflow[/b]  1. pick a member (left, top)   2. pick a book (left, bottom)   3. use a tab
Type in a search box to filter; press Enter (or click) on a row to select it.
Rows in the Advise alternatives, Recommend, Book, Member and Scenarios tables select a book/member the same way.

[b]Tabs[/b]  (F2-F9 or click)
 F2 Advise      decision, the rules that were checked, alternatives, real borrow
 F3 Recommend   free-text search (Qdrant) limited to books with a free copy
 F4 Book        what each store knows about the book + similar books
 F5 Member      loans, waitlist, history; return a loan or leave a waitlist
 F6 Librarian   return a book (the waitlist is served), decision log
 F7 Stores      health, counts, Redis cache entries, reset data
 F8 Scenarios   the 15 loan test cases (run one or all)
 F9 Demos       concurrency, stale cache, update across stores, join, vector evaluation

[b]Keys[/b]  F1 help   Ctrl+T theme   Ctrl+E export last answer to JSON   Ctrl+R refresh   Ctrl+Q quit

[b]Colours[/b]  [green]allow[/green]   [yellow]waitlist[/yellow]   [red]reject[/red]
Advise only reads. [b]Borrow[/b] writes a real loan/waitlist entry to PostgreSQL; Stores > Reset data undoes it."""


class Confirm(ModalScreen[bool]):
    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(self.text)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="no")
                yield Button("Yes", id="yes", variant="error")

    def on_button_pressed(self, e):
        self.dismiss(e.button.id == "yes")


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("f1", "dismiss", "Close")]

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(HELP)
            yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, e):
        self.dismiss()


class _LogWriter:
    """File-like object: sends printed lines to the demo log (called from a worker thread)."""

    def __init__(self, app, log):
        self.app, self.log, self.buf = app, log, ""

    def write(self, s):
        self.buf += s
        *lines, self.buf = self.buf.split("\n")
        for ln in lines:
            self.app.call_from_thread(self.log.write, Text(ln))
        return len(s)

    def flush(self):
        pass


def fill(table, rows, keys=None):
    table.clear()
    for i, r in enumerate(rows):
        table.add_row(*[c if isinstance(c, Text) else Text(str(c)) for c in r],
                      key=str(keys[i]) if keys else None)


def free_cell(available, total=None):
    text = f"{available}/{total}" if total is not None else str(available)
    return Text(text, style="green" if available > 1 else "yellow" if available == 1 else "red")


def outcome_cell(outcome):
    return Text(f"{ICONS[outcome]} {outcome}", style=COLOURS[outcome])


def book_rows(rows):
    return [[r["book_id"], r["title"][:45], r["genre"], free_cell(r["available"]),
             ", ".join(r["authors"][:2])[:30], r.get("source", "")] for r in rows], [r["book_id"] for r in rows]


def kv(rows):
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold")
    g.add_column()
    for k, v in rows:
        g.add_row(k, str(v))
    return g


class ShelfSenseApp(App):
    TITLE = "ShelfSense"
    SUB_TITLE = "library loan advisor"
    CSS = """
    #side { width: 44; padding: 0 1; border-right: heavy $primary 50%; }
    #main { padding: 0 1; }
    #side Input, #main Input { margin: 0 0 1 0; }
    .title { text-style: bold; color: $accent; margin-top: 1; }
    #members { height: 1fr; min-height: 5; }
    #books { height: 1fr; min-height: 5; }
    #selected { height: auto; margin: 1 0; padding: 0 1; border: round $accent; }
    #health { height: 1; margin-bottom: 1; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $boost; color: $text-muted; }
    DataTable { border: round $primary 60%; }
    DataTable:focus { border: round $accent; }
    .buttons { height: auto; margin-bottom: 1; }
    .buttons Button { margin-right: 1; }
    #verdict { height: 5; padding: 1 2; margin-bottom: 1; text-style: bold; background: $boost; }
    #verdict.allow { background: $success 25%; border-left: thick $success; }
    #verdict.waitlist { background: $warning 25%; border-left: thick $warning; }
    #verdict.needs_info { background: $primary 25%; border-left: thick $primary; }
    #verdict.reject { background: $error 25%; border-left: thick $error; }
    #trace-box { width: 46; margin-right: 1; }
    #trace { padding: 0 1; border: round $primary 60%; height: auto; }
    #alts, #recs, #similar, #loans, #wl, #history, #decisions, #cache, #cases, #bookwl { height: 1fr; min-height: 4; }
    #book-info, #member-info, #store-stats { height: auto; }
    #demo-log { border: round $primary 60%; height: 1fr; }
    Select { margin-bottom: 1; width: 30; }
    #case-summary { padding: 1 2; }
    #dialog { width: 80; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    Confirm, HelpScreen { align: center middle; }
    """
    BINDINGS = [Binding("f1", "help", "Help"), Binding("ctrl+t", "theme", "Theme"),
                Binding("ctrl+e", "export", "Export"), Binding("ctrl+r", "refresh_all", "Refresh"),
                Binding("ctrl+q", "quit", "Quit"), Binding("ctrl+l", "clear", "Clear", show=False)] + [
        Binding(f"f{i + 2}", f"tab('{t[0]}')", t[1], show=False) for i, t in enumerate(TABS)]

    def __init__(self):
        super().__init__()
        self.advisor = None
        self.member = self.book = None
        self.last = None
        self.db_lock = threading.Lock()
        self.busy = 0
        self._debounce = {}
        self._cases_loaded = False
        self._cases = {}
        self._members = {}
        self._books = {}

    # --- layout ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="side"):
                yield Static("MEMBER", classes="title")
                yield Input(placeholder="search id or name", id="member-filter")
                yield DataTable(id="members", cursor_type="row", zebra_stripes=True)
                yield Static("BOOK", classes="title")
                yield Input(placeholder="search title", id="title")
                yield DataTable(id="books", cursor_type="row", zebra_stripes=True)
                yield Static("", id="selected")
            with Vertical(id="main"):
                yield Static("connecting...", id="health")
                with TabbedContent(id="tabs"):
                    with TabPane("Advise", id="t-advise"):
                        with Horizontal(classes="buttons"):
                            yield Button("Advise", id="advise", variant="primary")
                            yield Button("Borrow", id="borrow", variant="warning")
                            yield Button("Export JSON", id="export")
                        yield Static("Pick a member and a book, then press Advise.", id="verdict")
                        with Horizontal():
                            with Vertical(id="trace-box"):
                                yield Static("Rules checked", classes="title")
                                yield Static("", id="trace")
                            with Vertical():
                                yield Static("Alternatives with a free copy", classes="title")
                                yield DataTable(id="alts", cursor_type="row", zebra_stripes=True)
                    with TabPane("Recommend", id="t-recommend"):
                        yield Input(placeholder="describe what you want to read, Enter to search", id="query")
                        with Horizontal(classes="buttons"):
                            yield Select([], prompt="Any genre", id="genre", allow_blank=True)
                            yield Button("Search", id="search", variant="primary")
                        yield DataTable(id="recs", cursor_type="row", zebra_stripes=True)
                    with TabPane("Book", id="t-book"):
                        with Horizontal(classes="buttons"):
                            yield Button("Find similar books", id="find-similar", variant="primary")
                            yield Button("Advise this book", id="to-advise")
                        with VerticalScroll():
                            yield Static("Select a book.", id="book-info")
                            yield Static("Similar books (same author, also borrowed, similar text)", classes="title")
                            yield DataTable(id="similar", cursor_type="row", zebra_stripes=True)
                    with TabPane("Member", id="t-member"):
                        with Horizontal(classes="buttons"):
                            yield Button("Return selected loan", id="return-loan", variant="warning")
                            yield Button("Leave selected waitlist", id="leave-wl")
                            yield Button("Refresh", id="refresh-member")
                        yield Static("Select a member.", id="member-info")
                        yield Static("Active loans", classes="title")
                        yield DataTable(id="loans", cursor_type="row", zebra_stripes=True)
                        yield Static("Waitlist", classes="title")
                        yield DataTable(id="wl", cursor_type="row", zebra_stripes=True)
                        yield Static("History (last 15 returned)", classes="title")
                        yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
                    with TabPane("Librarian", id="t-librarian"):
                        with Horizontal(classes="buttons"):
                            yield Button("Return selected book from selected member", id="lib-return",
                                         variant="warning")
                            yield Button("Refresh", id="lib-refresh")
                        yield Static("The first waitlisted member who may borrow gets the returned copy.",
                                     id="lib-result")
                        yield Static("Waitlist of the selected book", classes="title")
                        yield DataTable(id="bookwl", cursor_type="row", zebra_stripes=True)
                        yield Static("Latest decisions", classes="title")
                        yield DataTable(id="decisions", cursor_type="row", zebra_stripes=True)
                    with TabPane("Stores", id="t-stores"):
                        with Horizontal(classes="buttons"):
                            yield Button("Refresh", id="stores-refresh", variant="primary")
                            yield Button("Flush Redis cache", id="flush")
                            yield Button("Reset data", id="reset", variant="error")
                        yield Static("", id="store-stats")
                        yield Static("Redis cache (decisions)", classes="title")
                        yield DataTable(id="cache", cursor_type="row", zebra_stripes=True)
                    with TabPane("Scenarios", id="t-scenarios"):
                        with Horizontal(classes="buttons"):
                            yield Button("Run all", id="run-all", variant="primary")
                            yield Button("Run selected", id="run-one")
                            yield Static("", id="case-summary")
                        yield DataTable(id="cases", cursor_type="row", zebra_stripes=True)
                    with TabPane("Demos", id="t-demos"):
                        yield Static("These reset PostgreSQL to the generated data "
                                     "(loans made here are lost).")
                        with Horizontal(classes="buttons"):
                            yield Button("Join (Qdrant+PG)", id="demo-join")
                            yield Button("Concurrency", id="demo-concurrency")
                            yield Button("Stale cache", id="demo-stale")
                            yield Button("Update across", id="demo-update")
                            yield Button("Vector eval", id="demo-vectors")
                        yield RichLog(id="demo-log", wrap=True, markup=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        for tid, cols in (("members", ("id", "name", "tier", "")), ("books", ("id", "title", "free")),
                          ("alts", ("id", "title", "genre", "free", "authors", "via")),
                          ("recs", ("id", "title", "genre", "free", "authors", "via")),
                          ("similar", ("id", "title", "genre", "free", "authors", "via")),
                          ("loans", ("id", "title", "loaned", "due")), ("wl", ("id", "title", "position")),
                          ("history", ("id", "title", "loaned", "returned")),
                          ("bookwl", ("member", "name", "since")),
                          ("decisions", ("#", "time", "member", "book", "title", "outcome", "reason", "rules")),
                          ("cache", ("key", "outcome", "reason", "ttl (s)", "age (s)")),
                          ("cases", ("#", "member", "book", "expected", "why", "result"))):
            self.query_one(f"#{tid}", DataTable).add_columns(*cols)
            if tid == "cases":
                t = self.query_one("#cases", DataTable)
                t.clear(columns=True)
                for c in cols:
                    t.add_column(c, key=c)
        titles = {"members": "Members", "books": "Books", "alts": "Alternatives", "recs": "Recommendations",
                  "similar": "Similar", "loans": "Loans", "wl": "Waitlist", "history": "History",
                  "bookwl": "Waitlist", "decisions": "Decisions", "cache": "Cache", "cases": "Loan test cases"}
        for k, v in titles.items():
            self.query_one(f"#{k}").border_title = v
        self.query_one("#trace").border_title = "rules"
        self.query_one("#selected").border_title = "selected"
        if "tokyo-night" in self.available_themes:
            self.theme = "tokyo-night"
        self.update_selected()
        self.connect()
        self.set_interval(15, self.refresh_health)

    # --- background helper -----------------------------------------------------
    def bg(self, fn, done=None, wait=True):
        """Run fn in a worker thread holding the database lock; done(result) runs in the UI thread."""
        if self.advisor is None and not getattr(fn, "is_connect", False):
            self.notify("still connecting to the databases...", severity="warning")
            return
        if not wait and self.db_lock.locked():
            return

        def job():
            self.call_from_thread(self.busy_delta, 1)
            try:
                with self.db_lock:
                    res = fn()
            except LookupError as ex:
                self.call_from_thread(self.notify, str(ex), severity="error")
                return
            except Exception as ex:
                self.call_from_thread(self.notify, f"{type(ex).__name__}: {ex}", severity="error", timeout=10)
                return
            finally:
                self.call_from_thread(self.busy_delta, -1)
            if done:
                self.call_from_thread(done, res)
        self.run_worker(job, thread=True)

    def busy_delta(self, d):
        self.busy += d
        self.query_one("#status", Static).update("working..." if self.busy else "ready")

    def connect(self):
        def connect_job():
            from shelfsense.decision import Advisor
            self.advisor = Advisor()
            return postgres.genres(self.advisor.pg), postgres.search_members(self.advisor.pg), self.scenario_rows()
        connect_job.is_connect = True

        def ready(res):
            genres, members, books = res
            self.query_one("#genre", Select).set_options([(g.replace("_", " "), g) for g in genres])
            self.show_members(members)
            self.show_books(books)
            self.refresh_health()
        self.bg(connect_job, ready)

    # --- health / status ------------------------------------------------------------
    def refresh_health(self):
        if self.advisor:
            self.bg(self.advisor.health, self.show_health, wait=False)

    def show_health(self, h):
        t = Text()
        for name, (ok, ms) in h.items():
            t.append("● ", style="green" if ok else "red")
            t.append(f"{name} {ms} ms   " if ok else f"{name} DOWN   ", style="" if ok else "bold red")
        self.query_one("#health", Static).update(t)

    def update_selected(self):
        m = self.member or ("-", "")
        b = self.book or ("-", "")
        self.query_one("#selected", Static).update(
            f"[b]member[/b] {escape(m[0])} {escape(m[1])}\n[b]book[/b]   {escape(b[0])} {escape(b[1][:32])}")

    # --- sidebar -----------------------------------------------------------------
    def show_members(self, rows):
        fill(self.query_one("#members", DataTable),
             [[r["id"], r["name"][:18], r["tier"][:4], "suspended" if r["suspended"] else ""] for r in rows],
             [r["id"] for r in rows])
        self._members = {r["id"]: r for r in rows}

    def show_books(self, rows):
        fill(self.query_one("#books", DataTable),
             [[r["id"], r["title"][:30], free_cell(r["available"], r["total"])] for r in rows],
             [r["id"] for r in rows])
        self._books = {r["id"]: r for r in rows}

    def scenario_rows(self):
        ids = list(scenario.books().values())
        return [dict(zip(("id", "title", "genre", "available", "total", "book_type"), r)) for r in
                self.advisor.pg.execute("SELECT id, title, genre, available, total, book_type FROM books "
                                        "WHERE id = ANY(%s) ORDER BY title", (ids,))]

    def debounce(self, name, fn):
        if name in self._debounce:
            self._debounce[name].stop()
        self._debounce[name] = self.set_timer(0.3, fn)

    @on(Input.Changed, "#member-filter")
    def member_filter(self, e):
        text = e.value.strip()
        if self.advisor:
            self.debounce("m", lambda: self.bg(lambda: postgres.search_members(self.advisor.pg, text),
                                               self.show_members))

    @on(Input.Changed, "#title")
    def title_filter(self, e):
        text = e.value.strip()
        if not self.advisor:
            return
        if len(text) < 2:
            self.debounce("b", lambda: self.bg(self.scenario_rows, self.show_books))
        else:
            self.debounce("b", lambda: self.bg(lambda: postgres.search_books(self.advisor.pg, text, 30),
                                               self.show_books))

    # --- selection ------------------------------------------------------------------
    def select_member(self, member_id, name=""):
        self.member = (member_id, name)
        self.update_selected()
        self.load_member()

    def select_book(self, book_id, title=""):
        self.book = (book_id, title)
        self.update_selected()
        self.load_book()

    def load_member(self):
        if not (self.member and self.advisor):
            return
        mid = self.member[0]
        self.bg(lambda: self.advisor.member_profile(mid), self.show_profile)

    def load_book(self):
        if not (self.book and self.advisor):
            return
        bid = self.book[0]

        def job():
            return self.advisor.book_details(bid), postgres.book_waitlist(self.advisor.pg, bid)
        self.bg(job, self.show_book)

    @on(DataTable.RowSelected)
    def row_selected(self, e):
        tid, key = e.data_table.id, e.row_key.value
        if tid == "members":
            self.select_member(key, self._members.get(key, {}).get("name", ""))
        elif tid in ("books", "alts", "recs", "similar", "loans", "wl", "history"):
            row = e.data_table.get_row(e.row_key)
            self.select_book(key or str(row[0]), str(row[1]))
        elif tid == "cases":
            self.case_selected(key)

    # --- tabs & app actions -----------------------------------------------------------
    def action_tab(self, name):
        self.query_one("#tabs", TabbedContent).active = f"t-{name}"

    @on(TabbedContent.TabActivated)
    def tab_changed(self, e):
        pane = e.pane.id
        if not self.advisor:
            return
        if pane == "t-stores":
            self.load_stores()
        elif pane == "t-librarian":
            self.load_decisions()
        elif pane == "t-scenarios" and not self._cases_loaded:
            self.load_cases()
        elif pane == "t-book":
            self.load_book()
        elif pane == "t-member":
            self.load_member()

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_theme(self):
        names = [t for t in THEMES if t in self.available_themes]
        self.theme = names[(names.index(self.theme) + 1) % len(names)] if self.theme in names else names[0]
        self.notify(f"theme: {self.theme}")

    def action_clear(self):
        self.query_one("#verdict", Static).update("")
        self.query_one("#verdict").set_classes("")
        self.query_one("#trace", Static).update("")
        for tid in ("#alts", "#recs"):
            self.query_one(tid, DataTable).clear()

    def action_refresh_all(self):
        self.refresh_health()
        self.reload_views()

    def reload_views(self):
        self.load_member()
        self.load_book()
        self.load_decisions()
        if not self.advisor:
            return
        text = self.query_one("#title", Input).value.strip()
        if len(text) >= 2:
            self.bg(lambda: postgres.search_books(self.advisor.pg, text, 30), self.show_books)
        else:
            self.bg(self.scenario_rows, self.show_books)

    def action_export(self):
        if not self.last:
            return self.notify("nothing to export yet; run Advise or Borrow first", severity="warning")
        out = Path("exports")
        out.mkdir(exist_ok=True)
        m, b = self.last["member"], self.last["book"]
        path = out / f"{self.last['action']}_{m}_{b}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(self.last, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
        self.notify(f"saved {path}")

    # --- buttons ---------------------------------------------------------------------
    def need_both(self):
        if not (self.member and self.book):
            self.notify("select a member and a book first", severity="warning")
            return None
        return self.member[0], self.book[0]

    @on(Button.Pressed)
    def pressed(self, e):
        bid = e.button.id
        if bid in ("advise", "borrow"):
            mb = self.need_both()
            if mb:
                self.decide(bid, *mb)
        elif bid == "export":
            self.action_export()
        elif bid == "search":
            self.recommend()
        elif bid == "find-similar":
            self.find_similar()
        elif bid == "to-advise":
            self.action_tab("advise")
        elif bid in ("return-loan", "leave-wl"):
            self.member_action(bid)
        elif bid == "refresh-member":
            self.load_member()
        elif bid == "lib-return":
            mb = self.need_both()
            if mb:
                self.do_return(*mb)
        elif bid == "lib-refresh":
            self.load_decisions()
            self.load_book()
        elif bid == "stores-refresh":
            self.load_stores()
        elif bid == "flush":
            def flushed(_):
                self.notify("Redis cache flushed")
                self.load_stores()
            self.bg(lambda: self.advisor.flush_cache(), flushed)
        elif bid == "reset":
            self.push_screen(Confirm("Reset PostgreSQL and MongoDB to the generated data and flush Redis?\n"
                                     "All loans, waitlist entries and decisions made since are lost."),
                             lambda ok: ok and self.reset())
        elif bid == "run-all":
            self.run_cases(None)
        elif bid == "run-one":
            t = self.query_one("#cases", DataTable)
            if t.row_count:
                self.run_cases(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        elif bid and bid.startswith("demo-"):
            self.push_screen(Confirm(f"Run the '{bid[5:]}' demo? PostgreSQL is reset to the generated data "
                                     "first and afterwards, so loans made in this session are lost."),
                             lambda ok: ok and self.run_demo(bid[5:]))

    def on_input_submitted(self, e):
        if e.input.id == "query":
            self.recommend()

    # --- Advise ------------------------------------------------------------------------
    def decide(self, action, member, book):
        def job():
            trace = self.advisor.explain(member, book)
            if action == "advise":
                out = self.advisor.advise(member, book)
            else:
                out = self.advisor.borrow(member, book)
                out["from_cache"] = False
                if out["outcome"] != "allow":
                    out["alternatives"] = self.advisor.suggestions(book)
            return out, trace

        def done(res):
            out, trace = res
            self.last = {"action": action, "member": member, "book": book, **out, "rules": trace}
            self.show_advice(action, out, trace)
            if action == "borrow":
                self.reload_views()
        self.bg(job, done)

    def show_advice(self, action, out, trace):
        o = out["outcome"]
        who = f"{self.member[0]} {self.member[1]}".strip()
        what = self.book[1] or self.book[0]
        extra = []
        if out.get("from_cache"):
            extra.append("answer from the Redis cache")
        if action == "borrow":
            extra.append(f"recorded as decision #{out['decision_id']}")
        v = self.query_one("#verdict", Static)
        v.set_classes(o)
        v.update(f"[b]{escape(ICONS[o] + ' ' + o.upper() + ': ' + out['reason'])}[/b]\n"
                 f"[dim]{escape(who)} → {escape(what[:50])}"
                 + (f"   ({escape('; '.join(extra))})" if extra else "") + "[/dim]")
        lines, failed = [], False
        for r in trace:
            if failed:
                lines.append(f"[dim]– {escape(r['rule'])}[/dim]")
            elif r["ok"]:
                lines.append(f"[green]✔[/green] {escape(r['rule'])} [dim]({escape(r['detail'])})[/dim]")
            else:
                failed = True
                lines.append(f"[red]✘ {escape(r['rule'])}[/red] [dim]({escape(r['detail'])})[/dim]")
        if action == "borrow":
            lines.append("[dim]rules as they were before this borrow[/dim]")
        self.query_one("#trace", Static).update("\n".join(lines))
        rows, keys = book_rows(out.get("alternatives", []))
        fill(self.query_one("#alts", DataTable), rows, keys)

    # --- Recommend / Book -----------------------------------------------------------------
    def recommend(self):
        text = self.query_one("#query", Input).value.strip()
        if not text:
            return self.notify("describe what you want to read", severity="warning")
        g = self.query_one("#genre", Select).value
        genre = g if isinstance(g, str) else None

        def done(rows):
            r, k = book_rows(rows)
            fill(self.query_one("#recs", DataTable), r, k)
            self.notify(f"{len(rows)} recommendations (Enter on a row selects the book)")
        self.bg(lambda: self.advisor.recommend(text, genre=genre, limit=10), done)

    def find_similar(self):
        if not self.book:
            return self.notify("select a book first", severity="warning")
        bid = self.book[0]

        def done(rows):
            r, k = book_rows(rows)
            fill(self.query_one("#similar", DataTable), r, k)
        self.bg(lambda: self.advisor.suggestions(bid, 10), done)

    def show_book(self, res):
        d, waiting = res
        pg, doc, neo = d["postgres"], d["mongo"], d["neo4j"]
        extra = {k: v for k, v in doc.items() if k not in
                 ("title", "authors", "year", "genre", "description", "subjects", "book_type")}
        desc = doc.get("description", "")
        parts = [
            Panel(kv([("Title", pg["title"]), ("Genre", pg["genre"]), ("Year", pg["year"]), ("Type", pg["book_type"]),
                      ("Copies", f"{pg['available']} free of {pg['total']}  ({pg['on_loan']} on loan)"),
                      ("Waitlist", f"{pg['waiting']} waiting"), ("Loans ever", pg["times_borrowed"])]),
                  title="PostgreSQL · availability", title_align="left", border_style="blue"),
            Panel(Group(kv([("Authors", ", ".join(doc.get("authors", []))),
                            ("Subjects", ", ".join(doc.get("subjects", [])[:6])),
                            *[(k, v) for k, v in extra.items()]]),
                        Text("\n" + desc[:420] + ("…" if len(desc) > 420 else ""), style="dim")),
                  title=f"MongoDB · record ({doc.get('book_type', '?')} fields)", title_align="left",
                  border_style="green"),
            Panel(kv([("Readers (BORROWED edges)", neo["readers"]), ("Node refreshed", neo["refreshed_at"])]),
                  title="Neo4j · graph", title_align="left", border_style="magenta"),
            Panel(kv([("Cached decisions for this book", d["redis_cached_decisions"])]),
                  title="Redis · cache", title_align="left", border_style="red"),
        ]
        self.query_one("#book-info", Static).update(Group(*parts))
        self.book = (pg["id"], pg["title"])
        self.update_selected()
        fill(self.query_one("#bookwl", DataTable), [[w[0], w[1], str(w[2])[:16]] for w in waiting])

    # --- Member -------------------------------------------------------------------------------
    def show_profile(self, p):
        m = p["member"]
        self.member = (m["id"], m["name"])
        self.update_selected()
        state = "[red]suspended[/red]" if m["suspended"] else "[green]good standing[/green]"
        self.query_one("#member-info", Static).update(
            f"[b]{escape(m['id'])}[/b] {escape(m['name'])}   tier [b]{m['tier']}[/b]   "
            f"loans [b]{len(p['loans'])}/{m['max_loans']}[/b]   {state}")
        fill(self.query_one("#loans", DataTable), [[r[0], r[1][:45], r[2], r[3]] for r in p["loans"]],
             [r[0] for r in p["loans"]])
        fill(self.query_one("#wl", DataTable), [[r[0], r[1][:45], f"#{r[2]}"] for r in p["waitlist"]],
             [r[0] for r in p["waitlist"]])
        fill(self.query_one("#history", DataTable), [[r[0], r[1][:45], r[2], r[3]] for r in p["history"]])

    def member_action(self, which):
        if not self.member:
            return self.notify("select a member first", severity="warning")
        t = self.query_one("#loans" if which == "return-loan" else "#wl", DataTable)
        if not t.row_count:
            return self.notify("nothing to select", severity="warning")
        book = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        if which == "return-loan":
            self.do_return(self.member[0], book)
        else:
            m = self.member[0]

            def done(_):
                self.notify("removed from the waitlist")
                self.reload_views()
            self.bg(lambda: self.advisor.leave_waitlist(m, book), done)

    def do_return(self, member, book):
        def done(out):
            msg = (f"returned; copy given to {out['promoted']} from the waitlist (decision #{out['decision_id']})"
                   if out["promoted"] else "returned; nobody on the waitlist could take it")
            self.query_one("#lib-result", Static).update(f"[b green]✔[/b green] {escape(msg)}")
            self.notify(msg)
            self.reload_views()
        self.bg(lambda: self.advisor.return_book(member, book), done)

    # --- Librarian / Stores -----------------------------------------------------------------------
    def load_decisions(self):
        if not self.advisor:
            return

        def done(rows):
            fill(self.query_one("#decisions", DataTable),
                 [[r[0], str(r[1])[5:16], r[2], r[3], r[4][:30], outcome_cell(r[5]), r[6], f"v{r[7]}"] for r in rows])
        self.bg(lambda: postgres.recent_decisions(self.advisor.pg), done, wait=False)

    def load_stores(self):
        def job():
            return self.advisor.stats(), self.advisor.cache_entries()

        def done(res):
            stats, cache = res
            row = Table.grid()
            panels = []
            for store, colour in (("PostgreSQL", "blue"), ("MongoDB", "green"), ("Redis", "red"),
                                  ("Neo4j", "magenta"), ("Qdrant", "yellow")):
                panels.append(Panel(kv([(k, f"{v:,}") for k, v in stats[store].items()]), title=store,
                                    title_align="left", border_style=colour, width=24))
                row.add_column()
            row.add_row(*panels)
            self.query_one("#store-stats", Static).update(row)
            fill(self.query_one("#cache", DataTable),
                 [[e["key"], outcome_cell(e["outcome"]), e["reason"], e["ttl"], e["age"]] for e in cache])
        self.bg(job, done)
        self.refresh_health()

    def reset(self):
        def done(_):
            self.notify("data reset to the generated state")
            self.last = None
            self.reload_views()
            self.load_stores()
        self.bg(lambda: self.advisor.reset_data(), done)

    # --- Scenarios ------------------------------------------------------------------------------------
    def load_cases(self):
        cases = json.loads((DATA_DIR / "labels" / "loan_cases.json").read_text(encoding="utf-8"))

        def job():
            ids = list({c["book"] for c in cases})
            return dict(self.advisor.pg.execute("SELECT id, title FROM books WHERE id = ANY(%s)", (ids,)).fetchall())

        def done(titles):
            self._cases = {str(c["id"]): {**c, "title": titles.get(c["book"], c["book"])} for c in cases}
            fill(self.query_one("#cases", DataTable),
                 [[c["id"], c["member"], c["title"][:28], outcome_cell(c["expected"]), c["why"], ""]
                  for c in self._cases.values()], list(self._cases))
            self._cases_loaded = True
        self.bg(job, done)

    def case_selected(self, key):
        c = self._cases[key]
        self.select_member(c["member"])
        self.select_book(c["book"], c["title"])
        self.notify(f"case {key}: member {c['member']} and book selected")

    def run_cases(self, only):
        if not self._cases_loaded:
            return
        keys = [only] if only else list(self._cases)

        def job():
            return {k: postgres.check(self.advisor.pg, self._cases[k]["member"], self._cases[k]["book"])[0]
                    for k in keys}

        def done(res):
            t = self.query_one("#cases", DataTable)
            for k, got in res.items():
                ok = got == self._cases[k]["expected"]
                t.update_cell(k, "result", Text("✔ pass" if ok else f"✘ got {got}", style="green" if ok else "bold red"))
            if not only:
                n = sum(got == self._cases[k]["expected"] for k, got in res.items())
                self.query_one("#case-summary", Static).update(
                    f"[b {'green' if n == len(res) else 'red'}]{n}/{len(res)} correct[/]")
        self.bg(job, done)

    # --- Demos ----------------------------------------------------------------------------------------------
    def run_demo(self, name):
        import importlib
        mod, fn = DEMOS[name]
        log = self.query_one("#demo-log", RichLog)
        log.clear()
        log.write(Text(f"== {name} ==", style="bold"))

        def job():
            writer = _LogWriter(self, log)
            with contextlib.redirect_stdout(writer):
                getattr(importlib.import_module(mod), fn)()
            writer.write("\n")
            self.advisor.flush_cache()

        def done(_):
            log.write(Text("== finished ==", style="bold green"))
            self.reload_views()
        self.bg(job, done)

    def on_unmount(self):
        if self.advisor:
            self.advisor.close()


def run():
    # load the model before Textual takes over stdout/stderr; torch spawns a helper process that needs them
    from shelfsense import embeddings
    print("Loading embedding model...")
    embeddings.embed(["warm up"])
    ShelfSenseApp().run()


if __name__ == "__main__":
    run()
