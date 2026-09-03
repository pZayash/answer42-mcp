from __future__ import annotations

import collections
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .parsers import navigation_for_object, normalize_query
from .summary_vector import vector_enabled, vector_query_candidates, vector_score_texts

DEFAULT_PROJECT = "platform42/configurations/summary"
DEFAULT_REF = "beta"
DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "openclaw" / "configurations-rag"
DEFAULT_GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
SUMMARY_PREFIX = "summary:"
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_][A-Za-zА-Яа-яЁё0-9_.\-]*", re.UNICODE)
KIND_PRIORITY = {"config-summary": 0, "metadata-object": 1, "bsl-module": 2, "form": 3, "bsl-method": 4}
LAYER_PATHS = {"configuration": "{config}/configuration/rag/chunks", "fresh": "{config}/fresh/ai/rag/chunks"}
SEARCH_INDEX_PATHS = {"configuration": "{config}/configuration/rag/search-index", "fresh": "{config}/fresh/ai/rag/search-index"}
EXTENSION_LAYER_ALIASES = {"fresh": "fresh"}
CONFIG_ALIASES = {
    "accounting30": "bp",
    "accountingbase30": "bpb",
    "accountingcorp30": "bpc",
    "accountingkz30": "bpkz",
    "accountingnpo": "bnko",
    "arautomation20": "ka",
    "bnko": "bnko",
    "bp": "bp",
    "bpb": "bpb",
    "bpc": "bpc",
    "bpkz": "bpkz",
    "hrm30": "zup",
    "ka": "ka",
    "retail23": "rt",
    "retail30": "rtl",
    "rt": "rt",
    "rtl": "rtl",
    "smallbusiness30": "unf",
    "smallbusinesskz3": "unfkz",
    "trade110": "ut",
    "unf": "unf",
    "unfkz": "unfkz",
    "ut": "ut",
    "zup": "zup",
    "бнко": "bnko",
    "бухгалтерия базовая": "bpb",
    "бухгалтерия базовая редакция 3.0": "bpb",
    "бухгалтерия для казахстана": "bpkz",
    "бухгалтерия для казахстана редакция 3.0": "bpkz",
    "бухгалтерия для некоммерческой организации": "bnko",
    "бухгалтерия предприятия": "bp",
    "бухгалтерия предприятия базовая": "bpb",
    "бухгалтерия предприятия корп": "bpc",
    "бухгалтерия предприятия корп редакция 3.0": "bpc",
    "бухгалтерия предприятия корпоративная": "bpc",
    "бухгалтерия предприятия редакция 3.0": "bp",
    "бухгалтерия предприятия, редакция 3.0": "bp",
    "зарплата и управление персоналом": "zup",
    "зарплата и управление персоналом редакция 3": "zup",
    "зуп": "zup",
    "комплексная автоматизация": "ka",
    "комплексная автоматизация 2": "ka",
    "розница": "rt",
    "розница 2": "rt",
    "розница 2.3": "rt",
    "розница 3": "rtl",
    "розница 3.0": "rtl",
    "у н ф": "unf",
    "унф": "unf",
    "унф для казахстана": "unfkz",
    "управление нашей фирмой": "unf",
    "управление нашей фирмой для казахстана": "unfkz",
    "управление торговлей": "ut",
    "управление торговлей 11": "ut",
}


@dataclass(order=True)
class SummaryHit:
    score: float
    chunk_id: str = field(compare=False)
    layer: str = field(compare=False)
    kind: str = field(compare=False)
    title: str = field(compare=False)
    source: str = field(compare=False)
    file: str = field(compare=False)
    text: str = field(compare=False)
    metadata: dict[str, Any] = field(compare=False)
    exact_score: float = field(default=0.0, compare=False)
    token_score: float = field(default=0.0, compare=False)
    meta_score: float = field(default=0.0, compare=False)


@dataclass
class CandidateScore:
    chunk_id: str
    score: float
    matched_terms: int = 0
    field_mask: int = 0


@dataclass
class ChunkMeta:
    chunk_id: str
    kind: str
    file: str
    line: int
    title: str = ""
    source_path: str = ""

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> ChunkMeta:
        return cls(
            chunk_id=str(item.get("chunk_id") or item.get("id") or ""),
            kind=str(item.get("kind") or item.get("type") or ""),
            file=str(item.get("file") or ""),
            line=int(item.get("line") or 0),
            title=str(item.get("title") or ""),
            source_path=str(item.get("sourcePath") or item.get("source_path") or ""),
        )


class SummaryRawClient:
    def read_bytes(self, path: str) -> bytes:
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        data = self.read_bytes(path)
        if path.endswith(".gz"):
            data = gzip.decompress(data)
        return data.decode("utf-8")


class LocalSummaryClient(SummaryRawClient):
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def read_bytes(self, path: str) -> bytes:
        return (self.root / path).read_bytes()


class GitLabSummaryClient(SummaryRawClient):
    def __init__(self, *, project: str, ref: str, cache_dir: str | Path, cache_mb: int, offline: bool = False, gitlab_url: str = DEFAULT_GITLAB_URL):
        self.project = project
        self.ref = ref
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_mb = cache_mb
        self.offline = offline
        self.gitlab_url = gitlab_url.rstrip("/")
        self.project_enc = urllib.parse.quote(project, safe="")
        self.token = _gitlab_token(self.gitlab_url)

    def read_bytes(self, path: str) -> bytes:
        cache_path = self.cache_path(path)
        if cache_path.exists():
            os.utime(cache_path, None)
            data = cache_path.read_bytes()
            if cache_path.suffix == ".gz" and not path.endswith(".gz"):
                return gzip.decompress(data)
            return data
        raw_cache_path = self.cache_dir / _cache_key(self.project, self.ref, path, "none")
        if raw_cache_path.exists():
            os.utime(raw_cache_path, None)
            data = raw_cache_path.read_bytes()
            self.write_cache(path, data)
            return data
        if self.offline:
            raise FileNotFoundError(f"not in cache: {path}")
        url = f"{self.gitlab_url}/api/v4/projects/{self.project_enc}/repository/files/{urllib.parse.quote(path, safe='')}/raw?ref={urllib.parse.quote(self.ref, safe='')}"
        headers = {"User-Agent": "answer42-summary-rag/1.0"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"GitLab HTTP {exc.code} for {path}: {detail}") from exc
        self.write_cache(path, data)
        return data

    def cache_path(self, path: str) -> Path:
        return self.cache_dir / _cache_key(self.project, self.ref, path, "gzip")

    def write_cache(self, path: str, data: bytes) -> None:
        cache_path = self.cache_path(path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        stored = gzip.compress(data, compresslevel=6) if path.endswith(".jsonl") else data
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_bytes(stored)
        tmp.replace(cache_path)
        self.prune_cache()

    def prune_cache(self) -> None:
        if self.cache_mb <= 0 or not self.cache_dir.exists():
            return
        total = 0
        files: list[tuple[float, Path, int]] = []
        for path in self.cache_dir.rglob("*"):
            if path.is_file():
                stat = path.stat()
                total += stat.st_size
                files.append((stat.st_mtime, path, stat.st_size))
        max_bytes = self.cache_mb * 1024 * 1024
        for _, path, size in sorted(files):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass


class ConfigurationSummaryBackend:
    def __init__(self) -> None:
        self.enabled = _env_bool("MCP_1C_RAG_SUMMARY_ENABLED", True)
        self.project = os.environ.get("MCP_1C_RAG_SUMMARY_PROJECT", DEFAULT_PROJECT)
        self.ref = os.environ.get("MCP_1C_RAG_SUMMARY_REF", DEFAULT_REF)
        self.backend = os.environ.get("MCP_1C_RAG_SUMMARY_BACKEND", "gitlab-stream")
        self.local_root = os.environ.get("MCP_1C_RAG_SUMMARY_LOCAL_ROOT", "")
        self.cache_dir = Path(os.environ.get("MCP_1C_RAG_SUMMARY_CACHE_DIR", str(DEFAULT_CACHE))).expanduser()
        self.cache_mb = int(os.environ.get("MCP_1C_RAG_SUMMARY_CACHE_MB", "500") or 500)
        self.offline = _env_bool("MCP_1C_RAG_SUMMARY_OFFLINE", False)
        self.prefer_compact = _env_bool("MCP_1C_RAG_SUMMARY_PREFER_COMPACT", True)
        self.external_snapshots: dict[str, dict[str, Any]] = {}

    def client(self) -> SummaryRawClient:
        if self.backend == "local-stream" or self.local_root:
            root = self.local_root or str(Path(os.environ.get("OPENCLAW_WORKSPACE", "/home/openclaw/.openclaw/workspace")) / "configurations-summary")
            return LocalSummaryClient(root)
        return GitLabSummaryClient(project=self.project, ref=self.ref, cache_dir=self.cache_dir, cache_mb=self.cache_mb, offline=self.offline)

    def is_summary_snapshot(self, snapshot: str | int | None) -> bool:
        return isinstance(snapshot, str) and snapshot.startswith(SUMMARY_PREFIX)

    def snapshot_name(self, config_code: str, layer: str, identifier: str = "", version: str = "") -> str:
        parts = ["summary", config_code, layer]
        if identifier:
            parts.append(identifier)
        if version:
            parts.append(version)
        return ":".join(_safe_part(part) for part in parts)

    def create_snapshot(
        self,
        *,
        config_code: str,
        layer: str = "configuration",
        identifier: str = "",
        version: str = "",
        name: str = "",
        manifest: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        extra_layers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        manifest = manifest or self.load_manifest(config_code, layer)
        snapshot_layers = [{"layer": layer, "role": "base" if layer == "configuration" else layer, "manifest": manifest}]
        for extra in extra_layers or []:
            extra_layer = str(extra.get("layer") or "").strip()
            extra_manifest = extra.get("manifest")
            if extra_layer and isinstance(extra_manifest, dict):
                snapshot_layers.append({"layer": extra_layer, "role": str(extra.get("role") or extra_layer), "manifest": extra_manifest})
        layer_key = "+".join(str(item.get("layer") or "").strip() for item in snapshot_layers if str(item.get("layer") or "").strip()) or layer
        effective_name = name or self.snapshot_name(config_code, layer_key, identifier or str(manifest.get("identifier") or ""), version or str(manifest.get("version") or ""))
        public_layers = []
        for order, layer_info in enumerate(snapshot_layers, start=1):
            layer_name = str(layer_info["layer"])
            layer_manifest = layer_info.get("manifest") or {}
            public_layers.append({
                "layer_order": order,
                "role": layer_info.get("role") or layer_name,
                "name": f"summary:{config_code}:{layer_name}",
                "path": f"{self.project}@{self.ref}:{config_code}/{layer_name}",
                "kind": "configuration" if layer_name == "configuration" else "extension",
                "format": "configuration_summary",
                "commit_hash": "",
                "identifier": layer_manifest.get("identifier"),
                "version": layer_manifest.get("version"),
                "chunks": layer_manifest.get("chunkCount"),
            })
        item = {
            "id": effective_name,
            "name": effective_name,
            "backend": "configuration-summary",
            "config_code": config_code,
            "layer": layer,
            "summary_layers": snapshot_layers,
            "identifier": identifier or manifest.get("identifier") or "",
            "version": version or manifest.get("version") or "",
            "project": self.project,
            "ref": self.ref,
            "manifest": manifest,
            "warnings": warnings or [],
            "layers": public_layers,
        }
        self.external_snapshots[effective_name] = item
        return item

    def resolve_snapshot(self, snapshot: str | int | None) -> dict[str, Any] | None:
        if not self.is_summary_snapshot(snapshot):
            return None
        assert isinstance(snapshot, str)
        if snapshot in self.external_snapshots:
            return self.external_snapshots[snapshot]
        parts = snapshot.split(":")
        if len(parts) < 3:
            return None
        config_code = parts[1]
        layer_parts = [part for part in (parts[2] or "configuration").split("+") if part]
        layer = layer_parts[0] if layer_parts else "configuration"
        try:
            manifest = self.load_manifest(config_code, layer)
            extra_layers = [{"layer": extra_layer, "role": "extension", "manifest": self.load_manifest(config_code, extra_layer)} for extra_layer in layer_parts[1:]]
        except Exception:
            return None
        return self.create_snapshot(config_code=config_code, layer=layer, identifier=str(manifest.get("identifier") or ""), version=str(manifest.get("version") or ""), name=snapshot, manifest=manifest, extra_layers=extra_layers)

    def list_snapshots(self) -> list[dict[str, Any]]:
        return list(self.external_snapshots.values())

    def snapshot_has_index(self, snapshot: str | int | None) -> bool:
        info = self.resolve_snapshot(snapshot)
        if not info:
            return False
        try:
            self.load_manifest(str(info["config_code"]), str(info["layer"]))
            return True
        except Exception:
            return False

    def load_manifest(self, config_code: str, layer: str = "configuration") -> dict[str, Any]:
        return json.loads(self.client().read_text(f"{_rag_prefix(config_code, layer)}/manifest.json"))

    def find_configuration(self, config_name: str, config_version: str = "") -> dict[str, Any] | None:
        if not self.enabled:
            return None
        for config_code in dict.fromkeys(_config_candidates(config_name)):
            try:
                manifest = self.load_manifest(config_code, "configuration")
            except Exception:
                continue
            warnings: list[str] = []
            version = str(manifest.get("version") or "")
            if config_version and version and config_version != version:
                warnings.append(f"Summary RAG version mismatch: runtime {config_version}, summary {version}.")
            return {"config_code": config_code, "layer": "configuration", "identifier": str(manifest.get("identifier") or ""), "version": version, "manifest": manifest, "warnings": warnings}
        return None

    def find_extension(self, config_code: str, extension_name: str, extension_version: str = "") -> dict[str, Any] | None:
        if not self.enabled:
            return None
        for layer in dict.fromkeys(_extension_layer_candidates(extension_name)):
            try:
                manifest = self.load_manifest(config_code, layer)
            except Exception:
                continue
            warnings: list[str] = []
            version = str(manifest.get("extensionVersion") or "")
            if extension_version and version and extension_version != version:
                warnings.append(f"Summary RAG extension {extension_name!r} version mismatch: runtime {extension_version}, summary {version}.")
            return {"config_code": config_code, "layer": layer, "role": "extension", "name": extension_name, "version": version, "manifest": manifest, "warnings": warnings}
        return None

    def available_configurations(self) -> list[dict[str, Any]]:
        rows = []
        for code in ("bp",):
            try:
                manifest = self.load_manifest(code)
            except Exception:
                continue
            rows.append({"name": f"summary:{code}:configuration", "path": f"{self.project}@{self.ref}:{code}/configuration/rag", "kind": "configuration", "format": "configuration_summary", "backend": "configuration-summary", "config_code": code, "identifier": manifest.get("identifier"), "version": manifest.get("version"), "indexed": {"objects": 0, "chunks": manifest.get("chunkCount") or 0}})
        return rows

    def build_index(self, snapshot: str | int | None = None) -> dict[str, Any]:
        info = self.resolve_snapshot(snapshot)
        if not info:
            raise ValueError(f"summary snapshot not found: {snapshot}")
        manifest = info.get("manifest") or {}
        return {"backend": "configuration-summary", "db_path": None, "sources": 1, "objects": 0, "attributes": 0, "table_parts": 0, "forms": 0, "form_elements": 0, "data_composition_fields": 0, "help_pages": 0, "chunks": int(manifest.get("chunkCount") or 0), "message": "Summary RAG uses GitLab/local compact indexes; no local SQLite build is required."}

    def query(self, text: str, snapshot: str | int | None = None, limit: int = 10) -> dict[str, Any]:
        info = self.resolve_snapshot(snapshot)
        if not info:
            raise ValueError(f"summary snapshot not found: {snapshot}")
        layers = self._snapshot_layer_names(info)
        config_code = str(info["config_code"])
        hits, meta = self.search(config_code, layers, text, limit=limit, kinds="auto")
        vector_meta_by_layer: dict[str, Any] = {}
        use_vector = _should_use_vector("auto", text)
        for layer in layers:
            if use_vector:
                layer_hits = [hit for hit in hits if hit.layer == layer]
                vector_candidates, layer_vector_meta = self.vector_text_candidates(config_code, layer, text, layer_hits, limit=max(limit * 5, 50))
                if len(layer_hits) < limit:
                    extra_candidates, extra_meta = self.vector_candidates(config_code, layer, text, limit=max(limit * 5, 50))
                    seen = {candidate.chunk_id for candidate in vector_candidates}
                    vector_candidates.extend(candidate for candidate in extra_candidates if candidate.chunk_id not in seen)
                    layer_vector_meta["fallback_scan"] = extra_meta
            else:
                vector_candidates = []
                layer_vector_meta = {"enabled": vector_enabled(), "available": None, "skipped": "non_code_query"}
            vector_meta_by_layer[layer] = layer_vector_meta
            if vector_candidates:
                hits = self._merge_vector_candidates(config_code, layer, text, hits, vector_candidates, limit)
        summary = self._summary_info(info, meta)
        summary["vector"] = {"layers": vector_meta_by_layer}
        return {"query": text, "snapshot": snapshot, "backend": "configuration-summary", "summary": summary, "terms": [], "fts": [_hit_to_rag_row(hit) for hit in hits], "semantic": []}

    def lookup_object(self, object: str, snapshot: str | int | None = None) -> dict[str, Any]:
        info = self.resolve_snapshot(snapshot)
        if not info:
            raise ValueError(f"summary snapshot not found: {snapshot}")
        wanted_type, wanted_name = _split_object_name(object)
        query = " ".join(part for part in [wanted_type, wanted_name or object] if part)
        hits, meta = self.search(str(info["config_code"]), self._snapshot_layer_names(info), query, limit=80, kinds="metadata-object")
        matches = []
        seen = set()
        for hit in hits:
            row = hit.metadata
            mtype = str(row.get("metadataType") or "")
            mname = str(row.get("metadataName") or "")
            if wanted_type and mtype and normalize_query(mtype) != normalize_query(wanted_type):
                continue
            if wanted_name and mname and normalize_query(mname) != normalize_query(wanted_name):
                continue
            hit_info = self._info_for_hit_layer(info, row)
            obj = metadata_object_row_from_chunk(row, source_name=f"summary:{info['config_code']}")
            obj = self._enrich_metadata_object_from_summary_json(obj, row, hit_info)
            if obj["full_name"] not in seen:
                matches.append(obj)
                seen.add(obj["full_name"])
        return {"query": object, "snapshot": snapshot, "backend": "configuration-summary", "summary": self._summary_info(info, meta), "matches": matches}

    def lookup_form_element_types(self, snapshot: str | int | None = None, limit: int = 10_000, object_full_name: str | None = None) -> dict[str, Any]:
        info = self.resolve_snapshot(snapshot)
        if not info:
            raise ValueError(f"summary snapshot not found: {snapshot}")
        if object_full_name:
            return self._lookup_object_form_element_types(info, snapshot, object_full_name, limit=limit)
        hits, meta = self.search(str(info["config_code"]), self._snapshot_layer_names(info), "форма", limit=limit, kinds="form")
        items = []
        for hit in hits:
            items.extend(self._form_element_rows(hit.metadata, self._info_for_hit_layer(info, hit.metadata)))
        # Metadata-object chunks are intentionally not scanned wholesale here: on
        # large configurations that would read megabytes just for synthetic hints.
        # Object attributes are still available through lookup_object(), which is
        # what dynamic_list_metadata_available_fields uses for a concrete object.
        return {"snapshot": snapshot, "backend": "configuration-summary", "summary": self._summary_info(info, meta), "count": len(items), "items": items}

    def _lookup_object_form_element_types(self, info: dict[str, Any], snapshot: str | int | None, object_full_name: str, *, limit: int) -> dict[str, Any]:
        metadata_type, metadata_name = _split_object_name(object_full_name)
        query = " ".join(part for part in (metadata_type, metadata_name, "форма") if part)
        hits, meta = self.search(str(info["config_code"]), self._snapshot_layer_names(info), query, limit=min(max(limit, 20), 120), kinds="form")
        items = []
        seen = set()
        for hit in hits:
            row = hit.metadata
            row_type = str(row.get("metadataType") or "")
            row_name = str(row.get("metadataName") or "")
            if metadata_type and row_type and normalize_query(row_type) != normalize_query(metadata_type):
                continue
            if metadata_name and row_name and normalize_query(row_name) != normalize_query(metadata_name):
                continue
            for item in self._form_element_rows(row, self._info_for_hit_layer(info, row)):
                key = (
                    item.get("object_full_name"),
                    item.get("form_name"),
                    item.get("name"),
                    item.get("data_path"),
                    item.get("command_name"),
                )
                if key not in seen:
                    items.append(item)
                    seen.add(key)
        obj_lookup = self.lookup_object(object_full_name, snapshot)
        for obj in obj_lookup.get("matches") or []:
            if normalize_query(str(obj.get("full_name") or "")) != normalize_query(object_full_name):
                continue
            source_path = str(obj.get("source_path") or "")
            object_kind = str(obj.get("kind") or metadata_type)
            for attr in obj.get("attributes") or []:
                name = str(attr.get("name") or "")
                if not name:
                    continue
                key = (object_full_name, "<auto>", name, name, None)
                if key in seen:
                    continue
                items.append({
                    "object_full_name": object_full_name,
                    "object_kind": object_kind,
                    "form_name": "<auto>",
                    "name": name,
                    "element_type": "AutoGeneratedField",
                    "title": attr.get("synonym") or name,
                    "data_path": name,
                    "command_name": None,
                    "parent_name": None,
                    "source_path": attr.get("source_path") or source_path,
                    "attribute_type": attr.get("type_name"),
                    "attribute_role": attr.get("role") or "attribute",
                    "dcs_field_type": None,
                })
                seen.add(key)
            for table_part in obj.get("table_parts") or []:
                table_name = str(table_part.get("name") or "")
                for col in table_part.get("columns") or []:
                    col_name = str(col.get("name") or "")
                    if not table_name or not col_name:
                        continue
                    data_path = f"{table_name}.{col_name}"
                    key = (object_full_name, "<auto>", data_path, data_path, None)
                    if key in seen:
                        continue
                    items.append({
                        "object_full_name": object_full_name,
                        "object_kind": object_kind,
                        "form_name": "<auto>",
                        "name": data_path,
                        "element_type": "AutoGeneratedTablePartField",
                        "title": col.get("synonym") or col_name,
                        "data_path": data_path,
                        "command_name": None,
                        "parent_name": table_name,
                        "source_path": col.get("source_path") or source_path,
                        "attribute_type": col.get("type_name"),
                        "attribute_role": "table_part_column",
                        "dcs_field_type": None,
                    })
                    seen.add(key)
            break
        return {"snapshot": snapshot, "backend": "configuration-summary", "summary": self._summary_info(info, meta), "object_full_name": object_full_name, "count": len(items), "items": items}


    def _snapshot_layer_names(self, info: dict[str, Any]) -> list[str]:
        layers = [str(item.get("layer") or "") for item in info.get("summary_layers") or [] if str(item.get("layer") or "")]
        return layers or [str(info.get("layer") or "configuration")]

    def _info_for_hit_layer(self, info: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        layer = str(row.get("_layer") or info.get("layer") or "configuration")
        result = dict(info)
        result["layer"] = layer
        for item in info.get("summary_layers") or []:
            if str(item.get("layer") or "") == layer and isinstance(item.get("manifest"), dict):
                result["manifest"] = item["manifest"]
                break
        return result

    def _read_summary_json(self, row: dict[str, Any], info: dict[str, Any]) -> Any | None:
        summary_path = str(row.get("summaryPath") or "")
        if not summary_path:
            return None
        if summary_path.startswith("/") or ".." in Path(summary_path).parts:
            return None
        base = f"{info['config_code']}/{info['layer']}"
        candidates = [f"{base}/{summary_path}"]
        if str(info.get("layer")) == "configuration":
            candidates.append(f"{info['config_code']}/configuration/{summary_path}")
        for path in dict.fromkeys(candidates):
            try:
                return json.loads(self.client().read_text(path))
            except Exception:
                continue
        return None

    def _enrich_metadata_object_from_summary_json(self, obj: dict[str, Any], row: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
        data = self._read_summary_json(row, info)
        if data is None:
            return obj
        candidates = _iter_dicts(data)
        wanted_type = normalize_query(str(row.get("metadataType") or obj.get("kind") or ""))
        wanted_name = normalize_query(str(row.get("metadataName") or obj.get("name") or ""))
        selected = None
        for item in candidates:
            item_type = normalize_query(str(item.get("metadataType") or item.get("type") or item.get("kind") or ""))
            item_name = normalize_query(str(item.get("metadataName") or item.get("name") or item.get("Name") or ""))
            if item_name == wanted_name and (not wanted_type or not item_type or item_type == wanted_type):
                selected = item
                break
        if selected is None and isinstance(data, dict):
            selected = data
        if not isinstance(selected, dict):
            return obj
        synonym = selected.get("synonym") or selected.get("Synonym") or selected.get("presentation")
        if synonym and not obj.get("synonym"):
            obj["synonym"] = str(synonym)
        attrs = _structured_attributes(selected, obj["full_name"], str(obj.get("source_path") or ""))
        if attrs:
            obj["attributes"] = attrs
        table_parts = _structured_table_parts(selected, obj["full_name"], str(obj.get("source_path") or ""))
        if table_parts:
            obj["table_parts"] = table_parts
        return obj

    def _form_element_rows(self, row: dict[str, Any], info: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._read_summary_json(row, info)
        if isinstance(data, dict):
            structured = structured_form_element_rows(data, row)
            if structured:
                return structured
        return form_element_rows_from_chunk(row)

    def search(self, config_code: str, layers: list[str], query: str, *, limit: int, kinds: str) -> tuple[list[SummaryHit], dict[str, Any]]:
        if self.prefer_compact and query.strip():
            rows, meta = self._search_compact(config_code, layers, query, limit=limit, kinds=kinds)
            if rows or meta.get("compact_index"):
                return rows[:limit], meta
        return self._search_stream(config_code, layers, query, limit=limit, kinds=kinds), {"backend": "stream", "compact_index": False}

    def _search_stream(self, config_code: str, layers: list[str], query: str, *, limit: int, kinds: str) -> list[SummaryHit]:
        client = self.client()
        q_tokens = normalize_tokens(query)
        hits: dict[str, SummaryHit] = {}
        for layer in layers:
            files = select_files(load_chunks_index(client, config_code, layer), kinds, query)
            chunks_path = _chunks_path(config_code, layer)
            for info in files:
                fname = info.get("file")
                if not fname or not str(fname).endswith((".jsonl", ".jsonl.gz")):
                    continue
                for row in iter_jsonl(client, f"{chunks_path}/{fname}"):
                    row["_layer"] = layer
                    hit = score_row(row, query, q_tokens)
                    if hit is None:
                        continue
                    prev = hits.get(hit.chunk_id)
                    if prev is None or hit.score > prev.score:
                        hits[hit.chunk_id] = hit
        return sorted(hits.values(), reverse=True)[:limit]

    def _search_compact(self, config_code: str, layers: list[str], query: str, *, limit: int, kinds: str) -> tuple[list[SummaryHit], dict[str, Any]]:
        kind_filter = parse_kind_filter(kinds, query)
        if kind_filter is not None and "bsl-method" not in kind_filter:
            return [], {"backend": "stream", "compact_index": False, "reason": "stream_is_faster_for_small_kinds"}
        client = self.client()
        q_tokens = normalize_tokens(query)
        hits: dict[str, SummaryHit] = {}
        used_compact = False
        for layer in layers:
            prefix = _search_index_prefix(config_code, layer)
            try:
                manifest = json.loads(client.read_text(f"{prefix}/manifest.json"))
            except Exception:
                continue
            used_compact = True
            candidates = compact_candidates(client, prefix, manifest, query, candidate_limit=max(limit * 5, 100))
            metas = resolve_candidate_metas(client, prefix, manifest, candidates)
            cand_by_id = {cand.chunk_id: cand for cand in candidates}
            by_file: dict[str, list[tuple[int, CandidateScore, ChunkMeta]]] = {}
            for meta in metas:
                if kind_filter is not None and meta.kind not in kind_filter:
                    continue
                cand = cand_by_id.get(meta.chunk_id)
                if cand:
                    by_file.setdefault(meta.file, []).append((meta.line, cand, meta))
            chunks_path = _chunks_path(config_code, layer)
            for file_rel, entries in by_file.items():
                wanted = {line: (cand, meta) for line, cand, meta in entries}
                try:
                    for row in iter_jsonl_wanted_lines(client, f"{chunks_path}/{file_rel}", set(wanted)):
                        line_no = int(row.get("_rag_line") or 0)
                        cand, meta = wanted[line_no]
                        row["_layer"] = layer
                        hit = score_row(row, query, q_tokens)
                        if hit is None:
                            hit = SummaryHit(cand.score, meta.chunk_id, layer, meta.kind, meta.title, meta.source_path, file_rel, chunk_text(row), row, token_score=cand.score)
                        else:
                            hit.token_score += cand.score * 0.5
                            hit.score = hit.exact_score + hit.token_score + hit.meta_score
                        prev = hits.get(hit.chunk_id)
                        if prev is None or hit.score > prev.score:
                            hits[hit.chunk_id] = hit
                except Exception:
                    continue
        return sorted(hits.values(), reverse=True)[:limit], {"backend": "compact" if used_compact else "stream", "compact_index": used_compact}

    def vector_candidates(self, config_code: str, layer: str, query: str, *, limit: int) -> tuple[list[Any], dict[str, Any]]:
        return vector_query_candidates(self.client(), _rag_prefix(config_code, layer), query, limit=limit)

    def vector_text_candidates(self, config_code: str, layer: str, query: str, hits: list[SummaryHit], *, limit: int) -> tuple[list[Any], dict[str, Any]]:
        return vector_score_texts(self.client(), _rag_prefix(config_code, layer), query, [(hit.chunk_id, hit.text) for hit in hits], limit=limit)

    def _merge_vector_candidates(self, config_code: str, layer: str, query: str, hits: list[SummaryHit], candidates: list[Any], limit: int) -> list[SummaryHit]:
        existing = {hit.chunk_id: hit for hit in hits}
        missing = [candidate for candidate in candidates if candidate.chunk_id not in existing]
        if missing:
            prefix = _search_index_prefix(config_code, layer)
            manifest = json.loads(self.client().read_text(f"{prefix}/manifest.json"))
            metas = resolve_candidate_metas(self.client(), prefix, manifest, [CandidateScore(candidate.chunk_id, candidate.score) for candidate in missing])
            by_file: dict[str, list[tuple[int, Any, ChunkMeta]]] = {}
            cand_by_id = {candidate.chunk_id: candidate for candidate in missing}
            for meta in metas:
                candidate = cand_by_id.get(meta.chunk_id)
                if candidate is not None:
                    by_file.setdefault(meta.file, []).append((meta.line, candidate, meta))
            q_tokens = normalize_tokens(query)
            for file_rel, rows in by_file.items():
                wanted = {line: (candidate, meta) for line, candidate, meta in rows}
                try:
                    for row in iter_jsonl_wanted_lines(self.client(), f"{_chunks_path(config_code, layer)}/{file_rel}", set(wanted)):
                        line_no = int(row.get("_rag_line") or 0)
                        candidate, meta = wanted[line_no]
                        row["_layer"] = layer
                        hit = score_row(row, query, q_tokens) or SummaryHit(candidate.score, meta.chunk_id, layer, meta.kind, meta.title, meta.source_path, file_rel, chunk_text(row), row)
                        hit.meta_score += max(0.0, float(candidate.score)) * 5.0
                        hit.score = hit.exact_score + hit.token_score + hit.meta_score
                        existing[hit.chunk_id] = hit
                except Exception:
                    continue
        for candidate in candidates:
            if candidate.chunk_id in existing:
                hit = existing[candidate.chunk_id]
                hit.meta_score += max(0.0, float(candidate.score)) * 5.0
                hit.score = hit.exact_score + hit.token_score + hit.meta_score
        return sorted(existing.values(), reverse=True)[:limit]

    def _summary_info(self, info: dict[str, Any], search_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        manifest = info.get("manifest") or {}
        layers = []
        for item in info.get("summary_layers") or []:
            item_manifest = item.get("manifest") or {}
            layers.append({"layer": item.get("layer"), "role": item.get("role"), "chunkCount": item_manifest.get("chunkCount"), "version": item_manifest.get("version")})
        return {"project": self.project, "ref": self.ref, "configCode": info.get("config_code"), "layer": info.get("layer"), "layers": layers, "identifier": info.get("identifier") or manifest.get("identifier"), "version": info.get("version") or manifest.get("version"), "chunkCount": sum(int(item.get("chunkCount") or 0) for item in layers) if layers else manifest.get("chunkCount"), "search": search_meta or {}}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _safe_part(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё_.+-]+", "_", str(value)).strip("_") or "unknown"


def _cache_key(project: str, ref: str, path: str, compression: str) -> Path:
    digest = hashlib.sha1(f"{project}\0{ref}\0{path}".encode()).hexdigest()
    suffix = Path(path).suffix or ".raw"
    if compression == "gzip" and path.endswith(".jsonl"):
        suffix += ".gz"
    return Path(project.replace("/", "__")) / ref / f"{digest}{suffix}"


def _gitlab_token(gitlab_url: str) -> str | None:
    for name in ("GITLAB_TOKEN", "GL_TOKEN", "GITLAB_PRIVATE_TOKEN", "PRIVATE_TOKEN"):
        if os.environ.get(name):
            return os.environ[name].strip()
    host = urllib.parse.urlparse(gitlab_url).netloc or "gitlab.com"
    try:
        proc = subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
        for line in proc.stdout.splitlines():
            if line.startswith("password=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def normalize_tokens(text: str) -> list[str]:
    return [m.group(0) for m in WORD_RE.finditer(unicodedata.normalize("NFKC", text).lower()) if len(m.group(0)) >= 2]


def _config_candidates(config_name: str) -> list[str]:
    norm = normalize_query(config_name)
    keys = [norm, norm.replace(" ", ""), config_name.lower()]
    return [CONFIG_ALIASES[key] for key in keys if key in CONFIG_ALIASES] or [config_name.lower()]


def _extension_layer_candidates(extension_name: str) -> list[str]:
    norm = normalize_query(extension_name)
    keys = [norm, norm.replace(" ", ""), extension_name.casefold()]
    aliases = [EXTENSION_LAYER_ALIASES[key] for key in keys if key in EXTENSION_LAYER_ALIASES]
    safe = re.sub(r"[^0-9a-zа-яё_.-]+", "_", extension_name.casefold()).strip("_")
    return aliases or ([safe] if safe else [])


def _rag_prefix(config_code: str, layer: str) -> str:
    return f"{config_code}/configuration/rag" if layer == "configuration" else f"{config_code}/{layer}/ai/rag"


def _chunks_path(config_code: str, layer: str) -> str:
    return LAYER_PATHS.get(layer, "{config}/" + layer + "/ai/rag/chunks").format(config=config_code)


def _search_index_prefix(config_code: str, layer: str) -> str:
    return SEARCH_INDEX_PATHS.get(layer, "{config}/" + layer + "/ai/rag/search-index").format(config=config_code)


def load_chunks_index(client: SummaryRawClient, config_code: str, layer: str) -> list[dict[str, Any]]:
    chunks_path = _chunks_path(config_code, layer)
    for path in (f"{chunks_path}/_index.json", f"{_rag_prefix(config_code, layer)}/chunks-manifest.json"):
        try:
            data = json.loads(client.read_text(path))
        except FileNotFoundError:
            continue
        files = data.get("files") or []
        if files:
            return files
    raise FileNotFoundError(f"Cannot load chunks index for {config_code}/{layer}")


def select_files(files: list[dict[str, Any]], kinds: str, query: str) -> list[dict[str, Any]]:
    wanted = parse_kind_filter(kinds, query)
    return sorted([item for item in files if wanted is None or item.get("kind") in wanted], key=lambda item: KIND_PRIORITY.get(str(item.get("kind")), 99))


def parse_kind_filter(kinds: str, query: str) -> set[str] | None:
    if kinds == "all":
        return None
    if kinds == "auto":
        wanted = {"config-summary", "metadata-object", "bsl-module", "form"}
        q = query.lower()
        markers = ("процедура", "функция", "метод", "запрос", "выборка", "общиймодуль", "модуль", "&наклиенте", "&насервере")
        if any(marker in q for marker in markers) or re.search(r"[A-Za-zА-Яа-яЁё_]+\(", query):
            wanted.add("bsl-method")
        return wanted
    return {item.strip() for item in kinds.split(",") if item.strip()}


def _should_use_vector(kinds: str, query: str) -> bool:
    """Use coarse Stage 3 vectors only for code/method-like searches."""
    kind_filter = parse_kind_filter(kinds, query)
    if kind_filter is not None:
        return "bsl-method" in kind_filter
    q = query.lower()
    markers = ("процедура", "функция", "метод", "запрос", "выборка", "общиймодуль", "модуль", "&наклиенте", "&насервере")
    return any(marker in q for marker in markers) or re.search(r"[A-Za-zА-Яа-яЁё_]+\(", query) is not None


def iter_jsonl(client: SummaryRawClient, path: str) -> Iterator[dict[str, Any]]:
    for line_no, line in enumerate(client.read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            row["_rag_file"] = Path(path).name
            row["_rag_line"] = line_no
            yield row


def iter_jsonl_wanted_lines(client: SummaryRawClient, path: str, wanted_lines: set[int]) -> Iterator[dict[str, Any]]:
    """Yield only selected JSONL line numbers from a chunk file."""
    if not wanted_lines:
        return
    remaining = set(wanted_lines)
    file_name = Path(path).name
    for line_no, line in enumerate(client.read_text(path).splitlines(), start=1):
        if line_no not in remaining:
            continue
        remaining.discard(line_no)
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            row["_rag_file"] = file_name
            row["_rag_line"] = line_no
            yield row
        if not remaining:
            break


def chunk_text(row: dict[str, Any]) -> str:
    for key in ("text", "content", "summary", "page_content", "chunk", "body", "document"):
        value = row.get(key)
        if value:
            return "\n".join(str(item) for item in value) if isinstance(value, list) else str(value)
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def title_of(row: dict[str, Any]) -> str:
    for key in ("title", "name", "fullName", "objectName", "method", "module", "chunk_id", "id"):
        if row.get(key):
            return str(row[key])
    return ""


def source_of(row: dict[str, Any]) -> str:
    for key in ("sourcePath", "summaryPath", "path", "file"):
        if row.get(key):
            return str(row[key])
    return ""


def score_row(row: dict[str, Any], query: str, q_tokens: list[str]) -> SummaryHit | None:
    text = chunk_text(row)
    title = title_of(row)
    source = source_of(row)
    kind = str(row.get("kind") or row.get("type") or "?")
    chunk_id = str(row.get("chunk_id") or row.get("id") or row.get("uid") or f"{row.get('_rag_file')}:{row.get('_rag_line')}")
    hay_meta = json.dumps(row, ensure_ascii=False, sort_keys=True)
    hay = f"{title}\n{source}\n{text}\n{hay_meta}".lower()
    q_lower = query.lower().strip()
    exact = 0.0
    if q_lower and q_lower in hay:
        exact = 20.0 + min(30.0, hay.count(q_lower) * 4.0)
    token_score = 0.0
    if q_tokens:
        # Keep per-row scoring cheap on large metadata files: generated summary
        # text/title/source carry the searchable terms, while hay_meta remains
        # available for exact substring fallback below.  Tokenising full metadata
        # JSON for every row dominates Answer42 metadata lookups on BP.
        token_hay = f"{title} {source} {text}".lower()
        counts = collections.Counter(match.group(0) for match in WORD_RE.finditer(token_hay))
        matched = 0
        for token in set(q_tokens):
            if token in counts:
                matched += 1
                token_score += 2.0 + min(4.0, math.log1p(counts[token]))
            elif len(token) >= 5 and token in hay:
                matched += 1
                token_score += 1.5
        if matched:
            token_score += 10.0 * matched / len(set(q_tokens))
    meta_score = 0.0
    for token in set(q_tokens):
        if token in title.lower():
            meta_score += 8.0
        if token in source.lower():
            meta_score += 4.0
    if kind in {"metadata-object", "config-summary"}:
        meta_score += 2.0
    score = exact + token_score + meta_score
    if score <= 0:
        return None
    return SummaryHit(score, chunk_id, str(row.get("_layer", "")), kind, title, source, str(row.get("_rag_file", "")), text, row, exact, token_score, meta_score)


def _field_boost(fields: int) -> float:
    return 1.0 + (2.0 if fields & 1 else 0.0) + (1.5 if fields & 2 else 0.0) + (0.5 if fields & 8 else 0.0)


def _term_shard(term: str, count: int) -> int:
    return int(hashlib.md5(term.encode("utf-8")).hexdigest(), 16) % count


def compact_candidates(client: SummaryRawClient, prefix: str, manifest: dict[str, Any], query: str, *, candidate_limit: int) -> list[CandidateScore]:
    terms = set(normalize_tokens(query))
    if not terms:
        return []
    shard_count = int(manifest.get("termShardCount") or 16)
    chunk_count = int(manifest.get("chunkCount") or 1)
    pattern = manifest.get("files", {}).get("termShardPattern") or "terms-%03d.jsonl.gz"
    scores: dict[str, CandidateScore] = {}
    by_shard: dict[int, set[str]] = {}
    for term in terms:
        by_shard.setdefault(_term_shard(term, shard_count), set()).add(term)
    for shard, wanted in by_shard.items():
        try:
            data = client.read_text(f"{prefix}/{pattern % shard}")
        except Exception:
            continue
        prefixes = {term: '{"term": ' + json.dumps(term, ensure_ascii=False) for term in wanted}
        remaining = set(wanted)
        for line in data.splitlines():
            if not remaining:
                break
            stripped = line.lstrip()
            matched = next((term for term in tuple(remaining) if stripped.startswith(prefixes[term])), None)
            if matched is None:
                continue
            entry = json.loads(stripped)
            df = int(entry.get("df") or 0)
            if df <= 0:
                continue
            idf = math.log((chunk_count + 1) / (df + 1))
            for posting in entry.get("postings") or []:
                chunk_id = str(posting[0])
                freq = int(posting[1])
                fields = int(posting[2])
                item = scores.setdefault(chunk_id, CandidateScore(chunk_id, 0.0, 0, 0))
                item.score += idf * (1.0 + math.log1p(freq)) * _field_boost(fields)
                item.matched_terms += 1
                item.field_mask |= fields
            remaining.discard(matched)
    return sorted(scores.values(), key=lambda item: (-item.matched_terms, -item.score))[:candidate_limit]


def resolve_candidate_metas(client: SummaryRawClient, prefix: str, manifest: dict[str, Any], candidates: list[CandidateScore]) -> list[ChunkMeta]:
    files = manifest.get("files", {})
    pattern = files.get("chunkMapShardPattern")
    shard_count = int(manifest.get("chunkMapShardCount") or 0)
    wanted_order = [candidate.chunk_id for candidate in candidates]
    found: dict[str, ChunkMeta] = {}
    if pattern and shard_count > 0:
        by_shard: dict[int, set[str]] = {}
        for chunk_id in wanted_order:
            by_shard.setdefault(_term_shard(chunk_id, shard_count), set()).add(chunk_id)
        for shard, wanted in by_shard.items():
            try:
                found.update(_read_wanted_metas(client.read_text(f"{prefix}/{pattern % shard}"), wanted))
            except Exception:
                continue
    if not found:
        try:
            found.update(_read_wanted_metas(client.read_text(f"{prefix}/chunks-map.jsonl.gz"), set(wanted_order)))
        except Exception:
            pass
    return [found[chunk_id] for chunk_id in wanted_order if chunk_id in found]


def _read_wanted_metas(data: str, wanted: set[str]) -> dict[str, ChunkMeta]:
    remaining = set(wanted)
    found = {}
    prefix = '{"chunk_id": '
    for line in data.splitlines():
        if not remaining:
            break
        stripped = line.lstrip()
        if not stripped.startswith(prefix):
            continue
        comma = stripped.find(",", len(prefix))
        if comma < 0:
            continue
        try:
            chunk_id = json.loads(stripped[len(prefix):comma])
        except json.JSONDecodeError:
            continue
        if chunk_id not in remaining:
            continue
        meta = ChunkMeta.from_dict(json.loads(stripped))
        found[meta.chunk_id] = meta
        remaining.discard(meta.chunk_id)
    return found


def _hit_to_rag_row(hit: SummaryHit) -> dict[str, Any]:
    return {"text": hit.text, "object_full_name": _full_name_from_chunk(hit.metadata), "kind": _kind_from_chunk(hit.metadata), "source_path": hit.source, "score": -hit.score, "chunk_id": hit.chunk_id, "source_name": "configuration-summary", "metadata": hit.metadata}


def _split_object_name(value: str) -> tuple[str, str]:
    value = value.strip()
    if "." in value:
        left, right = value.split(".", 1)
        return left.strip(), right.strip()
    parts = value.split(maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", value)


def _full_name_from_chunk(row: dict[str, Any]) -> str:
    metadata_type = str(row.get("metadataType") or "")
    metadata_name = str(row.get("metadataName") or "")
    if metadata_type and metadata_name:
        return f"{metadata_type}.{metadata_name}"
    return str(row.get("id") or row.get("title") or "")


def _kind_from_chunk(row: dict[str, Any]) -> str:
    return str(row.get("metadataType") or row.get("kind") or "")


def metadata_object_row_from_chunk(row: dict[str, Any], *, source_name: str) -> dict[str, Any]:
    full_name = _full_name_from_chunk(row)
    metadata_type, metadata_name = _split_object_name(full_name)
    text = str(row.get("text") or "")
    source_path = str(row.get("sourcePath") or "")
    nav = navigation_for_object(full_name, metadata_type)
    return {
        "id": row.get("id") or full_name,
        "source_id": source_name,
        "full_name": full_name,
        "kind": metadata_type,
        "name": metadata_name,
        "synonym": _extract_after_label(text, "Синоним"),
        "source_path": source_path,
        "source_format": "configuration_summary",
        "is_hierarchical": None,
        "owner_types_json": "[]",
        "raw_json": json.dumps(row, ensure_ascii=False, sort_keys=True),
        "source_name": source_name,
        "source_kind": "configuration",
        "navigation": [{"kind": nav[0], "url": nav[1]}] if nav else [],
        "attributes": _parse_attributes(text, full_name, source_path),
        "table_parts": _parse_table_parts(text, full_name, source_path),
        "forms": [],
        "data_composition_fields": [],
    }


def _extract_after_label(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}:\s*([^\.]+)", text)
    return match.group(1).strip() if match else None


def _section(text: str, label: str, stop_labels: tuple[str, ...]) -> str:
    match = re.search(rf"{re.escape(label)}:\s*(.*)", text)
    if not match:
        return ""
    value = match.group(1)
    stops = [value.find(f". {stop}:") for stop in stop_labels]
    stops = [pos for pos in stops if pos >= 0]
    if stops:
        value = value[: min(stops)]
    return value.strip().rstrip(".")


def _split_comma_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


def _split_name_type(value: str) -> tuple[str, str | None]:
    if ":" in value:
        name, type_name = value.split(":", 1)
        return name.strip(), type_name.strip() or None
    return value.strip(), None


def _parse_attributes(text: str, object_full_name: str, source_path: str) -> list[dict[str, Any]]:
    result = []
    for role, label in (("attribute", "Реквизиты"), ("common_attribute", "Общие реквизиты")):
        for item in _split_comma_items(_section(text, label, ("Общие реквизиты", "Табличные части", "Формы", "Команды"))):
            name, type_name = _split_name_type(item)
            if name:
                result.append({"name": name, "type_name": type_name, "synonym": None, "required": None, "role": role, "source_path": source_path, "object_full_name": object_full_name})
    return result


def _parse_table_parts(text: str, object_full_name: str, source_path: str) -> list[dict[str, Any]]:
    result = []
    for match in re.finditer(r"([^,()]+)\s*\(колонки:\s*([^)]*)\)", _section(text, "Табличные части", ("Формы", "Команды", "Путь исходника"))):
        tp_name = match.group(1).strip()
        columns = []
        for item in _split_comma_items(match.group(2)):
            name, type_name = _split_name_type(item)
            if name:
                columns.append({"name": name, "type_name": type_name, "synonym": None, "required": None, "source_path": source_path, "object_full_name": object_full_name})
        result.append({"id": f"{object_full_name}.{tp_name}", "name": tp_name, "synonym": None, "columns": columns})
    return result


def form_element_rows_from_chunk(row: dict[str, Any]) -> list[dict[str, Any]]:
    full_name = _full_name_from_chunk(row)
    object_kind, _ = _split_object_name(full_name)
    form_name = str(row.get("formName") or "")
    source_path = str(row.get("sourcePath") or "")
    text = str(row.get("text") or "")
    items = []
    for item in _split_comma_items(_section(text, "Реквизиты формы", ("Элементы", "Команды"))):
        name, type_name = _split_name_type(item)
        if name:
            items.append({"object_full_name": full_name, "object_kind": object_kind, "form_name": form_name, "name": name, "element_type": "FormAttribute", "title": name, "data_path": name, "command_name": None, "parent_name": None, "source_path": source_path, "attribute_type": type_name, "attribute_role": "form_attribute", "dcs_field_type": None})
    for item in _split_comma_items(_section(text, "Элементы", ("Команды",))):
        name, data_path = [part.strip() for part in item.split(" -> ", 1)] if " -> " in item else (item.strip(), None)
        if name:
            items.append({"object_full_name": full_name, "object_kind": object_kind, "form_name": form_name, "name": name, "element_type": "FormElement", "title": name, "data_path": data_path, "command_name": None, "parent_name": None, "source_path": source_path, "attribute_type": None, "attribute_role": "form_element", "dcs_field_type": None})
    return items


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                result.extend(_iter_dicts(nested))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                result.extend(_iter_dicts(item))
    return result


def _structured_attributes(data: dict[str, Any], object_full_name: str, source_path: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, role in (("attributes", "attribute"), ("requisites", "attribute"), ("commonAttributes", "common_attribute")):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("Name") or "")
            if not name:
                continue
            result.append({
                "name": name,
                "type_name": _structured_type(item),
                "synonym": item.get("synonym") or item.get("title"),
                "required": item.get("required"),
                "role": str(item.get("role") or role),
                "source_path": str(item.get("sourcePath") or source_path),
                "object_full_name": object_full_name,
            })
    return result


def _structured_table_parts(data: dict[str, Any], object_full_name: str, source_path: str) -> list[dict[str, Any]]:
    value = data.get("tableParts") or data.get("tabularSections") or data.get("table_parts")
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("Name") or "")
        if not name:
            continue
        columns = []
        for col in item.get("columns") or item.get("attributes") or []:
            if isinstance(col, dict) and (col.get("name") or col.get("Name")):
                columns.append({
                    "name": str(col.get("name") or col.get("Name")),
                    "type_name": _structured_type(col),
                    "synonym": col.get("synonym") or col.get("title"),
                    "required": col.get("required"),
                    "source_path": str(col.get("sourcePath") or source_path),
                    "object_full_name": object_full_name,
                })
        result.append({"id": f"{object_full_name}.{name}", "name": name, "synonym": item.get("synonym") or item.get("title"), "columns": columns})
    return result


def _structured_type(item: dict[str, Any]) -> str | None:
    value = item.get("type") or item.get("typeName") or item.get("type_name") or item.get("types")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    if isinstance(value, dict):
        return ", ".join(str(v) for v in value.values() if isinstance(v, str) and v) or None
    return str(value) if value else None


def structured_form_element_rows(data: dict[str, Any], fallback_row: dict[str, Any]) -> list[dict[str, Any]]:
    full_name = _full_name_from_chunk(fallback_row)
    object_kind, _ = _split_object_name(full_name)
    form_name = str(fallback_row.get("formName") or data.get("formName") or data.get("name") or "")
    source_path = str(fallback_row.get("sourcePath") or data.get("sourcePath") or "")
    values = []
    for key in ("elements", "items", "fields"):
        if isinstance(data.get(key), list):
            values.extend(data[key])
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("Name") or item.get("id") or "")
        if not name:
            continue
        result.append({
            "object_full_name": full_name,
            "object_kind": object_kind,
            "form_name": form_name,
            "name": name,
            "element_type": item.get("type") or item.get("elementType") or "FormElement",
            "title": item.get("title") or item.get("caption") or name,
            "data_path": item.get("dataPath") or item.get("data_path"),
            "command_name": item.get("commandName") or item.get("command_name"),
            "parent_name": item.get("parentName") or item.get("parent_name"),
            "source_path": source_path,
            "attribute_type": _structured_type(item),
            "attribute_role": item.get("role") or "form_element",
            "dcs_field_type": None,
        })
    return result
