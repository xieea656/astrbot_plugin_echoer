"""Numpy fallback vector backend."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np


class NumpyVectorBackend:
    """Simple in-memory cosine index persisted as npz for environments without faiss."""

    def __init__(self, *, dimension: int, data_dir: Path):
        self.dimension = int(dimension)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._vectors: Dict[str, np.ndarray] = {}

    @property
    def size(self) -> int:
        return len(self._vectors)

    @property
    def num_vectors(self) -> int:
        return len(self._vectors)

    def add(self, vectors: np.ndarray, ids: Sequence[str]) -> int:
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.shape[1] != self.dimension:
            raise ValueError(f"dimension mismatch: {arr.shape[1]} != {self.dimension}")

        count = 0
        for idx, key in enumerate(ids):
            if key in self._vectors:
                continue
            vec = arr[idx]
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            self._vectors[str(key)] = vec.astype(np.float32)
            count += 1
        return count

    def search(self, query: np.ndarray, k: int = 10) -> Tuple[List[str], List[float]]:
        if not self._vectors:
            return [], []
        q = np.asarray(query, dtype=np.float32)
        if q.ndim == 2:
            q = q[0]
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        scored = []
        for key, vec in self._vectors.items():
            scored.append((key, float(np.dot(q, vec))))
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[: max(1, int(k))]
        return [x[0] for x in top], [x[1] for x in top]

    def delete(self, ids: Sequence[str]) -> int:
        n = 0
        for key in ids:
            if self._vectors.pop(str(key), None) is not None:
                n += 1
        return n

    def save(self) -> None:
        path = self.data_dir / "numpy_vectors.npz"
        keys = list(self._vectors.keys())
        if not keys:
            np.savez(path, keys=np.array([], dtype=str), vectors=np.empty((0, self.dimension), dtype=np.float32))
            return
        mat = np.vstack([self._vectors[k] for k in keys])
        np.savez(path, keys=np.array(keys, dtype=str), vectors=mat)

    def load(self) -> None:
        path = self.data_dir / "numpy_vectors.npz"
        if not path.exists():
            return
        data = np.load(path, allow_pickle=False)
        keys = data["keys"]
        vectors = data["vectors"]
        self._vectors.clear()
        for idx, key in enumerate(keys.tolist()):
            self._vectors[str(key)] = np.asarray(vectors[idx], dtype=np.float32)

    def clear(self) -> None:
        self._vectors.clear()
        path = self.data_dir / "numpy_vectors.npz"
        if path.exists():
            path.unlink()


def deterministic_vector(text: str, dimension: int) -> np.ndarray:
    """Utility for tests/fallback embedding."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(int(dimension), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec
