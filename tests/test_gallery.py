import numpy as np
import pytest

from app import config
from app.gallery import Gallery

DIM = config.EMBEDDING_DIM


def unit(i: int) -> np.ndarray:
    v = np.zeros(DIM, np.float32)
    v[i] = 1.0
    return v


def mix(a: np.ndarray, b: np.ndarray, w: float) -> np.ndarray:
    """Unit vector with cosine `w` to `a` (a and b orthogonal unit vectors)."""
    return w * a + np.sqrt(1 - w * w) * b


@pytest.fixture
def gallery() -> Gallery:
    return Gallery.from_people({"alice": [unit(0), unit(1)], "bob": [unit(2)]})


def test_accepts_clear_match(gallery):
    m = gallery.match(mix(unit(2), unit(9), 0.8))
    assert m.name == "bob"
    assert m.reason == "match"
    assert m.score == pytest.approx(0.8, abs=1e-5)
    assert m.second_name == "alice"
    assert m.second_score == pytest.approx(0.0, abs=1e-5)


def test_matches_best_embedding_not_average(gallery):
    # Exactly like alice's 2nd embedding; her average would only score ~0.71.
    m = gallery.match(unit(1))
    assert m.name == "alice"
    assert m.score == pytest.approx(1.0, abs=1e-5)


def test_below_threshold_is_unknown(gallery):
    m = gallery.match(mix(unit(2), unit(9), 0.3), threshold=0.45)
    assert m.name is None
    assert m.best_name == "bob"
    assert m.reason == "below threshold"


def test_low_margin_is_unknown(gallery):
    q = (unit(0) + unit(2)) / np.sqrt(2)  # equally close to alice and bob (~0.707)
    m = gallery.match(q, threshold=0.45, margin=0.05)
    assert m.name is None
    assert m.reason == "low margin"


def test_single_person_has_no_margin_check():
    g = Gallery.from_people({"alice": [unit(0)]})
    m = g.match(mix(unit(0), unit(5), 0.6))
    assert m.name == "alice"
    assert m.second_name is None and m.second_score is None


def test_empty_gallery():
    m = Gallery.from_people({}).match(unit(0))
    assert m.name is None and m.reason == "empty gallery"


def test_people_without_embeddings_are_skipped():
    g = Gallery.from_people({"alice": [unit(0)], "nobody": []})
    assert g.names == ["alice"]


def test_save_load_roundtrip(gallery, tmp_path):
    path = tmp_path / "gallery.npz"
    gallery.save(path)
    loaded = Gallery.load(path)
    assert loaded.names == gallery.names
    assert loaded.counts() == {"alice": 2, "bob": 1}
    np.testing.assert_allclose(loaded.embeddings, gallery.embeddings)
    assert loaded.match(unit(1)).name == "alice"
