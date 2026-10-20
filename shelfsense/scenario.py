"""The scenario books chosen by data/generate.py, by role (free1, free2, last, none, none2, journal, ebook, held).

  free1/free2: copies available   last: exactly one copy left   none/none2: all copies on loan
  journal: a journal              ebook: an ebook               held: book that member S_HAS holds
Members S_STD, S_PREM, S_SUSP, S_FULL, S_HAS, S_WAIT always exist.
"""
import json

from shelfsense.config import DATA_DIR


def books():
    return json.loads((DATA_DIR / "generated" / "scenario.json").read_text(encoding="utf-8"))
