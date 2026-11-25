"""Typer command line: `shelfsense --help` (no arguments or `tui` opens the full-screen UI).

Groups: db (up/down/load/sync/status), data (build/fetch/generate/labels), eval (all/vectors/...), plus
advise/borrow/recommend/similar/search/tui/test.
"""
import json
import subprocess
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="ShelfSense - library loan advisor", add_completion=False, invoke_without_command=True)
console = Console()
_state = {"json": False}


def _advisor():
    from shelfsense.decision import Advisor
    return Advisor()


def _books_table(rows, title):
    t = Table(title=title)
    for c in ("id", "title", "genre", "free", "authors", "via"):
        t.add_column(c)
    for r in rows:
        t.add_row(r["book_id"], r["title"][:55], r["genre"], str(r["available"]),
                  ", ".join(r["authors"][:2]), r.get("source", ""))
    console.print(t)


def _emit(data):
    console.print_json(json.dumps(data, default=str))


def _fail(msg):
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


@app.callback()
def main(ctx: typer.Context, as_json: bool = typer.Option(False, "--json", help="print raw JSON")):
    _state["json"] = as_json
    if ctx.invoked_subcommand is None:
        from shelfsense.tui import run
        run()


@app.command()
def tui():
    """Open the full-screen interface."""
    from shelfsense.tui import run
    run()


@app.command()
def advise(member: str, book: str):
    """Decision (allow / waitlist / reject) and alternatives."""
    adv = _advisor()
    try:
        out = adv.advise(member, book)
    except LookupError as e:
        _fail(e)
    finally:
        adv.close()
    if _state["json"]:
        return _emit(out)
    colour = {"allow": "green", "waitlist": "yellow", "reject": "red", "needs_info": "cyan"}[out["outcome"]]
    console.print(f"[bold {colour}]{out['outcome'].upper()}[/]: {out['reason']}"
                  + (" [dim](cached)[/dim]" if out["from_cache"] else ""))
    if out.get("alternatives"):
        _books_table(out["alternatives"], "Alternatives with a free copy")


@app.command()
def borrow(member: str, book: str):
    """Record the decision (and the loan or waitlist entry) in PostgreSQL."""
    adv = _advisor()
    try:
        out = adv.borrow(member, book)
    except LookupError as e:
        _fail(e)
    finally:
        adv.close()
    if _state["json"]:
        return _emit(out)
    console.print(f"{out['outcome'].upper()}: {out['reason']} (decision #{out['decision_id']})")


@app.command()
def explain(first: str = typer.Argument(..., help="a decision number, or a member id (then give the book too)"),
            book: Optional[str] = typer.Argument(None, help="book id: show the rules for this member and book now")):
    """Explain a stored decision from its own rows (rule version, evidence), or a live member/book check."""
    adv = _advisor()
    try:
        if book is not None:
            trace = adv.explain(first, book)
            if _state["json"]:
                return _emit(trace)
            _rule_table(trace, f"Rules now (v{adv.active_rules()['version']}) for {first} / {book}")
            return
        if not first.isdigit():
            _fail("give a decision number, or a member and a book")
            raise typer.Exit(1)
        out = adv.explain_decision(int(first))
    except LookupError as e:
        _fail(e)
        raise typer.Exit(1)
    finally:
        adv.close()
    if _state["json"]:
        return _emit(out)
    d, r = out["decision"], out["rules"]
    colour = {"allow": "green", "waitlist": "yellow", "reject": "red", "needs_info": "cyan"}[d["outcome"]]
    console.print(f"Decision #{d['id']} at {str(d['decided_at'])[:19]}: {d['member']} -> {d['book']}\n"
                  f"[bold {colour}]{d['outcome'].upper()}[/]: {d['reason']}   (deciding rule {d['rule_id']}, "
                  f"rules version {d['rule_version']}, active version now {out['active_version']})")
    console.print(f"Rules v{r['version']} valid {str(r['valid_from'])[:19]} - "
                  f"{str(r['valid_to'])[:19] if r['valid_to'] else 'now'}: {json.dumps(r['params'])}")
    console.print(f"Stored evidence: {json.dumps(d['inputs'])}")
    _rule_table(out["trace"], f"Stored evidence re-evaluated with rules v{r['version']}")
    console.print("Replay reproduces the stored outcome." if out["replay_ok"]
                  else f"[red]Replay gives {out['replay_outcome']}, stored {d['outcome']}[/red]")


def _rule_table(items, title):
    t = Table(title=title)
    for c in ("rule", "check", "result", "detail"):
        t.add_column(c)
    failed = False
    for i in items:
        state = "[dim]not reached[/dim]" if failed else "[green]pass[/green]" if i["ok"] else f"[red]FAIL -> {i['outcome']}[/red]"
        failed = failed or not i["ok"]
        t.add_row(i["id"], i["rule"], state, "" if state.startswith("[dim]") else i["detail"])
    console.print(t)


@app.command(name="return")
def return_(member: str, book: str):
    """Return a loan; the first eligible waitlisted member gets the copy. Updates Neo4j too."""
    adv = _advisor()
    try:
        out = adv.return_book(member, book)
    except LookupError as e:
        _fail(e)
    finally:
        adv.close()
    if _state["json"]:
        return _emit(out)
    console.print(f"returned; copy given to {out['promoted']}" if out["promoted"]
                  else "returned; nobody on the waitlist could take it")


rules_app = typer.Typer(help="Versioned loan rules: list, show, publish a new version.", no_args_is_help=True)
app.add_typer(rules_app, name="rules")


@rules_app.command("list")
def rules_list():
    """Every rule version with its validity period."""
    adv = _advisor()
    try:
        rows = adv.rules_history()
    finally:
        adv.close()
    if _state["json"]:
        return _emit(rows)
    t = Table(title="Rule versions")
    for c in ("version", "valid from", "valid to", "params", "note"):
        t.add_column(c)
    for r in rows:
        t.add_row(str(r["version"]), str(r["valid_from"])[:19], str(r["valid_to"])[:19] if r["valid_to"] else "active",
                  json.dumps(r["params"]), r["note"] or "")
    console.print(t)


@rules_app.command("show")
def rules_show(version: Optional[int] = typer.Argument(None, help="default: the active version")):
    """The parameters of one rule version."""
    adv = _advisor()
    try:
        r = adv.rules_history()
        r = next((x for x in r if x["version"] == version), None) if version else adv.active_rules()
    finally:
        adv.close()
    if r is None:
        _fail("unknown rules version")
        raise typer.Exit(1)
    _emit(r)


@rules_app.command("publish")
def rules_publish(loan_days: Optional[int] = typer.Option(None, help="loan period in days"),
                  standard_max: Optional[int] = typer.Option(None, help="loan limit of standard members"),
                  premium_max: Optional[int] = typer.Option(None, help="loan limit of premium members"),
                  note: Optional[str] = typer.Option(None, help="why the rules change")):
    """Publish a new rule version (the options you leave out keep their value). Cached decisions are dropped."""
    changes = {}
    if loan_days is not None:
        changes["loan_days"] = loan_days
    limits = {t: v for t, v in (("standard", standard_max), ("premium", premium_max)) if v is not None}
    if limits:
        changes["max_loans"] = limits
    if not changes:
        _fail("give at least one of --loan-days, --standard-max, --premium-max")
        raise typer.Exit(1)
    adv = _advisor()
    try:
        v = adv.publish_rules(changes, note)
    except ValueError as e:
        _fail(e)
        raise typer.Exit(1)
    finally:
        adv.close()
    console.print(f"published rules version {v}")


book_app = typer.Typer(help="Add, edit or delete books; Neo4j and Qdrant follow immediately.", no_args_is_help=True)
app.add_typer(book_app, name="book")


@book_app.command("add")
def book_add(id: str, title: str, author: str, genre: str, description: str,
             year: int = 2024, kind: str = typer.Option("paperback", help="paperback | ebook | journal"),
             copies: int = 1):
    """Add one book to PostgreSQL + MongoDB, then Neo4j and Qdrant (only this book is embedded)."""
    adv = _advisor()
    try:
        adv.add_book({"id": id, "title": title, "authors": [author], "genre": genre, "description": description,
                      "year": year, "book_type": kind}, copies)
    except Exception as e:  # noqa: BLE001
        _fail(e)
    finally:
        adv.close()
    console.print(f"added {id}")


@book_app.command("edit")
def book_edit(id: str, title: Optional[str] = None, genre: Optional[str] = None,
              description: Optional[str] = None, year: Optional[int] = None):
    """Change a book; it is re-embedded in Qdrant only if title or description changed."""
    fields = {k: v for k, v in dict(title=title, genre=genre, description=description, year=year).items()
              if v is not None}
    adv = _advisor()
    try:
        adv.update_book(id, **fields)
    except LookupError as e:
        _fail(e)
    finally:
        adv.close()
    console.print(f"updated {id}")


@book_app.command("delete")
def book_delete(id: str):
    """Delete a book from all stores (refused while it is on loan)."""
    adv = _advisor()
    try:
        adv.delete_book(id)
    except (LookupError, ValueError) as e:
        _fail(e)
    finally:
        adv.close()
    console.print(f"deleted {id}")


@app.command()
def recommend(text: str, genre: Optional[str] = None,
              n: int = typer.Option(5, "--n", "-n", help="number of results")):
    """Free-text search in Qdrant, limited to books with a free copy."""
    adv = _advisor()
    try:
        out = adv.recommend(text, genre=genre, limit=n)
    finally:
        adv.close()
    _emit(out) if _state["json"] else _books_table(out, "Recommendations")


@app.command()
def similar(book: str, n: int = typer.Option(5, "--n", "-n", help="number of results")):
    """Alternatives to a book: same author, also borrowed, similar text."""
    adv = _advisor()
    try:
        out = adv.suggestions(book, n)
    finally:
        adv.close()
    _emit(out) if _state["json"] else _books_table(out, "Similar books")


@app.command()
def search(title: str):
    """Find book ids by title substring."""
    from shelfsense.tui import find_books
    adv = _advisor()
    try:
        rows = find_books(adv, title)
    finally:
        adv.close()
    if _state["json"]:
        return _emit(rows)
    t = Table(title=f"Books matching '{title}'")
    for c in ("id", "title", "genre", "free"):
        t.add_column(c)
    for r in rows:
        t.add_row(r["id"], r["title"][:60], r["genre"], str(r["available"]))
    console.print(t)


# ---- db: the five stores ----------------------------------------------------------------------
db_app = typer.Typer(help="Start the databases and fill them.", no_args_is_help=True)
app.add_typer(db_app, name="db")
ROOT = Path(__file__).resolve().parents[1]


def _compose(*args):
    cmd = ["docker", "compose", "-p", "shelfsense", "-f", str(ROOT / "docker-compose.yml"), *args]
    try:
        code = subprocess.call(cmd)
    except FileNotFoundError:
        _fail("docker not found")
    if code:
        raise typer.Exit(code)


@db_app.command()
def up():
    """Start PostgreSQL, MongoDB, Redis, Neo4j and Qdrant in Docker."""
    _compose("up", "-d")


@db_app.command()
def down():
    """Stop the containers (data volumes are kept)."""
    _compose("down")


@db_app.command()
def schema():
    """Create or upgrade the PostgreSQL tables (idempotent; keeps the data)."""
    from shelfsense.stores import postgres
    with postgres.connect() as c:
        postgres.apply_schema(c)
    console.print("schema applied")


@db_app.command()
def load():
    """Reset and fill PostgreSQL and MongoDB from data/generated (fast)."""
    from shelfsense import load_sources
    load_sources.main()


@db_app.command()
def sync():
    """Rebuild Neo4j, Qdrant and Redis from the sources (about 10 minutes for 20k books)."""
    from shelfsense.sync import sync_all
    console.print(sync_all())


@db_app.command()
def pgvector():
    """Fill the pgvector table from the vectors already in Qdrant (no re-embedding, about a minute)."""
    from shelfsense.sync import sync_pgvector_from_qdrant
    console.print(sync_pgvector_from_qdrant())


@db_app.command()
def status():
    """Row/document/vector counts of every store."""
    from eval.run_all import store_counts
    _emit(store_counts())


# ---- data: build the dataset files --------------------------------------------------------------
data_app = typer.Typer(help="Build the dataset files (raw books, synthetic data, labels).", no_args_is_help=True)
app.add_typer(data_app, name="data")
BOOKS_OPT = typer.Option(20000, "--books", "-b", help="about how many books (e.g. 500 for a small set)")


@data_app.command()
def build(books: int = BOOKS_OPT,
          skip_fetch: bool = typer.Option(False, "--skip-fetch", help="reuse data/raw/books.json")):
    """Run fetch, generate and labels in one go."""
    from data import build_data
    build_data.run(books, skip_fetch)


@data_app.command()
def fetch(books: int = BOOKS_OPT):
    """Stream the Open Library dumps into data/raw/books.json."""
    from data import fetch_books
    fetch_books.run(books)


@data_app.command()
def generate():
    """Synthetic members, loans, waitlist, scenario and loan labels from raw/books.json."""
    from data import generate as g
    g.main()


@data_app.command()
def labels():
    """Retrieval labels (interest queries) from raw/books.json."""
    from data import build_labels
    build_labels.main()


# ---- eval: measurements and demos ---------------------------------------------------------------
eval_app = typer.Typer(help="Measurements and demos (need filled databases).", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@eval_app.command("all")
def eval_all():
    """Everything -> report/results.txt and results.json."""
    from eval import run_all
    run_all.main()


@eval_app.command()
def vectors():
    """Qdrant recall/MRR, ANN recall and filtered search."""
    from eval import eval_vectors
    eval_vectors.main()


@eval_app.command()
def compare():
    """pgvector vs Qdrant: recall, MRR, ANN recall, filtered search, latency, index size."""
    from eval import eval_compare
    eval_compare.main()


@eval_app.command("rules")
def rules_demo():
    """Rule version demo: the same request before/after publishing v2, old decision still explained."""
    from eval import demo_rules
    demo_rules.run()


@eval_app.command()
def join():
    """Qdrant + PostgreSQL join demo."""
    from eval import demo_join
    demo_join.run()


@eval_app.command()
def concurrency():
    """Two members borrow the last copy at once."""
    from eval import demo_concurrency
    demo_concurrency.run()


@eval_app.command("stale-cache")
def stale_cache():
    """Redis invalidation failure demo."""
    from eval import demo_stale_cache
    demo_stale_cache.run()


@eval_app.command("update-across")
def update_across():
    """Update one book in all stores."""
    from eval import demo_update_across
    demo_update_across.run()


@app.command()
def test(args: List[str] = typer.Argument(None, help="extra pytest arguments")):
    """Run the test suite (uses separate test databases; the real data is untouched)."""
    import pytest
    raise typer.Exit(pytest.main(["-q", str(ROOT / "tests"), *(args or [])]))


if __name__ == "__main__":
    app()
