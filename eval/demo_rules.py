"""Rule version demo: the same request is decided under rules v1 and v2; the old decision stays explainable.

1. S_FULL asks for a free book under v1 (limit 3, already reached) -> reject (r2_limit).
2. v2 is published (standard limit 4). The same request is now allowed.
3. The v1 decision is explained: it is replayed with the evidence and the version it was made with.
Run: python -m eval.demo_rules   (it resets the data and the rule versions when done)
"""
from shelfsense import decision, rules, scenario
from shelfsense.stores import postgres

MEMBER, BOOK = "S_FULL", scenario.books()["free1"]


def run(verbose=True):
    say = print if verbose else (lambda *a: None)
    with postgres.connect() as c:
        postgres.load(c)
        rules.reset_to_initial(c)
    a = decision.Advisor()
    a.redis.flushdb()
    old = a.borrow(MEMBER, BOOK)
    say(f"1. rules v{a.active_rules()['version']}: {old['outcome']} - {old['reason']} (decision #{old['decision_id']})")
    v = a.publish_rules({"max_loans": {"standard": 4}}, "demo: standard limit 3 -> 4")
    new = a.borrow(MEMBER, BOOK)
    say(f"2. published v{v}: same request -> {new['outcome']} - {new['reason']} (decision #{new['decision_id']})")
    e_old, e_new = a.explain_decision(old["decision_id"]), a.explain_decision(new["decision_id"])
    for label, e in (("old", e_old), ("new", e_new)):
        d = e["decision"]
        say(f"3. {label} decision #{d['id']}: rule v{d['rule_version']} (active now v{e['active_version']}), "
            f"deciding rule {d['rule_id']}, replay {e['replay_outcome']} -> {'matches' if e['replay_ok'] else 'DIFFERS'}")
    a.close()
    with postgres.connect() as c:
        postgres.load(c)
        rules.reset_to_initial(c)
    return {"v1_outcome": old["outcome"], "v2_outcome": new["outcome"],
            "old_decision": {"rule_version": e_old["decision"]["rule_version"], "replay_ok": e_old["replay_ok"],
                             "active_version_at_explain": e_old["active_version"]},
            "new_decision": {"rule_version": e_new["decision"]["rule_version"], "replay_ok": e_new["replay_ok"]}}


if __name__ == "__main__":
    run()
