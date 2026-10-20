"""MongoDB store: system of record for book records (fields differ by book type)."""
import json
import random
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import CollectionInvalid

from shelfsense.config import DATA_DIR, MONGO_DB, MONGO_URI

GEN = DATA_DIR / "generated"

_COMMON = {
    "title": {"bsonType": "string", "minLength": 1},
    "authors": {"bsonType": "array", "minItems": 1, "items": {"bsonType": "string"}},
    "year": {"bsonType": "int"},
    "genre": {"bsonType": "string"},
    "description": {"bsonType": "string"},
    "subjects": {"bsonType": "array", "items": {"bsonType": "string"}},
}
_COMMON_REQ = ["title", "authors", "year", "genre", "description", "book_type"]


def _variant(book_type, extra_props, extra_req):
    return {
        "bsonType": "object",
        "required": _COMMON_REQ + extra_req,
        "properties": {**_COMMON, "book_type": {"enum": [book_type]}, **extra_props},
    }


SCHEMA = {"$jsonSchema": {"oneOf": [
    _variant("paperback", {"pages": {"bsonType": "int", "minimum": 1}, "isbn": {"bsonType": "string"}},
             ["pages", "isbn"]),
    _variant("ebook", {"file_format": {"enum": ["epub", "pdf", "mobi"]},
                       "size_mb": {"bsonType": ["double", "int"], "minimum": 0}},
             ["file_format", "size_mb"]),
    _variant("journal", {"issn": {"bsonType": "string"}, "volume": {"bsonType": "int"},
                         "issue": {"bsonType": "int"}},
             ["issn", "volume", "issue"]),
]}}


def connect():
    return MongoClient(MONGO_URI)[MONGO_DB]


def create_collection(db):
    db.drop_collection("books")
    try:
        db.create_collection("books", validator=SCHEMA, validationAction="error", validationLevel="strict")
    except CollectionInvalid:
        pass
    return db["books"]


def load(db):
    """Load book records. Type-specific fields (isbn, issn, ...) are synthetic."""
    rnd = random.Random(42)
    coll = create_collection(db)
    docs = []
    for b in json.loads((GEN / "books.json").read_text(encoding="utf-8")):
        d = {"_id": b["id"], "title": b["title"], "authors": b["authors"], "year": int(b["year"]),
             "genre": b["genre"], "subjects": b["subjects"], "description": b["description"],
             "book_type": b["book_type"]}
        if b["book_type"] == "paperback":
            d["pages"] = int(b["pages"] or rnd.randint(120, 700))
            d["isbn"] = "978" + "".join(str(rnd.randint(0, 9)) for _ in range(10))
        elif b["book_type"] == "ebook":
            d["file_format"] = rnd.choice(["epub", "pdf", "mobi"])
            d["size_mb"] = round(rnd.uniform(0.5, 25), 1)
        else:
            d["issn"] = f"{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}"
            d["volume"], d["issue"] = rnd.randint(1, 60), rnd.randint(1, 12)
        docs.append(d)
    coll.insert_many(docs)
    return len(docs)


def type_defaults(book_type, rnd=random):
    """Synthetic type-specific fields for a newly added book (the validator requires them)."""
    if book_type == "paperback":
        return {"pages": rnd.randint(120, 700), "isbn": "978" + "".join(str(rnd.randint(0, 9)) for _ in range(10))}
    if book_type == "ebook":
        return {"file_format": rnd.choice(["epub", "pdf", "mobi"]), "size_mb": round(rnd.uniform(0.5, 25), 1)}
    return {"issn": f"{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}",
            "volume": rnd.randint(1, 60), "issue": rnd.randint(1, 12)}
