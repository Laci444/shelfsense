"""Build data/raw/books.json from the Open Library bulk dumps (replaces the old API fetch + dump fetch).

Usage: python -m data.fetch_books [--books N]        (default 20000)

Streams ol_dump_works (4 GB) and ol_dump_authors (0.8 GB) over HTTPS; nothing of the dumps is saved to disk.
Selection: works with a description >= 200 chars (mostly ASCII = English), a title, subjects, a first
publish year and at least one author; genre = first matching rule over the work's subjects.
Works are taken in dump order (sorted by key) until N are collected. To keep genres balanced, one genre may
supply at most 1.5 x (N / number of genres) books. A few works are dropped when no author name resolves,
and thin genres (horror, fantasy, cooking) may fill less than their share, so the result is "about N".
The dump has no page counts (pages = None; the loader invents them). The "latest" dump changes over time,
so two runs months apart can return different books.
"""
import argparse
import gzip
import json
import math
import re
import time
import urllib.request

from shelfsense.config import DATA_DIR

OUT = DATA_DIR / "raw" / "books.json"
UA = {"User-Agent": "ShelfSense-university-project (student)"}
BASE = "https://openlibrary.org/data/ol_dump_{}_latest.txt.gz"
DEFAULT_BOOKS = 20000
GENRE_SLACK = 1.5

# first match wins, so specific genres come before broad ones
RULES = [
    ("science_fiction", ("science fiction",)),
    ("fantasy", ("fantasy",)),
    ("horror", ("horror", "ghost stories")),
    ("mystery", ("mystery", "detective")),
    ("romance", ("romance", "love stories")),
    ("poetry", ("poetry", "poems")),
    ("cooking", ("cooking", "cookery", "recipes")),
    ("biography", ("biography", "autobiograph")),
    ("science", ("physics", "astronomy", "biology", "chemistry", "mathematics", "evolution", "geology")),
    ("history", ("history",)),
]
YEAR = re.compile(r"\b(1[5-9]\d\d|20[0-2]\d)\b")


def stream(name):
    req = urllib.request.Request(BASE.format(name), headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, gzip.open(r, "rt", encoding="utf-8") as f:
        yield from f


def genre_of(subjects):
    low = [s.lower() for s in subjects]
    for genre, keys in RULES:
        if any(k in s for s in low for k in keys):
            return genre
    return None


def mostly_ascii(s):
    return sum(c.isascii() for c in s) / len(s) > 0.97


def collect_works(target):
    cap = math.ceil(GENRE_SLACK * target / len(RULES))
    counts = {g: 0 for g, _ in RULES}
    works, t0 = [], time.time()
    for n, line in enumerate(stream("works"), 1):
        if n % 500000 == 0:
            print(f"  {n:,} lines, kept {len(works):,}, {time.time() - t0:.0f}s", flush=True)
        if '"description"' not in line or '"subjects"' not in line or '"authors"' not in line:
            continue
        try:
            w = json.loads(line.split("\t", 4)[4])
        except (IndexError, ValueError):
            continue
        d = w.get("description")
        d = d.get("value") if isinstance(d, dict) else d
        title = w.get("title")
        subs = [s for s in w.get("subjects", []) if isinstance(s, str)]
        if not (isinstance(d, str) and len(d) >= 200 and title and subs) or not mostly_ascii(d):
            continue
        m = YEAR.search(str(w.get("first_publish_date", "")))
        akeys = [a["author"]["key"] for a in w.get("authors", [])
                 if isinstance(a, dict) and isinstance(a.get("author"), dict) and "key" in a["author"]]
        genre = genre_of(subs)
        if not (m and akeys and genre) or counts[genre] >= cap:
            continue
        counts[genre] += 1
        works.append({"id": w["key"].split("/")[-1], "title": title.strip(), "akeys": akeys[:3],
                      "year": int(m.group(1)), "genre": genre, "subjects": [s.lower() for s in subs[:15]],
                      "description": d.strip()[:1500]})
        if len(works) >= target:
            break
    print("per genre:", counts, flush=True)
    return works


def resolve_authors(wanted):
    names, t0 = {}, time.time()
    for n, line in enumerate(stream("authors"), 1):
        if n % 1000000 == 0:
            print(f"  authors {n:,} lines, resolved {len(names):,}/{len(wanted):,}, {time.time() - t0:.0f}s", flush=True)
        key = line.split("\t", 2)[1]
        if key in wanted:
            try:
                names[key] = json.loads(line.split("\t", 4)[4]).get("name")
            except (IndexError, ValueError):
                pass
            if len(names) == len(wanted):
                break
    return names


def run(target=DEFAULT_BOOKS):
    works = collect_works(target)
    names = resolve_authors({k for w in works for k in w["akeys"]})
    books = []
    for w in works:
        authors = [names[k] for k in w.pop("akeys") if names.get(k)]
        if authors:
            books.append({**w, "authors": authors, "pages": None})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    print(f"{len(books)} books (target {target}) -> {OUT}")
    return books


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--books", type=int, default=DEFAULT_BOOKS, help="how many books to collect")
    run(ap.parse_args().books)
