from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .detect import detect_source_format
from .model import SourceInfo
from .parsers import navigation_for_object, normalize_query, parse_source
from .store import RagStore, json_dumps, row_to_dict

DEFAULT_RAG_DB = Path(os.environ.get("MCP_1C_RAG_DB", "build/rag/onec-rag.sqlite"))


class RagService:
    def __init__(self, db_path: str | Path = DEFAULT_RAG_DB):
        self.store = RagStore(db_path)
        self.store.init()

    @property
    def db_path(self) -> str:
        return str(self.store.db_path)

    def detect_source(self, path: str) -> dict[str, Any]:
        return {"path": path, "format": detect_source_format(path), "exists": Path(path).exists()}

    def add_source(self, name: str, path: str, kind: str = "standalone", format: str = "auto") -> dict[str, Any]:
        p = Path(path).resolve()
        fmt = detect_source_format(p) if format == "auto" else format
        commit = _git_commit(p)
        branch = _git_branch(p)
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO sources(name,path,kind,format,branch,commit_hash)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET path=excluded.path, kind=excluded.kind,
                  format=excluded.format, branch=excluded.branch, commit_hash=excluded.commit_hash
                """,
                (name, str(p), kind, fmt, branch, commit),
            )
            row = con.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
        return row_to_dict(row) | {"detected_format": fmt}

    def list_sources(self) -> list[dict[str, Any]]:
        with self.store.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM sources ORDER BY id")]

    def create_snapshot(self, name: str, base: str | None = None, extensions: list[str] | None = None, sources: list[str] | None = None) -> dict[str, Any]:
        layers: list[tuple[str, str]] = []
        if sources:
            layers = [(s, "source") for s in sources]
        else:
            if base:
                layers.append((base, "base"))
            for ext in extensions or []:
                layers.append((ext, "extension"))
        if not layers:
            raise ValueError("snapshot needs base/extensions or sources")
        with self.store.connect() as con:
            con.execute("INSERT INTO snapshots(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name,))
            snap = con.execute("SELECT * FROM snapshots WHERE name=?", (name,)).fetchone()
            snapshot_id = snap["id"]
            con.execute("DELETE FROM snapshot_layers WHERE snapshot_id=?", (snapshot_id,))
            result_layers = []
            for order, (source_name, role) in enumerate(layers, start=1):
                src = con.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
                if src is None:
                    raise ValueError(f"source not found: {source_name}")
                con.execute(
                    "INSERT INTO snapshot_layers(snapshot_id,source_id,layer_order,role) VALUES(?,?,?,?)",
                    (snapshot_id, src["id"], order, role),
                )
                result_layers.append({"source": source_name, "source_id": src["id"], "order": order, "role": role, "format": src["format"]})
        return {"snapshot_id": snapshot_id, "name": name, "layers": result_layers}

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self.store.connect() as con:
            snaps = [dict(r) for r in con.execute("SELECT * FROM snapshots ORDER BY id")]
            for snap in snaps:
                snap["layers"] = [
                    dict(r)
                    for r in con.execute(
                        """
                        SELECT sl.layer_order, sl.role, s.name, s.path, s.kind, s.format, s.commit_hash
                        FROM snapshot_layers sl JOIN sources s ON s.id=sl.source_id
                        WHERE sl.snapshot_id=? ORDER BY sl.layer_order
                        """,
                        (snap["id"],),
                    )
                ]
            return snaps

    def build_index(self, snapshot: str | int | None = None, source: str | None = None) -> dict[str, Any]:
        with self.store.connect() as con:
            if source:
                source_rows = [con.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()]
                snapshot_id = None
            elif snapshot is not None:
                snap = _snapshot_row(con, snapshot)
                if snap is None:
                    raise ValueError(f"snapshot not found: {snapshot}")
                snapshot_id = snap["id"]
                source_rows = [
                    r
                    for r in con.execute(
                        """
                        SELECT s.* FROM snapshot_layers sl JOIN sources s ON s.id=sl.source_id
                        WHERE sl.snapshot_id=? ORDER BY sl.layer_order
                        """,
                        (snapshot_id,),
                    )
                ]
            else:
                snapshot_id = None
                source_rows = [r for r in con.execute("SELECT * FROM sources ORDER BY id")]
            source_rows = [r for r in source_rows if r is not None]
            counts = {"sources": 0, "objects": 0, "attributes": 0, "table_parts": 0, "forms": 0, "form_elements": 0, "chunks": 0}
            for src in source_rows:
                info = SourceInfo(src["name"], Path(src["path"]), src["kind"], src["format"], src["repo_url"], src["branch"], src["commit_hash"])
                parsed = parse_source(info)
                self.store.clear_index_for_source(con, src["id"])
                counts["sources"] += 1
                object_ids: dict[str, int] = {}
                table_part_ids: dict[tuple[str, str], int] = {}
                form_ids: dict[tuple[str, str], int] = {}
                for obj in parsed.objects:
                    cur = con.execute(
                        """
                        INSERT INTO metadata_objects(source_id, full_name, kind, name, synonym, source_path, source_format, is_hierarchical, owner_types_json, raw_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (src["id"], obj.full_name, obj.kind, obj.name, obj.synonym, obj.source_path, obj.source_format, int(bool(obj.is_hierarchical)) if obj.is_hierarchical is not None else None, json_dumps(obj.owner_types), json_dumps(obj.raw)),
                    )
                    oid = cur.lastrowid
                    object_ids[obj.full_name] = oid
                    counts["objects"] += 1
                    nav = navigation_for_object(obj.full_name, obj.kind)
                    if nav:
                        con.execute("INSERT INTO navigation_links(object_id, kind, url) VALUES(?,?,?)", (oid, nav[0], nav[1]))
                    chunk_text = f"{obj.full_name} {obj.synonym or ''} {obj.kind} navigation {nav[1] if nav else ''}"
                    cid = _insert_chunk(con, snapshot_id, oid, "object", chunk_text, obj.source_path, {"source": src["name"]})
                    con.execute("INSERT INTO search_fts(text, object_full_name, kind, source_path, chunk_id) VALUES(?,?,?,?,?)", (chunk_text, obj.full_name, obj.kind, obj.source_path, cid))
                    counts["chunks"] += 1
                    if obj.synonym:
                        _insert_business_term(con, snapshot_id, obj.synonym, oid, 0.7, "object synonym")
                for attr in parsed.attributes:
                    oid = object_ids.get(attr.object_full_name)
                    if not oid:
                        continue
                    con.execute("INSERT INTO attributes(object_id,name,type_name,synonym,required,source_path,raw_json) VALUES(?,?,?,?,?,?,?)", (oid, attr.name, attr.type_name, attr.synonym, attr.required, attr.source_path, json_dumps(attr.raw)))
                    counts["attributes"] += 1
                for tp in parsed.table_parts:
                    oid = object_ids.get(tp.object_full_name)
                    if not oid:
                        continue
                    cur = con.execute("INSERT INTO table_parts(object_id,name,synonym,source_path,raw_json) VALUES(?,?,?,?,?)", (oid, tp.name, tp.synonym, tp.source_path, json_dumps(tp.raw)))
                    table_part_ids[(tp.object_full_name, tp.name)] = cur.lastrowid
                    counts["table_parts"] += 1
                for col in parsed.table_part_columns:
                    tpid = table_part_ids.get((col.object_full_name, col.table_part))
                    if tpid:
                        con.execute("INSERT INTO table_part_columns(table_part_id,name,type_name,synonym,required,source_path,raw_json) VALUES(?,?,?,?,?,?,?)", (tpid, col.name, col.type_name, col.synonym, col.required, col.source_path, json_dumps(col.raw)))
                for form in parsed.forms:
                    oid = object_ids.get(form.object_full_name)
                    if not oid:
                        continue
                    cur = con.execute("INSERT INTO forms(object_id,name,kind,source_path,raw_json) VALUES(?,?,?,?,?)", (oid, form.name, form.kind, form.source_path, json_dumps(form.raw)))
                    form_ids[(form.object_full_name, form.name)] = cur.lastrowid
                    counts["forms"] += 1
                for elem in parsed.form_elements:
                    fid = form_ids.get((elem.object_full_name, elem.form_name))
                    if not fid:
                        continue
                    con.execute("INSERT INTO form_elements(form_id,name,element_type,title,data_path,command_name,parent_name,source_path,raw_json) VALUES(?,?,?,?,?,?,?,?,?)", (fid, elem.name, elem.element_type, elem.title, elem.data_path, elem.command_name, elem.parent_name, elem.source_path, json_dumps(elem.raw)))
                    counts["form_elements"] += 1
        return {"db_path": self.db_path, **counts}

    def lookup_object(self, object: str, snapshot: str | int | None = None) -> dict[str, Any]:
        with self.store.connect() as con:
            source_filter = ""
            params: list[Any] = []
            if snapshot is not None:
                snap = _snapshot_row(con, snapshot)
                if snap is None:
                    raise ValueError(f"snapshot not found: {snapshot}")
                source_filter = "AND mo.source_id IN (SELECT source_id FROM snapshot_layers WHERE snapshot_id=?)"
                params.append(snap["id"])
            rows = [dict(r) for r in con.execute(f"SELECT mo.*, s.name AS source_name, s.kind AS source_kind FROM metadata_objects mo JOIN sources s ON s.id=mo.source_id WHERE mo.full_name LIKE ? {source_filter} ORDER BY mo.source_id", [object, *params])]
            if not rows and "." not in object:
                rows = [dict(r) for r in con.execute(f"SELECT mo.*, s.name AS source_name, s.kind AS source_kind FROM metadata_objects mo JOIN sources s ON s.id=mo.source_id WHERE mo.name LIKE ? {source_filter} ORDER BY mo.source_id", [object, *params])]
            for row in rows:
                oid = row["id"]
                row["navigation"] = [dict(r) for r in con.execute("SELECT kind,url FROM navigation_links WHERE object_id=?", (oid,))]
                row["attributes"] = [dict(r) for r in con.execute("SELECT name,type_name,synonym,required FROM attributes WHERE object_id=? ORDER BY id", (oid,))]
                row["table_parts"] = [dict(r) for r in con.execute("SELECT id,name,synonym FROM table_parts WHERE object_id=? ORDER BY id", (oid,))]
                for tp in row["table_parts"]:
                    tp["columns"] = [dict(r) for r in con.execute("SELECT name,type_name,synonym,required FROM table_part_columns WHERE table_part_id=? ORDER BY id", (tp["id"],))]
                row["forms"] = [dict(r) for r in con.execute("SELECT id,name,kind,source_path FROM forms WHERE object_id=? ORDER BY id", (oid,))]
            return {"query": object, "snapshot": snapshot, "matches": rows}

    def query(self, text: str, snapshot: str | int | None = None, limit: int = 10) -> dict[str, Any]:
        q = normalize_query(text)
        with self.store.connect() as con:
            term_rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT bt.*, mo.full_name, mo.kind, s.name AS source_name
                    FROM business_terms bt
                    LEFT JOIN metadata_objects mo ON mo.id=bt.object_id
                    LEFT JOIN sources s ON s.id=mo.source_id
                    WHERE bt.normalized_term LIKE ?
                    ORDER BY bt.confidence DESC LIMIT ?
                    """,
                    (f"%{q}%", limit),
                )
            ]
            fts_query = " ".join(q.split()) or q
            try:
                fts_rows = [dict(r) for r in con.execute("SELECT text, object_full_name, kind, source_path, bm25(search_fts) AS score FROM search_fts WHERE search_fts MATCH ? ORDER BY score LIMIT ?", (fts_query, limit))]
            except Exception:
                fts_rows = []
            if len(fts_rows) < limit:
                seen = {(r.get("object_full_name"), r.get("source_path")) for r in fts_rows}
                for row in con.execute("SELECT text, object_full_name, kind, source_path FROM search_fts WHERE lower(text) LIKE ? LIMIT ?", (f"%{q}%", limit)):
                    item = dict(row)
                    key = (item.get("object_full_name"), item.get("source_path"))
                    if key not in seen:
                        fts_rows.append(item)
                        seen.add(key)
                    if len(fts_rows) >= limit:
                        break
        return {"query": text, "terms": term_rows, "fts": fts_rows}


def _git_commit(path: Path) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _git_branch(path: Path) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _snapshot_row(con, snapshot: str | int):
    if isinstance(snapshot, int) or str(snapshot).isdigit():
        return con.execute("SELECT * FROM snapshots WHERE id=?", (int(snapshot),)).fetchone()
    return con.execute("SELECT * FROM snapshots WHERE name=?", (snapshot,)).fetchone()


def _insert_chunk(con, snapshot_id, object_id, chunk_type, text, source_path, metadata) -> int:
    cur = con.execute("INSERT INTO rag_chunks(snapshot_id,object_id,chunk_type,text,source_path,metadata_json) VALUES(?,?,?,?,?,?)", (snapshot_id, object_id, chunk_type, text, source_path, json_dumps(metadata)))
    return cur.lastrowid


def _insert_business_term(con, snapshot_id, term, object_id, confidence, evidence) -> None:
    con.execute("INSERT INTO business_terms(snapshot_id,term,normalized_term,object_id,confidence,evidence) VALUES(?,?,?,?,?,?)", (snapshot_id, term, normalize_query(term), object_id, confidence, evidence))
