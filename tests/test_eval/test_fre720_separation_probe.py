"""FRE-720 -- pure unit tests for the insights-corpus separation probe (ADR-0105 D10 / AC-8).

No substrate, no embedder call: the loader, the D10 branch decision, and the artifact
schema are all pure functions over synthetic or committed-fixture inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.eval.fre435_memory_recall.separation_report import (
    SeparationStats,
    summarize_separation,
)
from scripts.eval.fre720_insights_separation.decision import decide_branch
from scripts.eval.fre720_insights_separation.probe_pairs import (
    Corpus,
    CorpusEntry,
    PairSetError,
    load_corpus,
    load_pair_set,
)
from scripts.eval.fre720_insights_separation.separation_probe import build_artifact

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_PATH = _REPO_ROOT / "scripts/eval/fre720_insights_separation/corpus.yaml"
_PAIRS_PATH = _REPO_ROOT / "scripts/eval/fre720_insights_separation/pairs.yaml"


def _corpus(entry_ids: list[str]) -> Corpus:
    return Corpus(
        entries={
            i: CorpusEntry(entry_id=i, text=f"text {i}", category="c", scope="s") for i in entry_ids
        }
    )


class TestLoadCorpus:
    """The committed corpus.yaml loads and matches the pairs.yaml it backs."""

    def test_loads_the_committed_corpus(self) -> None:
        """The 35-doc real proposal corpus loads with non-empty text on every entry."""
        corpus = load_corpus(_CORPUS_PATH)
        assert len(corpus.entries) == 35
        assert all(e.text.strip() for e in corpus.entries.values())

    def test_rejects_missing_entries_key(self, tmp_path: Path) -> None:
        """A YAML file with no top-level `entries` mapping is rejected."""
        path = tmp_path / "corpus.yaml"
        path.write_text("not_entries: {}\n")
        with pytest.raises(PairSetError, match="entries"):
            load_corpus(path)


class TestLoadPairSet:
    """Degenerate-set + unknown-reference guards, mirroring probes.py's discipline."""

    def test_loads_the_committed_pair_set(self) -> None:
        """The committed 25-positive / 24-negative pair set loads against the corpus."""
        corpus = load_corpus(_CORPUS_PATH)
        pairs = load_pair_set(_PAIRS_PATH, corpus)
        assert len(pairs) == 49
        assert sum(1 for p in pairs if p.label == "positive") == 25
        assert sum(1 for p in pairs if p.label == "negative") == 24

    def test_rejects_pair_referencing_unknown_entry_id(self, tmp_path: Path) -> None:
        """A pair whose `a`/`b` is absent from the corpus is rejected by entry_id."""
        corpus = _corpus(["a", "b"])
        path = tmp_path / "pairs.yaml"
        path.write_text(
            "pairs:\n"
            "  - {pair_id: P1, a: a, b: nonexistent, label: positive}\n"
            "  - {pair_id: P2, a: a, b: b, label: negative}\n"
        )
        with pytest.raises(PairSetError, match="nonexistent"):
            load_pair_set(path, corpus)

    @pytest.mark.parametrize(
        "body",
        [
            # All positive -- nothing to separate against.
            "pairs:\n  - {pair_id: P1, a: a, b: b, label: positive}\n",
            # All negative -- no true-match signal at all.
            "pairs:\n  - {pair_id: P1, a: a, b: b, label: negative}\n",
            # Empty.
            "pairs: []\n",
        ],
    )
    def test_rejects_degenerate_pair_set(self, tmp_path: Path, body: str) -> None:
        """All-positive, all-negative, or empty pair sets cannot measure separation."""
        corpus = _corpus(["a", "b"])
        path = tmp_path / "pairs.yaml"
        path.write_text(body)
        with pytest.raises(PairSetError, match="degenerate"):
            load_pair_set(path, corpus)


class TestDecideBranch:
    """ADR-0105 D10: adopt semantic dedup only on a clean cosine floor."""

    def test_clean_floor_recommends_semantic(self) -> None:
        """`max(negatives) < min(positives)` -> the semantic-vector branch."""
        stats = summarize_separation(positives=[0.80, 0.85, 0.90], negatives=[0.40, 0.55, 0.60])
        assert stats.clean_floor is True
        assert decide_branch(stats) == "semantic"

    def test_overlap_recommends_fallback(self) -> None:
        """Overlapping clouds -> the category+facet fallback branch."""
        stats = summarize_separation(positives=[0.70, 0.78, 0.82], negatives=[0.60, 0.79, 0.81])
        assert stats.clean_floor is False
        assert decide_branch(stats) == "fallback"


class TestBuildArtifact:
    """The AC-8 artifact schema: every required field present, from synthetic inputs."""

    def test_artifact_contains_every_ac8_field(self) -> None:
        """Every ADR-0105 AC-8-required field is populated, from synthetic separation stats."""
        stats: SeparationStats = summarize_separation(
            positives=[0.80, 0.85, 0.90], negatives=[0.40, 0.55, 0.60]
        )
        artifact = build_artifact(
            corpus_size=35,
            total_corpus_at_pull_time=1857,
            positive_pairs=25,
            negative_pairs=24,
            stats=stats,
            floor_recall=1.0,
            floor_fpr=0.0,
            floor_value=0.65,
            floor_youden_j=1.0,
            decision="semantic",
            time_window_start="2026-04-15T21:36:47Z",
            time_window_end="2026-04-29T09:37:32Z",
            probe_code_version="abc1234",
            git_dirty=False,
            corpus_sha256="deadbeef",
            pairs_sha256="cafef00d",
            run_id="run-abc1234-20260705",
        )
        required_fields = {
            "corpus_source",
            "query_description",
            "time_window",
            "item_counts",
            "pair_counts",
            "cosine_distributions",
            "chosen_floor",
            "decision",
            "probe_code_version",
            "git_dirty",
            "corpus_sha256",
            "pairs_sha256",
            "run_id",
        }
        assert required_fields <= artifact.keys()
        assert artifact["decision"] == "semantic"
        assert artifact["item_counts"] == {"corpus_docs": 35, "total_corpus_at_pull_time": 1857}
        assert artifact["pair_counts"] == {"positive": 25, "negative": 24}
        assert artifact["cosine_distributions"] == stats.__dict__
        assert artifact["time_window"] == {
            "start": "2026-04-15T21:36:47Z",
            "end": "2026-04-29T09:37:32Z",
        }
