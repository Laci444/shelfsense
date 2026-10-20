import os
from pathlib import Path

# raw/, generated/ and labels/ live under this directory (override to build a small test dataset elsewhere)
DATA_DIR = Path(os.getenv("SHELFSENSE_DATA", Path(__file__).resolve().parents[1] / "data"))

PG_DSN = os.getenv("PG_DSN", "postgresql://shelf:shelf@localhost:5432/shelfsense")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = "shelfsense"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_AUTH = ("neo4j", os.getenv("NEO4J_PASSWORD", "shelfsense"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

MONGO_DB = os.getenv("MONGO_DB", MONGO_DB)

# Tests write to separate Postgres/Mongo databases and Redis db 1; Neo4j and Qdrant are only read.
if os.getenv("SHELFSENSE_TEST"):
    PG_DSN = PG_DSN.rsplit("/", 1)[0] + "/shelfsense_test"
    MONGO_DB = "shelfsense_test"
    REDIS_URL = REDIS_URL.rsplit("/", 1)[0] + "/1"

# Changes in PostgreSQL/MongoDB are pushed to Neo4j and Qdrant right away. Tests share the real Neo4j and
# Qdrant, so they switch this off unless a test explicitly enables it (with throw-away ids).
PROPAGATE = os.getenv("SHELFSENSE_PROPAGATE", "0" if os.getenv("SHELFSENSE_TEST") else "1") == "1"

COLLECTION = "books"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
DECISION_TTL_S = 300
READING_LIST_TTL_S = 1800
