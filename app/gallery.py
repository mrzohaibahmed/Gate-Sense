"""Face gallery: enrolled embeddings and matching.

Every embedding of every person is kept (not an average). A query is compared
with all of them by cosine similarity, each person scores their best match,
and the top person is accepted only if their score clears the threshold and
beats the second-best person by the margin. Otherwise the face is unknown.

Saved to ``data/gallery.npz`` (``app.config.GALLERY_PATH``). That file is
biometric data and is git-ignored. Later it moves to SQLite, then PostgreSQL.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app import config


@dataclass
class Match:
    name: str | None  # accepted person, or None for unknown
    best_name: str | None  # top-scoring person, even if rejected
    score: float  # top person's best cosine similarity
    second_name: str | None  # runner-up person (None if only one enrolled)
    second_score: float | None
    reason: str  # "match", "below threshold", "low margin" or "empty gallery"


def _normalize(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


class Gallery:
    def __init__(self, names: list[str], embeddings: np.ndarray, labels: np.ndarray):
        self.names = list(names)  # one entry per person
        self.embeddings = _normalize(np.asarray(embeddings, dtype=np.float32).reshape(-1, config.EMBEDDING_DIM))
        self.labels = np.asarray(labels, dtype=np.int64)  # person index for each embedding row

    @classmethod
    def from_people(cls, people: dict[str, list[np.ndarray]]) -> "Gallery":
        """Build from {name: [embedding, ...]}; people with no embeddings are skipped."""
        names, rows, labels = [], [], []
        for name, embs in people.items():
            if not embs:
                continue
            labels += [len(names)] * len(embs)
            names.append(name)
            rows += embs
        embeddings = np.stack(rows) if rows else np.empty((0, config.EMBEDDING_DIM), np.float32)
        return cls(names, embeddings, np.array(labels))

    def __len__(self) -> int:
        return len(self.names)

    def counts(self) -> dict[str, int]:
        """Number of stored embeddings per person."""
        return {name: int((self.labels == i).sum()) for i, name in enumerate(self.names)}

    def save(self, path: Path = config.GALLERY_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, names=np.array(self.names), embeddings=self.embeddings, labels=self.labels)

    @classmethod
    def load(cls, path: Path = config.GALLERY_PATH) -> "Gallery":
        with np.load(path, allow_pickle=False) as data:
            return cls(data["names"].tolist(), data["embeddings"], data["labels"])

    def person_scores(self, emb: np.ndarray) -> np.ndarray:
        """Best cosine similarity per person (index matches ``names``)."""
        sims = self.embeddings @ _normalize(np.asarray(emb, dtype=np.float32))
        best = np.full(len(self.names), -np.inf, dtype=np.float32)
        np.maximum.at(best, self.labels, sims)
        return best

    def match(
        self,
        emb: np.ndarray,
        threshold: float = config.MATCH_THRESHOLD,
        margin: float = config.MATCH_MARGIN,
    ) -> Match:
        if not self.names:
            return Match(None, None, 0.0, None, None, "empty gallery")
        scores = self.person_scores(emb)
        order = np.argsort(scores)[::-1]
        top = int(order[0])
        best_name, score = self.names[top], float(scores[top])
        second_name = second_score = None
        if len(order) > 1:
            second_name, second_score = self.names[int(order[1])], float(scores[order[1]])

        if score < threshold:
            reason = "below threshold"
        elif second_score is not None and score - second_score < margin:
            reason = "low margin"
        else:
            reason = "match"
        name = best_name if reason == "match" else None
        return Match(name, best_name, score, second_name, second_score, reason)
