"""Loan rules as versioned data, and the pure function that applies them.

A rule set is a row of the `rules` table (`params` JSON). `publish` closes the active version and adds
the next one; decisions store the version they used, so an old decision can be replayed with its own rules.

params = {"loan_days": 21,
          "max_loans": {"<tier>": limit},
          "restricted_types": {"<book_type>": ["<tier allowed>", ...]}}

`evaluate(inputs, params)` does no I/O: `inputs` is the evidence read from PostgreSQL (see
postgres.gather) and the same dict is stored with the decision, so a stored decision can be re-evaluated.
"""
import copy
import json

DEFAULT_PARAMS = {"loan_days": 21,
                  "max_loans": {"standard": 3, "premium": 6},
                  "restricted_types": {"journal": ["premium"]}}


def validate(params):
    if not isinstance(params.get("loan_days"), int) or params["loan_days"] <= 0:
        raise ValueError("loan_days must be a positive integer")
    limits = params.get("max_loans")
    if not isinstance(limits, dict) or not limits or not all(isinstance(v, int) and v > 0 for v in limits.values()):
        raise ValueError("max_loans must map each tier to a positive integer")
    restricted = params.get("restricted_types")
    if not isinstance(restricted, dict) or not all(isinstance(v, list) for v in restricted.values()):
        raise ValueError("restricted_types must map a book type to the list of allowed tiers")
    return params


def active(conn):
    """The active rule set: {"version", "params", "valid_from"}."""
    row = conn.execute("SELECT version, params, valid_from FROM rules WHERE valid_to IS NULL").fetchone()
    if row is None:
        raise RuntimeError("no active rules; run `shelfsense db schema`")
    return {"version": row[0], "params": row[1], "valid_from": row[2]}


def get(conn, version):
    row = conn.execute("SELECT version, params, valid_from, valid_to, note FROM rules WHERE version = %s",
                       (version,)).fetchone()
    if row is None:
        raise LookupError(f"unknown rules version {version}")
    return dict(zip(("version", "params", "valid_from", "valid_to", "note"), row))


def history(conn):
    return [dict(zip(("version", "params", "valid_from", "valid_to", "note"), r)) for r in conn.execute(
        "SELECT version, params, valid_from, valid_to, note FROM rules ORDER BY version")]


def publish(conn, changes, note=None):
    """New version = the active params with `changes` applied. Returns the new version number."""
    with conn.transaction():
        cur = conn.execute("SELECT version, params FROM rules WHERE valid_to IS NULL FOR UPDATE").fetchone()
        params = copy.deepcopy(cur[1])
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(params.get(key), dict):
                params[key].update(value)
            else:
                params[key] = value
        validate(params)
        if params == cur[1]:
            raise ValueError("nothing changed")
        conn.execute("UPDATE rules SET valid_to = now() WHERE version = %s", (cur[0],))
        conn.execute("INSERT INTO rules (version, params, note) VALUES (%s, %s::jsonb, %s)",
                     (cur[0] + 1, json.dumps(params), note))
    return cur[0] + 1


def evaluate(inputs, params):
    """Apply the rules to the evidence. Returns the rule list in the order applied.

    Each item: {id, rule, ok, detail, outcome, reason}; the first failing item decides (outcome/reason).
    Missing evidence (unknown member or book, no limit for the member's tier) gives ONE failing
    item with outcome `needs_info`: the program does not guess.
    """
    member, book = inputs.get("member"), inputs.get("book")
    if member is None:
        return [_item("r0_evidence", "Member is known", False, "no such member", "needs_info", "unknown member")]
    if book is None:
        return [_item("r0_evidence", "Book is known", False, "no such book", "needs_info", "unknown book")]
    tier = member["tier"]
    limit = params["max_loans"].get(tier)
    if limit is None:
        return [_item("r0_evidence", "The rules define a loan limit for the tier", False,
                      f"no limit for tier {tier}", "needs_info", f"no loan limit defined for tier {tier}")]
    allowed = params["restricted_types"].get(book["book_type"])
    return [
        _item("r1_suspended", "Member is not suspended", not member["suspended"],
              "suspended" if member["suspended"] else "good standing", "reject", "member suspended"),
        _item("r2_limit", "Member is below the loan limit", inputs["active_loans"] < limit,
              f"{inputs['active_loans']} of {limit} loans", "reject", "member at max loans"),
        _item("r3_holds", "Member does not already hold the book", not inputs["holds_book"],
              "holds a copy" if inputs["holds_book"] else "no copy held", "reject", "member already holds this book"),
        _item("r4_waiting", "Member is not already on the waitlist", not inputs["on_waitlist"],
              "already waiting" if inputs["on_waitlist"] else "not waiting", "reject", "member already on waitlist"),
        _item("r5_type", "Book type is allowed for the tier", allowed is None or tier in allowed,
              f"{book['book_type']}, {tier}", "reject",
              "journals are premium only" if book["book_type"] == "journal" and allowed == ["premium"]
              else f"{book['book_type']} is not allowed for {tier} members"),
        _item("r6_available", "A copy is available", book["available"] > 0,
              f"{book['available']} free", "waitlist", "no copy available"),
    ]


def decide(inputs, params):
    """(outcome, reason, rule_id) from evaluate(): the first failing rule, else allow."""
    for r in evaluate(inputs, params):
        if not r["ok"]:
            return r["outcome"], r["reason"], r["id"]
    return "allow", "copy available", "r6_available"


def _item(rid, rule, ok, detail, outcome, reason):
    return {"id": rid, "rule": rule, "ok": ok, "detail": detail, "outcome": outcome, "reason": reason}
