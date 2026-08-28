from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class VectorCandidate:
    chunk_id: str
    score: float
    shard: int


def vector_enabled() -> bool:
    return _env_bool("MCP_1C_RAG_SUMMARY_VECTOR_ENABLED", False)


def vector_query_candidates(client: Any, prefix: str, query: str, *, limit: int = 100) -> tuple[list[VectorCandidate], dict[str, Any]]:
    """Return optional GitLab-hosted vector candidates.

    This is a Stage 3 consumer for coarse-quantized vector indexes stored next to
    configuration summary chunks.  It never builds or persists a local vector DB:
    the client reads ``vector-index/*`` files through the same GitLab/local raw
    access and bounded cache as lexical summary RAG.
    """
    if not vector_enabled():
        return [], {"enabled": False}
    manifest_path = f"{prefix}/vector-index/manifest.json"
    try:
        manifest = json.loads(client.read_text(manifest_path))
    except Exception as exc:
        return [], {"enabled": True, "available": False, "diagnostics": [{"code": "vector_index_missing", "message": str(exc)}]}

    dim = int(manifest.get("dim") or 0)
    if dim <= 0:
        return [], {"enabled": True, "available": False, "diagnostics": [{"code": "vector_index_invalid", "message": "Vector index manifest has no positive dim."}]}

    mode = os.environ.get("MCP_1C_RAG_SUMMARY_VECTOR_MODE", "hash").strip().lower()
    if mode != "hash":
        return [], {"enabled": True, "available": False, "diagnostics": [{"code": "query_embedding_provider_missing", "message": "Only hash query embeddings are currently built in; configure a provider before enabling production vector rerank."}]}
    query_vector = _hash_embedding(query, dim)

    files = manifest.get("files") or {}
    centroids_name = files.get("centroids") or "centroids.jsonl.gz"
    shard_pattern = files.get("vectorShardPattern") or "vectors-%03d.jsonl.gz"
    top_shards = int(os.environ.get("MCP_1C_RAG_SUMMARY_VECTOR_TOP_SHARDS", "3") or 3)
    shard_count = int(manifest.get("shardCount") or 0)

    centroids = _read_centroids(client.read_text(f"{prefix}/vector-index/{centroids_name}"))
    if not centroids and shard_count:
        centroids = [(idx, []) for idx in range(shard_count)]
    selected = [shard for shard, _ in sorted(((shard, _cosine(query_vector, vector)) for shard, vector in centroids if vector), key=lambda item: item[1], reverse=True)[:top_shards]]
    if not selected and shard_count:
        selected = list(range(min(top_shards, shard_count)))

    candidates: list[VectorCandidate] = []
    for shard in selected:
        try:
            data = client.read_text(f"{prefix}/vector-index/{shard_pattern % shard}")
        except Exception:
            continue
        for row in _iter_vector_rows(data):
            vector = row.get("vector")
            chunk_id = str(row.get("chunk_id") or row.get("id") or "")
            if not chunk_id or not isinstance(vector, list):
                continue
            score = _cosine(query_vector, [float(item) for item in vector])
            candidates.append(VectorCandidate(chunk_id=chunk_id, score=score, shard=shard))
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:limit], {"enabled": True, "available": True, "backend": "gitlab-hosted-coarse-vector", "selected_shards": selected, "manifest": {"schema": manifest.get("schema"), "dim": dim, "shardCount": shard_count, "embeddingModel": manifest.get("embeddingModel")}}


def _read_centroids(data: str) -> list[tuple[int, list[float]]]:
    stripped = data.strip()
    if not stripped:
        return []
    rows: list[Any]
    if stripped.startswith("["):
        rows = json.loads(stripped)
    else:
        rows = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        shard = row.get("shard") if "shard" in row else row.get("id")
        vector = row.get("vector") or row.get("centroid")
        if shard is None or not isinstance(vector, list):
            continue
        result.append((int(shard), [float(item) for item in vector]))
    return result


def _iter_vector_rows(data: str):
    for line in data.splitlines():
        if line.strip():
            yield json.loads(line)


def _hash_embedding(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    tokens = re.findall(r"[\wА-Яа-яЁё]+", text.lower(), flags=re.UNICODE)
    features: list[str] = []
    for token in tokens:
        features.append("tok:" + token)
        for size in (3, 4):
            if len(token) >= size:
                features.extend(f"ng{size}:" + token[pos : pos + size] for pos in range(len(token) - size + 1))
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    width = min(len(left), len(right))
    if width == 0:
        return 0.0
    return sum(left[i] * right[i] for i in range(width))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() not in {"0", "false", "no", "off", ""}
