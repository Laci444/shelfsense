"""Generate synthetic members, copies, loans, waitlist from data/raw/books.json; everything scales with the
number of books (members ~ 0.145 x books, loan history ~ 1 x, active loans ~ 0.29 x, waitlist ~ 0.02 x).

Output (fixed seed, deterministic for a given books.json):
  data/generated/{members,books,loans,waitlist}.json
  data/generated/scenario.json     which books play which role in the loan test cases
  data/labels/loan_cases.json      the 15 labelled loan decisions, using the scenario books/members

Book type (paperback/ebook/journal), copy counts and loans are synthetic; titles, authors, years, genres and
descriptions come from Open Library. Scenario books are 8 books drawn from the dataset; their loans/copies are
set up by hand below, and the expected outcomes in LOAN_CASES follow from that set-up and the loan rules in the
README, not from running the program, so they are independent of any tuning.
Usage: python -m data.generate
"""
import json
import random
from datetime import datetime, timedelta, timezone

from shelfsense.config import DATA_DIR

OUT = DATA_DIR / "generated"
LABELS = DATA_DIR / "labels"
SEED = 42
MIN_BOOKS = 50
MEMBERS_PER_BOOK, HISTORY_PER_BOOK, ACTIVE_PER_BOOK, WAITLIST_PER_BOOK = 0.145, 1.0, 0.29, 0.02
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

ROLES = ["free1", "free2", "last", "none", "none2", "journal", "ebook", "held"]

# (id, member, book role, expected outcome, why)
LOAN_CASES = [
    (1, "S_STD", "free1", "allow", "copy available, standard member, no restrictions"),
    (2, "S_PREM", "free2", "allow", "copy available, premium member"),
    (3, "S_STD", "last", "allow", "exactly one copy left"),
    (4, "S_STD", "none", "waitlist", "all copies on loan"),
    (5, "S_PREM", "none", "waitlist", "all copies on loan, premium"),
    (6, "S_SUSP", "free1", "reject", "member suspended"),
    (7, "S_FULL", "free1", "reject", "member at max loans"),
    (8, "S_HAS", "held", "reject", "member already holds this book"),
    (9, "S_WAIT", "none2", "reject", "member already on waitlist"),
    (10, "S_STD", "journal", "reject", "journals are premium only"),
    (11, "S_PREM", "journal", "allow", "journal, premium member"),
    (12, "S_STD", "ebook", "allow", "ebook available, standard member"),
    (13, "S_SUSP", "none", "reject", "suspension takes precedence over waitlist"),
    (14, "S_FULL", "none", "reject", "loan limit takes precedence over waitlist"),
    (15, "S_PREM", "none2", "waitlist", "all copies on loan, not yet on waitlist"),
]

FIRST = ["Anna", "Bela", "Csilla", "David", "Eva", "Ferenc", "Greta", "Hugo", "Ivan", "Judit",
         "Karl", "Lena", "Marta", "Nora", "Oscar", "Petra", "Quinn", "Rita", "Samu", "Tessa"]
LAST = ["Nagy", "Kovacs", "Szabo", "Toth", "Horvath", "Varga", "Kiss", "Molnar", "Smith", "Meyer"]


def main():
    rnd = random.Random(SEED)
    raw = json.loads((DATA_DIR / "raw" / "books.json").read_text(encoding="utf-8"))
    n_books = len(raw)
    assert n_books >= MIN_BOOKS, f"need at least {MIN_BOOKS} books, got {n_books}"
    n_members = max(20, round(n_books * MEMBERS_PER_BOOK))
    n_history, n_active = round(n_books * HISTORY_PER_BOOK), round(n_books * ACTIVE_PER_BOOK)
    n_waitlist = round(n_books * WAITLIST_PER_BOOK)

    scenario = dict(zip(ROLES, rnd.sample(sorted(b["id"] for b in raw), len(ROLES))))
    B_FREE1, B_FREE2, B_LAST, B_NONE, B_NONE2, B_JOURNAL, B_EBOOK, B_HELD = (scenario[r] for r in ROLES)
    SCENARIO_BOOKS = set(scenario.values())

    # members
    members = []
    for i in range(1, n_members + 1):
        tier = "premium" if rnd.random() < 0.25 else "standard"
        members.append({"id": f"M{i:04d}", "name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
                        "tier": tier, "max_loans": 6 if tier == "premium" else 3,
                        "suspended": rnd.random() < 0.03})
    scen = {"S_STD": ("standard", 3, False), "S_PREM": ("premium", 6, False),
            "S_SUSP": ("standard", 3, True), "S_FULL": ("standard", 3, False),
            "S_HAS": ("standard", 3, False), "S_WAIT": ("standard", 3, False)}
    for mid, (tier, mx, susp) in scen.items():
        members.append({"id": mid, "name": f"Scenario {mid}", "tier": tier,
                        "max_loans": mx, "suspended": susp})
    mdict = {m["id"]: m for m in members}
    random_members = [m["id"] for m in members if not m["id"].startswith("S_")]

    # books with copy counts and type
    books = []
    for b in raw:
        t = rnd.choices(["paperback", "ebook", "journal"], [0.77, 0.15, 0.08])[0]
        books.append({**b, "book_type": t, "total": rnd.randint(1, 5)})
    bdict = {b["id"]: b for b in books}
    for bid, (t, total) in {B_FREE1: ("paperback", 3), B_FREE2: ("paperback", 3),
                            B_LAST: ("paperback", 2), B_NONE: ("paperback", 2),
                            B_NONE2: ("paperback", 2), B_JOURNAL: ("journal", 3),
                            B_EBOOK: ("ebook", 3), B_HELD: ("paperback", 2)}.items():
        bdict[bid]["book_type"], bdict[bid]["total"] = t, total

    loans, active = [], {"by_book": {}, "by_member": {}}
    waitlist = []
    held = set()  # active (member, book) pairs

    def add_loan(mid, bid, returned):
        if not returned:
            if active["by_book"].get(bid, 0) >= bdict[bid]["total"]:
                return False
            if active["by_member"].get(mid, 0) >= mdict[mid]["max_loans"]:
                return False
            if (mid, bid) in held:
                return False
        start = NOW - timedelta(days=rnd.randint(1, 60 if not returned else 400))
        due = start + timedelta(days=21)
        loans.append({"member_id": mid, "book_id": bid, "loaned_at": start.isoformat(),
                      "due_at": due.isoformat(),
                      "returned_at": (start + timedelta(days=rnd.randint(3, 25))).isoformat() if returned else None})
        if not returned:
            held.add((mid, bid))
            active["by_book"][bid] = active["by_book"].get(bid, 0) + 1
            active["by_member"][mid] = active["by_member"].get(mid, 0) + 1
        return True

    free_books = [b["id"] for b in books if b["id"] not in SCENARIO_BOOKS]
    # scenario state
    add_loan("M0001", B_LAST, False)
    add_loan("M0002", B_NONE, False); add_loan("M0003", B_NONE, False)
    add_loan("M0004", B_NONE2, False); add_loan("M0005", B_NONE2, False)
    add_loan("S_HAS", B_HELD, False)
    for bid in rnd.sample(free_books, 3):
        add_loan("S_FULL", bid, False)
    waitlist.append({"member_id": "S_WAIT", "book_id": B_NONE2, "created_at": NOW.isoformat()})
    # random history (returned) and active loans; popularity skew so some books run out of copies
    cum, tot = [], 0.0
    for i in range(len(free_books)):
        tot += 1 / (i + 1) ** 0.7
        cum.append(tot)
    pick = lambda: rnd.choices(free_books, cum_weights=cum)[0]
    for _ in range(n_history):
        add_loan(rnd.choice(random_members), rnd.choice(free_books), True)
    for _ in range(n_active):
        add_loan(rnd.choice(random_members), pick(), False)
    # random waitlist on fully loaned non-scenario books
    full = [b for b in free_books if active["by_book"].get(b, 0) >= bdict[b]["total"]]
    for bid in full[:n_waitlist]:
        mid = rnd.choice(random_members)
        if (mid, bid) not in held:
            waitlist.append({"member_id": mid, "book_id": bid,
                             "created_at": (NOW - timedelta(days=rnd.randint(0, 20))).isoformat()})
    seen, wl = set(), []
    for w in waitlist:
        k = (w["member_id"], w["book_id"])
        if k not in seen:
            seen.add(k); wl.append(w)

    for b in books:
        b["available"] = b["total"] - active["by_book"].get(b["id"], 0)

    OUT.mkdir(parents=True, exist_ok=True)
    LABELS.mkdir(parents=True, exist_ok=True)
    for name, data in [("members", members), ("books", books), ("loans", loans), ("waitlist", wl)]:
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(name, len(data))
    (OUT / "scenario.json").write_text(json.dumps(scenario, indent=1), encoding="utf-8")
    cases = [{"id": i, "member": m, "book": scenario[role], "expected": exp, "why": why}
             for i, m, role, exp, why in LOAN_CASES]
    (LABELS / "loan_cases.json").write_text(json.dumps(cases, indent=1), encoding="utf-8")
    print("scenario", scenario, "| loan cases", len(cases))


if __name__ == "__main__":
    main()
