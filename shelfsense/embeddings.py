"""Embeddings: all-MiniLM-L6-v2, L2-normalised (so cosine == dot product)."""
from functools import lru_cache

from shelfsense.config import EMBED_MODEL


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL)


def embed(texts, batch_size=64):
    return _model().encode(list(texts), batch_size=batch_size, normalize_embeddings=True,
                           show_progress_bar=False)


def book_text(b):
    return f"{b['title']}. {b['description']}"
