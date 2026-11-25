import json

from shelfsense import labels


def test_labels_match_the_freeze():
    v = labels.verify()
    assert v["frozen"], "run `shelfsense eval labels --freeze` once the labels are final"
    assert v["ok"], f"label files changed after the freeze: {v['files']}"
    assert v["counts"] == {"interest_queries": 20, "loan_cases": 15}


def test_freeze_detects_a_changed_label(tmp_path, monkeypatch):
    for f in labels.FILES:
        (tmp_path / f).write_text(json.dumps([{"id": 1, "relevant": ["a"]}]), encoding="utf-8")
    monkeypatch.setattr(labels, "LABEL_DIR", tmp_path)
    monkeypatch.setattr(labels, "LOCK", tmp_path / "lock.json")
    assert labels.verify()["frozen"] is False
    labels.freeze()
    assert labels.verify()["ok"]
    (tmp_path / labels.FILES[0]).write_text(json.dumps([{"id": 1, "relevant": ["a", "b"]}]), encoding="utf-8")
    v = labels.verify()
    assert not v["ok"] and v["files"][labels.FILES[0]] == "CHANGED"
