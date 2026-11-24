"""Redis store (derived): cached decisions and reading lists, all with TTLs.

Every cached decision keeps source_id (the PostgreSQL books.id whose availability it was derived from;
a read-only check writes no decisions row) and refreshed_at.
"""
import json
import time

import redis

from shelfsense.config import DECISION_TTL_S, READING_LIST_TTL_S, REDIS_URL


def connect():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def decision_key(member_id, book_id):
    return f"decision:{member_id}:{book_id}"


def cache_decision(r, member_id, book_id, outcome, reason, source_id=None):
    r.set(decision_key(member_id, book_id),
          json.dumps({"outcome": outcome, "reason": reason, "source_id": source_id,
                      "refreshed_at": time.time()}),
          ex=DECISION_TTL_S)


def get_decision(r, member_id, book_id):
    v = r.get(decision_key(member_id, book_id))
    return json.loads(v) if v else None


def invalidate_book(r, book_id):
    """Delete every cached decision for a book (used when its availability changes)."""
    n = 0
    for k in r.scan_iter(f"decision:*:{book_id}"):
        n += r.delete(k)
    return n


def invalidate_all_decisions(r):
    """Delete every cached decision (used when the rules change: they were made with the old version)."""
    n = 0
    for k in r.scan_iter("decision:*"):
        n += r.delete(k)
    return n


def _list_key(member_id):
    return f"readinglist:{member_id}"


def add_to_list(r, member_id, book_id):
    r.sadd(_list_key(member_id), book_id)
    r.expire(_list_key(member_id), READING_LIST_TTL_S)


def remove_from_list(r, member_id, book_id):
    r.srem(_list_key(member_id), book_id)


def read_list(r, member_id):
    r.expire(_list_key(member_id), READING_LIST_TTL_S)  # activity renews the TTL
    return sorted(r.smembers(_list_key(member_id)))
