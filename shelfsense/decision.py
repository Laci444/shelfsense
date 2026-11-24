"""Decision logic across all five stores.

advise(member, book)  read path:  Redis cache (aside) -> PostgreSQL rules, then suggestions:
                      Neo4j (also borrowed / same author) + Qdrant (similar blurbs),
                      filtered to books with a free copy (PostgreSQL) and enriched from MongoDB.
borrow(member, book)  write path: PostgreSQL transaction (authoritative), then the cached
                      decisions for that book are deleted so the next read reloads them.
recommend(text)       free-text interest -> Qdrant (optional genre filter) -> PostgreSQL availability.

Derived stores follow the sources of record immediately (incremental, one book / one loan at a time):
  borrow / return  -> Neo4j BORROWED edge (created, marked returned; a waitlist promotion creates one too)
  add / update / delete book -> PostgreSQL + MongoDB, then Neo4j node and Qdrant point (re-embedded only
                      when title or description changed). `sync` stays as the full rebuild / repair.
If a derived store is unreachable the source change still stands; the error is kept in `derived_errors`.
"""
import random
import time

from shelfsense import embeddings, rules
from shelfsense.config import PROPAGATE
from shelfsense.stores import mongo, neo4j_graph, postgres, qdrant_store, redis_cache


class Advisor:
    def __init__(self):
        self.pg = postgres.connect()
        self.pg.autocommit = True  # reads must not leave a transaction open (it would block resets/TRUNCATE)
        self.mongo = mongo.connect()
        self.redis = redis_cache.connect()
        self.neo = neo4j_graph.connect()
        self.qdrant = qdrant_store.connect()
        self.propagate = PROPAGATE
        self.derived_errors = []

    def _derive(self, label, fn):
        """Run one update of a derived store. It must never undo the committed source change."""
        if not self.propagate:
            return
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.derived_errors.append(f"{label}: {e}")

    # --- book catalogue (PostgreSQL + MongoDB, then Neo4j + Qdrant) ---------------------------
    def add_book(self, book, copies=1):
        """book: id, title, authors, year, genre, description, book_type (+ optional subjects/type fields)."""
        doc = {"subjects": [], **book}
        doc["year"] = int(doc["year"])
        doc.update({k: v for k, v in mongo.type_defaults(doc["book_type"], random).items() if k not in doc})
        _id = doc.pop("id")
        mdoc = {**doc, "_id": _id}
        self.mongo["books"].insert_one(mdoc)  # the schema validator rejects bad documents first
        try:
            postgres.add_book(self.pg, book, copies)
        except Exception:
            self.mongo["books"].delete_one({"_id": mdoc["_id"]})
            raise
        self._index_book(book["id"])

    def update_book(self, book_id, **fields):
        """fields: title, authors, year, genre, description, subjects."""
        before = self.mongo["books"].find_one({"_id": book_id})
        if before is None:
            raise LookupError("unknown book")
        if "year" in fields:
            fields["year"] = int(fields["year"])
        postgres.update_book(self.pg, book_id, fields)
        self.mongo["books"].update_one({"_id": book_id}, {"$set": fields})
        redis_cache.invalidate_book(self.redis, book_id)
        text_changed = any(fields.get(k, before[k]) != before[k] for k in ("title", "description"))
        meta_changed = any(fields.get(k, before[k]) != before[k] for k in ("genre", "year", "authors"))
        if text_changed or meta_changed:
            self._index_book(book_id, re_embed=text_changed)

    def delete_book(self, book_id):
        postgres.delete_book(self.pg, book_id)
        self.mongo["books"].delete_one({"_id": book_id})
        redis_cache.invalidate_book(self.redis, book_id)
        self._derive("Neo4j", lambda: neo4j_graph.delete_book(self.neo, book_id))
        self._derive("Qdrant", lambda: qdrant_store.delete_book(self.qdrant, book_id))

    def _index_book(self, book_id, re_embed=True):
        """Push one book from MongoDB to Neo4j and Qdrant (only this book is embedded)."""
        doc = self.mongo["books"].find_one({"_id": book_id})
        doc["id"] = doc.pop("_id")
        self._derive("Neo4j", lambda: neo4j_graph.upsert_book(self.neo, doc))
        if re_embed:
            self._derive("Qdrant", lambda: qdrant_store.upsert_book(
                self.qdrant, doc, embeddings.embed([embeddings.book_text(doc)])[0]))
        else:
            self._derive("Qdrant", lambda: qdrant_store.update_payload(self.qdrant, doc))

    def close(self):
        self.pg.close()
        self.neo.close()

    # --- explain, return, profiles ---------------------------------------
    def explain(self, member_id, book_id):
        """Every loan rule with pass/fail (read-only, straight from PostgreSQL)."""
        return postgres.trace(self.pg, member_id, book_id)

    def explain_decision(self, decision_id):
        """A stored decision, re-evaluated from its own stored evidence and rule version."""
        return postgres.explain_decision(self.pg, decision_id)

    # --- versioned rules ----------------------------------------------------
    def active_rules(self):
        return rules.active(self.pg)

    def rules_history(self):
        return rules.history(self.pg)

    def publish_rules(self, changes, note=None):
        """New rule version. Cached decisions were made with the old one, so they are deleted."""
        version = rules.publish(self.pg, changes, note)
        redis_cache.invalidate_all_decisions(self.redis)
        return version

    def return_book(self, member_id, book_id):
        out = postgres.return_book(self.pg, member_id, book_id)
        redis_cache.invalidate_book(self.redis, book_id)
        self._derive("Neo4j", lambda: neo4j_graph.record_return(self.neo, member_id, book_id))
        if out["promoted"]:
            self._derive("Neo4j", lambda: neo4j_graph.record_loan(self.neo, out["promoted"], book_id))
        return out

    def leave_waitlist(self, member_id, book_id):
        postgres.leave_waitlist(self.pg, member_id, book_id)
        redis_cache.invalidate_book(self.redis, book_id)

    def member_profile(self, member_id):
        return postgres.member_profile(self.pg, member_id)

    def book_details(self, book_id):
        """One book as seen by each store."""
        row = self.pg.execute("SELECT id, title, genre, year, book_type, available, total FROM books WHERE id = %s",
                              (book_id,)).fetchone()
        if row is None:
            raise LookupError("unknown book")
        pg = dict(zip(("id", "title", "genre", "year", "book_type", "available", "total"), row))
        pg["waiting"] = self.pg.execute("SELECT count(*) FROM waitlist WHERE book_id = %s", (book_id,)).fetchone()[0]
        pg["on_loan"] = self.pg.execute("SELECT count(*) FROM loans WHERE book_id = %s AND returned_at IS NULL",
                                        (book_id,)).fetchone()[0]
        pg["times_borrowed"] = self.pg.execute("SELECT count(*) FROM loans WHERE book_id = %s",
                                               (book_id,)).fetchone()[0]
        doc = self.mongo["books"].find_one({"_id": book_id}) or {}
        doc.pop("_id", None)
        with self.neo.session() as s:
            rec = s.run("MATCH (b:Book {source_id: $id}) OPTIONAL MATCH (b)<-[x:BORROWED]-() "
                        "RETURN count(x) AS readers, b.refreshed_at AS refreshed", id=book_id).single()
        neo = {"readers": rec["readers"], "refreshed_at": str(rec["refreshed"])[:19] if rec["refreshed"] else None}
        cached = sum(1 for _ in self.redis.scan_iter(f"decision:*:{book_id}"))
        return {"postgres": pg, "mongo": doc, "neo4j": neo, "redis_cached_decisions": cached}

    # --- operations ---------------------------------------------------------
    def health(self):
        """{store: (ok, milliseconds)}"""
        def timed(fn):
            t = time.perf_counter()
            try:
                fn()
                return True, round((time.perf_counter() - t) * 1000)
            except Exception:
                return False, 0
        return {"PostgreSQL": timed(lambda: self.pg.execute("SELECT 1")),
                "MongoDB": timed(lambda: self.mongo.command("ping")),
                "Redis": timed(self.redis.ping),
                "Neo4j": timed(self.neo.verify_connectivity),
                "Qdrant": timed(lambda: self.qdrant.get_collections())}

    def stats(self):
        pg = {t: self.pg.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("members", "books", "loans", "waitlist", "decisions")}
        with self.neo.session() as s:
            neo = {"books": s.run("MATCH (b:Book) RETURN count(b) AS n").single()["n"],
                   "borrowed edges": s.run("MATCH ()-[x:BORROWED]->() RETURN count(x) AS n").single()["n"]}
        return {"PostgreSQL": pg, "MongoDB": {"books": self.mongo["books"].count_documents({})}, "Neo4j": neo,
                "Qdrant": {"vectors": self.qdrant.count(qdrant_store.COLLECTION).count},
                "Redis": {"cached keys": self.redis.dbsize()}}

    def cache_entries(self, limit=100):
        out = []
        for k in self.redis.scan_iter("decision:*", count=500):
            v = redis_cache.get_decision(self.redis, *k.split(":", 2)[1:])
            if v:
                out.append({"key": k, "outcome": v["outcome"], "reason": v["reason"],
                            "ttl": self.redis.ttl(k), "age": round(time.time() - v["refreshed_at"])})
            if len(out) >= limit:
                break
        return sorted(out, key=lambda e: e["age"])

    def flush_cache(self):
        self.redis.flushdb()

    def reset_data(self):
        """PostgreSQL + MongoDB back to data/generated, Neo4j graph rebuilt from them, Redis flushed."""
        with postgres.connect() as c:
            postgres.load(c)
        mongo.load(mongo.connect())
        if self.propagate:
            from shelfsense import sync
            sync.sync_graph()
        self.redis.flushdb()

    # --- decision -------------------------------------------------------
    def decide_cached(self, member_id, book_id):
        """Cache-aside read of the decision. Returns (outcome, reason, from_cache)."""
        hit = redis_cache.get_decision(self.redis, member_id, book_id)
        if hit:
            return hit["outcome"], hit["reason"], True
        outcome, reason = postgres.check(self.pg, member_id, book_id)
        redis_cache.cache_decision(self.redis, member_id, book_id, outcome, reason, source_id=book_id)
        return outcome, reason, False

    def borrow(self, member_id, book_id):
        outcome, reason, decision_id = postgres.borrow(self.pg, member_id, book_id)
        redis_cache.invalidate_book(self.redis, book_id)
        if outcome == "allow":
            self._derive("Neo4j", lambda: neo4j_graph.record_loan(self.neo, member_id, book_id))
        return {"outcome": outcome, "reason": reason, "decision_id": decision_id}

    # --- suggestions ----------------------------------------------------
    def _available(self, ids):
        """Postgres is the truth for availability: keep only books with a free copy."""
        if not ids:
            return {}
        rows = self.pg.execute(
            "SELECT id, title, genre, available FROM books WHERE id = ANY(%s) AND available > 0", (ids,))
        return {r[0]: {"title": r[1], "genre": r[2], "available": r[3]} for r in rows}

    def _details(self, found):
        for bid, info in found.items():
            doc = self.mongo["books"].find_one({"_id": bid}, {"authors": 1, "book_type": 1, "year": 1})
            info["authors"] = doc["authors"]
            info["book_type"] = doc["book_type"]
            info["year"] = doc["year"]
        return found

    def similar_to(self, book_id, limit=5):
        pid = qdrant_store.point_id(book_id)
        pts = self.qdrant.retrieve(qdrant_store.COLLECTION, [pid], with_vectors=True)
        if not pts:
            return []
        hits = qdrant_store.search(self.qdrant, pts[0].vector, limit=limit * 3)
        return [h[0] for h in hits if h[0] != book_id]

    def suggestions(self, book_id, limit=5):
        """Mix graph and vector signals; keep order, drop duplicates and unavailable books."""
        graph = [r["book_id"] for r in neo4j_graph.also_borrowed(self.neo, book_id, limit * 3)]
        author = neo4j_graph.same_author(self.neo, book_id, limit * 3)
        vector = self.similar_to(book_id, limit)
        merged, seen = [], {book_id}
        for src, ids in (("same_author", author), ("also_borrowed", graph), ("similar_text", vector)):
            for i in ids:
                if i not in seen:
                    seen.add(i)
                    merged.append((i, src))
        free = self._details(self._available([i for i, _ in merged]))
        out = [{"book_id": i, "source": s, **free[i]} for i, s in merged if i in free]
        return out[:limit]

    def advise(self, member_id, book_id, limit=5):
        outcome, reason, cached = self.decide_cached(member_id, book_id)
        result = {"outcome": outcome, "reason": reason, "from_cache": cached}
        if outcome != "allow":
            result["alternatives"] = self.suggestions(book_id, limit)
        return result

    def recommend(self, text, genre=None, limit=5):
        vec = embeddings.embed([text])[0]
        hits = qdrant_store.search(self.qdrant, vec, limit=limit * 3, genre=genre)
        ids = [h[0] for h in hits]
        free = self._details(self._available(ids))
        return [{"book_id": i, **free[i]} for i in ids if i in free][:limit]
