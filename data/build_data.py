"""Build the whole dataset in one go: books from Open Library -> synthetic data -> labels.

Usage: python -m data.build_data [--books N] [--skip-fetch]
  --books N      about how many books (default 20000; try 500 for a quick small dataset)
  --skip-fetch   reuse the existing data/raw/books.json (only regenerate synthetic data and labels)
Set SHELFSENSE_DATA=/some/dir to write raw/, generated/ and labels/ somewhere else.
Afterwards: python -m shelfsense.load_sources && python -m shelfsense.sync
"""
import argparse

from data import build_labels, fetch_books, generate

def run(books=fetch_books.DEFAULT_BOOKS, skip_fetch=False):
    if not skip_fetch:
        fetch_books.run(books)
    generate.main()
    build_labels.main()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--books", type=int, default=fetch_books.DEFAULT_BOOKS)
    ap.add_argument("--skip-fetch", action="store_true")
    a = ap.parse_args()
    run(a.books, a.skip_fetch)
