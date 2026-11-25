"""Build data/labels/interest_queries.json for whatever dataset is in data/raw/books.json.

Relevance is defined by rules written BEFORE any embedding or tuning:
a book is relevant to a query if its genre is in `genres` AND the regex `rx`
matches its title + subjects + description (case-insensitive). The 20 queries and rules are fixed
below; only the list of relevant book ids depends on the dataset, so the labels work for any size.
The loan labels (loan_cases.json) are written by data/generate.py.
Usage: python -m data.build_labels
"""
import json
import re

from shelfsense.config import DATA_DIR

QUERIES = [
    ("a detective investigating a murder in a small town", ["mystery"], r"detective|murder|investigat"),
    ("espionage and cold war intrigue", ["mystery", "history", "science_fiction", "biography"], r"\bspy\b|spies|espionage|cold war|secret agent|intelligence service"),
    ("epic fantasy with dragons and magic", ["fantasy"], r"dragon"),
    ("wizards and magical schools", ["fantasy"], r"wizard|magic|sorcer"),
    ("first contact with alien civilisations", ["science_fiction"], r"alien|extraterrestrial|first contact"),
    ("robots and artificial intelligence", ["science_fiction"], r"robot|artificial intelligence|android"),
    ("space travel and interstellar war", ["science_fiction"], r"\bspace\b|starship|interstellar|galax"),
    ("a love story between two people from different worlds", ["romance"], r"love|romance|romantic"),
    ("scary haunted house ghost story", ["horror"], r"haunt|ghost"),
    ("vampires and the undead", ["horror"], r"vampire|zombie|undead"),
    ("life story of a famous leader", ["biography"], r"president|leader|king|queen|prime minister|general"),
    ("memoir about overcoming hardship", ["biography"], r"memoir|struggle|overcom|survive|childhood"),
    ("history of the Second World War", ["history"], r"world war|nazi|second world war"),
    ("ancient Rome and Greece", ["history"], r"\brome\b|roman|greek|greece|athens"),
    ("classic poems about nature and the seasons", ["poetry"], r"nature|season|spring|autumn|winter|sea"),
    ("traditional recipes and home cooking", ["cooking"], r"recipe|cook|baking|kitchen"),
    ("physics and the origin of the universe", ["science"], r"physic|universe|cosmos|quantum"),
    ("serial killer psychological thriller", ["mystery", "horror"], r"serial killer|psychological|killer"),
    ("war and its effect on ordinary families", ["history", "biography", "romance"], r"\bwar\b|soldier|battle"),
    ("coming-of-age adventure for young readers", ["fantasy", "science_fiction", "mystery"], r"young|boy|girl|teen|coming of age|adventure"),
]


def main():
    import sys

    from shelfsense import labels
    if labels.LOCK.exists() and "--force" not in sys.argv:
        sys.exit("labels are frozen (data/labels/labels.lock.json); rebuilding them would invalidate every "
                 "measurement. Use --force only if you really mean to start over, then re-freeze.")
    books = json.loads((DATA_DIR / "raw" / "books.json").read_text(encoding="utf-8"))
    out = []
    for i, (q, genres, rx) in enumerate(QUERIES, 1):
        pat = re.compile(rx, re.I)
        rel = [b["id"] for b in books if b["genre"] in genres
               and pat.search(" ".join([b["title"], " ".join(b["subjects"]), b["description"]]))]
        out.append({"id": i, "query": q, "genres": genres, "rule": rx, "relevant": rel})
        print(f"{i:2d} {len(rel):3d}  {q}")
    (DATA_DIR / "labels").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "labels" / "interest_queries.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
