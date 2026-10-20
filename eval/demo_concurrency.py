"""Demo: two members try to take the LAST copy at the same time.

Postgres locks the book row (SELECT ... FOR UPDATE), so exactly one wins.
Run: python -m eval.demo_concurrency
"""
import threading

from shelfsense import scenario
from shelfsense.stores import postgres

BOOK = scenario.books()["last"]  # one copy left in the scenario data


def run(rounds=20, verbose=True):
    with postgres.connect() as c:
        postgres.load(c)
    good, samples = 0, []
    for r in range(rounds):
        with postgres.connect() as c:
            c.execute("UPDATE books SET available = 1 WHERE id = %s", (BOOK,))
            c.execute("DELETE FROM loans WHERE book_id = %s AND returned_at IS NULL AND member_id IN ('S_STD','S_PREM')", (BOOK,))
            c.execute("DELETE FROM waitlist WHERE book_id = %s AND member_id IN ('S_STD','S_PREM')", (BOOK,))
            c.commit()
        results, barrier = {}, threading.Barrier(2)

        def worker(member):
            conn = postgres.connect()
            barrier.wait()
            results[member] = postgres.borrow(conn, member, BOOK)[:2]
            conn.close()

        ts = [threading.Thread(target=worker, args=(m,)) for m in ("S_STD", "S_PREM")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        with postgres.connect() as c:
            avail = c.execute("SELECT available FROM books WHERE id=%s", (BOOK,)).fetchone()[0]
        ok = sorted(o for o, _ in results.values()) == ["allow", "waitlist"] and avail == 0
        good += ok
        if r < 3:
            samples.append({"round": r + 1, "results": results, "available_after": avail})
            if verbose:
                print(f"round {r + 1}: {results}  available_after={avail}")
    out = {"rounds": rounds, "rounds_correct": good, "samples": samples}
    with postgres.connect() as c:
        postgres.load(c)  # reset the data the demo changed
    if verbose:
        print(f"{good}/{rounds} rounds: exactly one allow + one waitlist, available=0 (never negative)")
    return out


if __name__ == "__main__":
    run()
