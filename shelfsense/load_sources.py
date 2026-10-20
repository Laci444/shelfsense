"""Load PostgreSQL and MongoDB from data/generated and flush the Redis cache. Usage: shelfsense db load"""
from shelfsense.config import PROPAGATE
from shelfsense.stores import mongo, postgres, redis_cache


def main():
    with postgres.connect() as conn:
        postgres.load(conn)
        for t in ("members", "books", "loans", "waitlist"):
            print("postgres", t, conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
    print("mongo books", mongo.load(mongo.connect()))
    redis_cache.connect().flushdb()
    print("redis flushed")
    if PROPAGATE:
        from shelfsense import sync
        sync.sync_graph()
        print("neo4j graph rebuilt (run `shelfsense db sync` once if Qdrant is empty or the books changed)")


if __name__ == "__main__":
    main()
