"""Editorial proof guides: served only for the exact pinned source, never as a substitute proof."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from euler.observability import app, monitor, proof_guide
from euler.observability.derivation import proof_sections
from .test_monitor import _write, seed_research_events

PROOF = ("# Lemma\n\nWe construct the cover first. Then we apply the join, with $x+y$ fixed.\n\n"
         "## Step two\nThe cycle closes because every seam is an original edge.\n")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "guided"
    _write(root / "project.json", {"name": "guided", "workers": []})
    _write(root / "fact_graph/facts/f1.md", "---\nfact_id: f1\nauthor: a\npredecessors: []\n---\n"
           "## statement\nA cycle exists.\n## proof\n" + PROOF + "## intuition\nJoin two covers.\n")
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(root))
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    monitor._CACHE.clear()
    yield root
    monitor._CACHE.clear()


def _guide(proof, project="guided"):
    sentence = proof.index("Then we apply")
    second = proof.index("## Step two")
    return {"project": project, "fact_id": "f1", "proof_sha256": proof_guide.digest(proof.strip()),
            "steps": [{"text": {"zh": "先构造覆盖，再拼接。", "en": "Build the cover, then join."},
                       "cite": [{"start": sentence, "end": second, "quote": "Then we apply the join"}]},
                      {"text": {"zh": "闭合。", "en": "Close."},
                       "cite": [{"start": second, "end": len(proof.strip()), "quote": "## Step two"}]}]}


def test_shipped_guides_are_pinned_bilingual_and_cite_offsets():
    guides = proof_guide.load_guides()
    assert guides == []  # Private campaign guides are deployment data, not distribution assets.
    assert len({(g["project"], g["fact_id"]) for g in guides}) == len(guides)
    for guide in guides:
        assert re.fullmatch(r"[0-9a-f]{64}", guide["proof_sha256"]) and guide["project"] and guide["fact_id"]
        assert guide["steps"]
        assert any(step["role"] == "argument" for step in guide["steps"])
        for step in guide["steps"]:
            assert step["role"] in proof_guide.ROLES
            assert step["text"]["zh"].strip() and step["text"]["en"].strip()
            assert not re.search(r"[\u3400-\u9fff\uff00-\uffef]", step["text"]["en"])
            # Short headings stay headings: shorter than the step they summarize, bilingual, no CJK in English.
            assert 0 < len(step["title"]["zh"]) < len(step["text"]["zh"]) and 0 < len(step["title"]["en"]) <= 70
            assert not re.search(r"[\u3400-\u9fff\uff00-\uffef]", step["title"]["en"])
            for cite in step["cite"]:
                assert 0 <= cite["start"] < cite["end"] and 0 < len(cite["quote"]) <= cite["end"] - cite["start"]


def test_guide_is_served_only_while_the_exact_source_matches(project, monkeypatch, tmp_path):
    source = PROOF.strip()
    guides = tmp_path / "guides.json"
    _write(guides, {"guides": [_guide(PROOF)]})
    monkeypatch.setattr(proof_guide, "GUIDES_PATH", guides)
    client = TestClient(app)
    view = client.get("/api/derivation", params={"layer": "facts", "node_id": "f1"}).json()
    assert view["proof"] == source and view["proof_sha256"] == proof_guide.digest(source)
    process = view["process"]
    assert process["status"] == "editorial" and process["source_sha256"] == view["proof_sha256"]
    first, second = (step["cites"][0] for step in process["steps"])
    assert source[first["start"]:first["end"]].startswith("Then we apply the join") and not first["whole"]
    assert view["sections"][second["section"]]["heading"] == "Step two" and second["whole"]
    assert view["intuition"] == "Join two covers."
    # One changed character withdraws the guide until it is re-read and re-pinned.
    path = project / "fact_graph/facts/f1.md"
    path.write_text(path.read_text().replace("original edge", "original  edge"))
    monitor._CACHE.clear()
    changed = client.get("/api/derivation", params={"layer": "facts", "node_id": "f1"}).json()
    assert changed["process"]["status"] == "withdrawn" and changed["process"]["reason"] == "source_changed"
    assert "steps" not in changed["process"] and changed["sections"]
    # Another project's guide never applies to this source.
    _write(guides, {"guides": [_guide(PROOF, project="elsewhere")]})
    monitor._CACHE.clear()
    path.write_text(path.read_text().replace("original  edge", "original edge"))
    assert client.get("/api/derivation", params={"layer": "facts", "node_id": "f1"}).json()["process"] is None


def test_mismatched_or_unsafe_anchors_withdraw_the_whole_guide():
    source = PROOF.strip()
    sections = proof_sections(source)
    good = _guide(PROOF)
    good["steps"][1]["role"] = "audit"
    good["steps"][0]["title"] = {"zh": "构造并拼接", "en": "Build and join"}
    good["steps"][1]["title"] = {"zh": "只有中文"}
    served = proof_guide.validate(good, source, sections)
    assert served["status"] == "editorial" and [step["role"] for step in served["steps"]] == ["argument", "audit"]
    assert served["steps"][0]["title"]["en"] == "Build and join" and served["steps"][1]["title"] is None
    wrong_quote = json.loads(json.dumps(good)); wrong_quote["steps"][0]["cite"][0]["quote"] = "Then we prove"
    assert proof_guide.validate(wrong_quote, source, sections)["reason"] == "anchor_mismatch"
    inside_math = json.loads(json.dumps(good))
    cut = source.index("+y$")
    inside_math["steps"][0]["cite"][0]["end"] = cut
    assert proof_guide.validate(inside_math, source, sections)["reason"] == "anchor_mismatch"
    across = json.loads(json.dumps(good)); across["steps"][0]["cite"][0]["end"] = len(source)
    assert proof_guide.validate(across, source, sections)["reason"] == "anchor_mismatch"


def test_absent_source_has_no_process_and_headingless_text_keeps_only_ordinals(project, monkeypatch, tmp_path):
    ids = seed_research_events(project)
    monitor._CACHE.clear()
    view = TestClient(app).get("/api/derivation", params={"layer": "research", "node_id": ids["target"]}).json()
    assert view["sections"] == [] and view["proof"] == "" and view["process"] is None
    assert view["proof_sha256"] is None and view["gap_code"] == "no_proof_text"
    assert view["source_kind"] == "recorded_relations" and view["routes"]
    plain = "A first paragraph of argument." * 30 + "\n\n" + "A second paragraph." * 40
    units = proof_sections(plain)
    assert len(units) >= 2 and all(unit["heading"] is None for unit in units)
    assert [unit["ordinal"] for unit in units] == list(range(1, len(units) + 1))
    assert "".join(unit["text"] for unit in units) == plain
