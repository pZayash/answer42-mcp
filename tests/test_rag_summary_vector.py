from __future__ import annotations

import gzip
import json
from pathlib import Path

from mcp_1c.rag.summary_vector import vector_query_candidates


class Client:
    def __init__(self, root: Path):
        self.root = root

    def read_bytes(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def read_text(self, path: str) -> str:
        data = self.read_bytes(path)
        if path.endswith(".gz"):
            data = gzip.decompress(data)
        return data.decode("utf-8")


def _gz_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_gitlab_hosted_vector_candidates(monkeypatch, tmp_path: Path):
    prefix = tmp_path / "bp" / "configuration" / "rag" / "vector-index"
    prefix.mkdir(parents=True)
    (prefix / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "platform42-config-summary-vector-index-v1",
                "embeddingModel": "answer42-local-hash-v1",
                "dim": 4,
                "shardCount": 1,
                "files": {"centroids": "centroids.jsonl.gz", "vectorShardPattern": "vectors-%03d.jsonl.gz"},
            }
        ),
        encoding="utf-8",
    )
    _gz_jsonl(prefix / "centroids.jsonl.gz", [{"shard": 0, "vector": [1, 0, 0, 0]}])
    _gz_jsonl(prefix / "vectors-000.jsonl.gz", [{"chunk_id": "c1", "vector": [1, 0, 0, 0]}, {"chunk_id": "c2", "vector": [-1, 0, 0, 0]}])
    monkeypatch.setenv("MCP_1C_RAG_SUMMARY_VECTOR_ENABLED", "1")

    rows, meta = vector_query_candidates(Client(tmp_path), "bp/configuration/rag", "номенклатура", limit=2)

    assert meta["available"] is True
    assert len(rows) == 2
    assert rows[0].score >= rows[1].score
