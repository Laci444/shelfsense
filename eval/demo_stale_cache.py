"""Failure case: a stale cached decision in Redis.

borrow() invalidates Redis AFTER the Postgres commit. If the process dies in between (simulated here
by changing Postgres directly), Redis keeps the old answer until its TTL expires.
Run: python -m eval.demo_stale_cache
"""
from shelfsense import decision, scenario
from shelfsense.stores import postgres, redis_cache

BOOK, MEMBER = scenario.books()["none"], "S_PREM"  # no copy available in the scenario data


def run(verbose=True):
    say = print if verbose else (lambda *a: None)
    with postgres.connect() as c:
        postgres.load(c)
    a = decision.Advisor()
    a.redis.flushdb()
    first = a.decide_cached(MEMBER, BOOK)
    say("1. first read          :", first)
    a.pg.execute("UPDATE books SET available = 1 WHERE id = %s", (BOOK,))  # a copy is returned
    a.pg.commit()
    truth = postgres.check(a.pg, MEMBER, BOOK)
    say("2. copy returned (PG)  :", truth, "<- truth")
    stale = a.decide_cached(MEMBER, BOOK)
    say("3. cached read (stale) :", stale)
    ttl = a.redis.ttl(redis_cache.decision_key(MEMBER, BOOK))
    say(f"   TTL left: {ttl} s -> stale at most this long")
    redis_cache.invalidate_book(a.redis, BOOK)
    fresh = a.decide_cached(MEMBER, BOOK)
    say("4. after invalidation  :", fresh)
    say("Safety net: borrow() always re-checks in Postgres under a row lock, so a stale "
        "'allow' can never oversell a copy; only the advice shown to the user is wrong.")
    a.close()
    with postgres.connect() as c:
        postgres.load(c)  # reset the data the demo changed
    return {"first_read": first, "postgres_truth": truth, "cached_read_after_change": stale,
            "ttl_left_s": ttl, "after_invalidation": fresh}


if __name__ == "__main__":
    run()
