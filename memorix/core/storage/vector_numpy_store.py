"""Numpy-based VectorStore compatibility layer used when faiss is unavailable."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np

from .vector_numpy import NumpyVectorBackend


class NumpyCompatVectorStore:
    """Compatibility shim that mimics the migrated VectorStore interface."""

    def __init__(self, *, dimension: int, data_dir: Path):
        self.dimension = int(max(1, dimension))
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._backend = NumpyVectorBackend(dimension=self.dimension, data_dir=self.data_dir)
        self.min_train_threshold = 0
        self._metadata_path = self.data_dir / "vectors_metadata.pkl"

    @property
    def size(self) -> int:
        return self.num_vectors

    @property
    def num_vectors(self) -> int:
        return int(self._backend.num_vectors)

    def add(self, vectors: np.ndarray, ids: Sequence[str]) -> int:
        return self._backend.add(vectors=vectors, ids=ids)

    def search(self, query_vector: np.ndarray, k: int = 10) -> Tuple[List[str], List[float]]:
        return self._backend.search(query=query_vector, k=k)

    def delete(self, ids: Sequence[str]) -> int:
        return self._backend.delete(ids=ids)

    def save(self) -> None:
        self._backend.save()
        payload = {"dimension": self.dimension, "backend": "numpy", "num_vectors": self.num_vectors}
        with self._metadata_path.open("wb") as handle:
            pickle.dump(payload, handle)

    def load(self) -> None:
        self._backend.load()

    def clear(self) -> None:
        self._backend.clear()
        if self._metadata_path.exists():
            self._metadata_path.unlink()

    def has_data(self) -> bool:
        path = self.data_dir / "numpy_vectors.npz"
        return path.exists()
