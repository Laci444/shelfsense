import pytest

from shelfsense import decision, rules, scenario
from shelfsense.stores import postgres

S = scenario.books()


@pytest.fixture()
def adv():
    with postgres.connect() as c:
        postgres.load(c)   # also empties decisions, so rule versions above 1 can be removed
        c.execute("DELETE FROM rules WHERE version > 1")
        c.execute("UPDATE rules SET valid_to = NULL WHERE version = 1")
    a = decision.Advisor()
    a.redis.flushdb()
    yield a
    with postgres.connect() as c:
        postgres.load(c)
        c.execute("DELETE FROM rules WHERE version > 1")
        c.execute("UPDATE rules SET valid_to = NULL WHERE version = 1")
    a.close()


def test_schema_is_idempotent_and_seeds_rules(adv):
    with postgres.connect() as c:
        postgres.apply_schema(c)
        postgres.apply_schema(c)
    act = adv.active_rules()
    assert act["version"] == 1 and act["params"]["max_loans"] == {"standard": 3, "premium": 6}


def test_decision_stores_rule_version_inputs_and_rule_id(adv):
    out = adv.borrow("S_SUSP", S["free1"])
    row = adv.pg.execute("SELECT outcome, rule_version, rule_id, inputs, requested_member FROM decisions WHERE id=%s",
                         (out["decision_id"],)).fetchone()
    assert row[:3] == ("reject", 1, "r1_suspended") and row[4] == "S_SUSP"
    assert row[3]["member"]["id"] == "S_SUSP" and row[3]["member"]["version"] >= 1
    assert row[3]["book"]["id"] == S["free1"] and row[3]["book"]["available"] > 0


def test_unknown_member_or_book_needs_info_and_is_recorded(adv):
    out = adv.borrow("NOBODY", S["free1"])
    assert out["outcome"] == "needs_info" and out["reason"] == "unknown member"
    out2 = adv.borrow("S_STD", "NO_SUCH_BOOK")
    assert out2["outcome"] == "needs_info" and out2["reason"] == "unknown book"
    row = adv.pg.execute("SELECT member_id, book_id, requested_book FROM decisions WHERE id=%s",
                         (out2["decision_id"],)).fetchone()
    assert row == (None, None, "NO_SUCH_BOOK")
    assert adv.advise("NOBODY", S["free1"])["outcome"] == "needs_info"
    assert adv.pg.execute("SELECT count(*) FROM loans WHERE member_id='NOBODY'").fetchone()[0] == 0


def test_missing_tier_limit_needs_info():
    inputs = {"member": {"tier": "gold", "suspended": False}, "book": {"book_type": "paperback", "available": 1},
              "active_loans": 0, "holds_book": False, "on_waitlist": False}
    outcome, reason, rule_id = rules.decide(inputs, rules.DEFAULT_PARAMS)
    assert (outcome, rule_id) == ("needs_info", "r0_evidence") and "gold" in reason


def test_publish_changes_limit_and_clears_cache(adv):
    adv.decide_cached("S_STD", S["free1"])
    assert adv.redis.dbsize() == 1
    v = adv.publish_rules({"max_loans": {"standard": 1}}, "tighter limit")
    assert v == 2 and adv.redis.dbsize() == 0
    assert adv.active_rules()["params"]["max_loans"] == {"standard": 1, "premium": 6}
    assert adv.borrow("S_STD", S["free1"])["outcome"] == "allow"
    again = adv.borrow("S_STD", S["free2"])
    assert (again["outcome"], again["reason"]) == ("reject", "member at max loans")
    hist = adv.rules_history()
    assert [h["version"] for h in hist] == [1, 2] and hist[0]["valid_to"] is not None and hist[1]["valid_to"] is None


def test_publish_validates_and_rejects_no_change(adv):
    with pytest.raises(ValueError):
        adv.publish_rules({"loan_days": 0})
    with pytest.raises(ValueError):
        adv.publish_rules({"loan_days": 21})   # identical to the active version


def test_old_decision_explained_with_its_own_version_after_rules_change(adv):
    first = adv.borrow("S_FULL", S["free1"])             # S_FULL is at the standard limit of 3
    assert first["outcome"] == "reject"
    adv.publish_rules({"max_loans": {"standard": 10}}, "relaxed")
    assert adv.borrow("S_FULL", S["free1"])["outcome"] == "allow"
    old = adv.explain_decision(first["decision_id"])
    assert old["decision"]["rule_version"] == 1 and old["active_version"] == 2
    assert old["replay_ok"] and old["replay_outcome"] == "reject"
    assert next(i for i in old["trace"] if not i["ok"])["id"] == "r2_limit"
    new = adv.explain_decision(adv.pg.execute("SELECT max(id) FROM decisions").fetchone()[0])
    assert new["decision"]["rule_version"] == 2 and new["replay_ok"]


def test_loan_days_follow_the_rules(adv):
    adv.publish_rules({"loan_days": 7})
    adv.borrow("S_STD", S["free2"])
    days = adv.pg.execute("SELECT (due_at::date - loaned_at::date) FROM loans WHERE member_id='S_STD' AND "
                          "book_id=%s AND returned_at IS NULL", (S["free2"],)).fetchone()[0]
    assert days == 7


def test_promotion_decision_is_recorded_with_evidence(adv):
    book = S["last"]
    adv.borrow("S_STD", book)
    adv.borrow("S_PREM", book)                           # waitlisted
    out = adv.return_book("S_STD", book)
    e = adv.explain_decision(out["decision_id"])
    assert e["decision"]["reason"] == "promoted from waitlist" and e["replay_ok"]


def test_explain_unknown_decision(adv):
    with pytest.raises(LookupError):
        adv.explain_decision(999999)
