"""Frozen evaluation labels.

The labels (relevant books per query, expected loan outcomes) must not be changed after tuning starts, or
the measurements prove nothing. `freeze` stores a SHA-256 of each label file in data/labels/labels.lock.json;
`verify` recomputes them. Evaluations print the status, a test fails when a file differs from the lock,
and data.build_labels refuses to overwrite frozen labels unless --force is given.
"""
import hashlib
import json
from datetime import date

from shelfsense.config import DATA_DIR

LABEL_DIR = DATA_DIR / "labels"
LOCK = LABEL_DIR / "labels.lock.json"
FILES = ("interest_queries.json", "loan_cases.json")


def digest(name):
    return hashlib.sha256((LABEL_DIR / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def freeze(note=""):
    lock = {"frozen_on": date.today().isoformat(), "note": note, "sha256": {f: digest(f) for f in FILES},
            "counts": {"interest_queries": len(json.loads((LABEL_DIR / FILES[0]).read_text(encoding="utf-8"))),
                       "loan_cases": len(json.loads((LABEL_DIR / FILES[1]).read_text(encoding="utf-8")))}}
    LOCK.write_text(json.dumps(lock, indent=1), encoding="utf-8")
    return lock


def verify():
    """{'frozen': bool, 'ok': bool, 'files': {name: 'match'|'CHANGED'|'unfrozen'}, 'frozen_on': ...}"""
    if not LOCK.exists():
        return {"frozen": False, "ok": False, "files": {f: "unfrozen" for f in FILES}, "frozen_on": None}
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    files = {f: ("match" if lock["sha256"].get(f) == digest(f) else "CHANGED") for f in FILES}
    return {"frozen": True, "ok": all(v == "match" for v in files.values()), "files": files,
            "frozen_on": lock["frozen_on"], "counts": lock.get("counts")}


def banner():
    v = verify()
    if not v["frozen"]:
        return "labels: NOT frozen (run `shelfsense eval labels --freeze`)"
    return f"labels: frozen {v['frozen_on']}, " + ("unchanged" if v["ok"] else f"CHANGED since freeze {v['files']}")
