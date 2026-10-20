"""Qdrant store (derived): book embeddings, cosine distance, payload indexes on genre/year.

Qdrant point ids must be UUIDs or ints, so the text id (Open Library work id) is mapped
deterministically with uuid5 and kept in the payload as `source_id`.
"""
import time
import uuid

from qdrant_client import QdrantClient, models

from shelfsense.config import COLLECTION, EMBED_DIM, QDRANT_URL

_NS = uuid.UUID("5b1e5c0e-0c0e-4c53-8f0e-5e1f5e5e5e5e")


def point_id(source_id):
    return str(uuid.uuid5(_NS, source_id))


def connect():
    return QdrantClient(url=QDRANT_URL)


def rebuild(client, books, vectors, hnsw_m=16, hnsw_ef_construct=100):
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
        hnsw_config=models.HnswConfigDiff(m=hnsw_m, ef_construct=hnsw_ef_construct),
        # one segment: the default (one per CPU) splits 20k vectors into pieces below the HNSW threshold
        optimizers_config=models.OptimizersConfigDiff(default_segment_number=1))
    client.create_payload_index(COLLECTION, "genre", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(COLLECTION, "year", models.PayloadSchemaType.INTEGER)
    now = time.time()
    client.upload_points(COLLECTION, [
        models.PointStruct(id=point_id(b["id"]), vector=v.tolist(),
                           payload={"source_id": b["id"], "title": b["title"], "genre": b["genre"],
                                    "year": int(b["year"]), "refreshed_at": now})
        for b, v in zip(books, vectors)], batch_size=128, wait=True)


def _payload(b):
    return {"source_id": b["id"], "title": b["title"], "genre": b["genre"], "year": int(b["year"]),
            "refreshed_at": time.time()}


def upsert_book(client, b, vector):
    """Insert or replace the single point of one book (re-embedded by the caller)."""
    client.upsert(COLLECTION, [models.PointStruct(id=point_id(b["id"]), vector=list(map(float, vector)),
                                                  payload=_payload(b))], wait=True)


def update_payload(client, b):
    """Metadata changed but the text did not: keep the vector, refresh the payload."""
    client.set_payload(COLLECTION, _payload(b), points=[point_id(b["id"])], wait=True)


def delete_book(client, book_id):
    client.delete(COLLECTION, points_selector=models.PointIdsList(points=[point_id(book_id)]), wait=True)


def search(client, vector, limit=5, genre=None, year_from=None, hnsw_ef=None, exact=False):
    must = []
    if genre:
        must.append(models.FieldCondition(key="genre", match=models.MatchValue(value=genre)))
    if year_from is not None:
        must.append(models.FieldCondition(key="year", range=models.Range(gte=year_from)))
    res = client.query_points(
        COLLECTION, query=list(map(float, vector)), limit=limit,
        query_filter=models.Filter(must=must) if must else None,
        search_params=models.SearchParams(hnsw_ef=hnsw_ef, exact=exact),
        with_payload=True)
    return [(p.payload["source_id"], p.score) for p in res.points]
