from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .detect import detect_source_format, discover_source_roots
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

    def discover_sources(self, root: str, recursive: bool = True, max_depth: int = 4) -> list[dict[str, Any]]:
        return discover_source_roots(root, recursive=recursive, max_depth=max_depth)

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

    def add_sources_from_directory(self, root: str, prefix: str = "", recursive: bool = True, max_depth: int = 4, watch: bool = False) -> dict[str, Any]:
        discovered = self.discover_sources(root, recursive=recursive, max_depth=max_depth)
        registered: list[dict[str, Any]] = []
        for item in discovered:
            source_path = Path(item["path"])
            name = prefix + _safe_source_name(source_path.name)
            if not name:
                name = prefix + ("Source" + str(len(registered) + 1))
            original_name = name
            index = 2
            while any(src["name"] == name for src in self.list_sources()):
                existing = next((src for src in self.list_sources() if src["name"] == name), None)
                if existing and Path(existing["path"]).resolve() == source_path.resolve():
                    break
                name = f"{original_name}_{index}"
                index += 1
            registered.append(self.add_source(name=name, path=str(source_path), kind=item.get("kind") or "configuration", format=item.get("format") or "auto"))
        if watch:
            self.add_watch_root(root, recursive=recursive, max_depth=max_depth, prefix=prefix)
        return {"root": str(Path(root).expanduser().resolve()), "watch": watch, "discovered_count": len(discovered), "registered_count": len(registered), "discovered": discovered, "registered": registered}

    def add_watch_root(self, path: str, recursive: bool = True, max_depth: int = 4, prefix: str = "") -> dict[str, Any]:
        p = Path(path).expanduser().resolve()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO source_roots(path,recursive,max_depth,prefix)
                VALUES(?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET recursive=excluded.recursive,
                  max_depth=excluded.max_depth, prefix=excluded.prefix, updated_at=CURRENT_TIMESTAMP
                """,
                (str(p), int(bool(recursive)), int(max_depth), prefix),
            )
            row = con.execute("SELECT * FROM source_roots WHERE path=?", (str(p),)).fetchone()
        return row_to_dict(row)

    def list_watch_roots(self) -> list[dict[str, Any]]:
        with self.store.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM source_roots ORDER BY id")]

    def sync_watch_roots(self) -> dict[str, Any]:
        roots = self.list_watch_roots()
        results = []
        for root in roots:
            results.append(self.add_sources_from_directory(root["path"], prefix=root.get("prefix") or "", recursive=bool(root.get("recursive")), max_depth=int(root.get("max_depth") or 4), watch=False))
        return {"roots_count": len(roots), "roots": roots, "results": results}

    def available_configurations(self, sync: bool = True) -> dict[str, Any]:
        sync_result = self.sync_watch_roots() if sync else None
        sources = self.list_sources()
        snapshots = self.list_snapshots()
        indexed = self.indexed_source_stats()
        indexed_by_source_id = {item["source_id"]: item for item in indexed}
        configs = []
        extensions = []
        other = []
        for src in sources:
            item = dict(src)
            item["indexed"] = indexed_by_source_id.get(src["id"], {"objects": 0, "chunks": 0})
            kind = str(src.get("kind") or "")
            if kind in {"base", "configuration"}:
                configs.append(item)
            elif kind == "extension":
                extensions.append(item)
            else:
                other.append(item)
        return {"rag_enabled": True, "db_path": self.db_path, "watch_roots": self.list_watch_roots(), "sync": sync_result, "configurations": configs, "extensions": extensions, "other_sources": other, "snapshots": snapshots}

    def indexed_source_stats(self) -> list[dict[str, Any]]:
        with self.store.connect() as con:
            return [
                dict(r)
                for r in con.execute(
                    """
                    SELECT s.id AS source_id, s.name AS source_name,
                           COUNT(DISTINCT mo.id) AS objects,
                           COUNT(DISTINCT rc.id) AS chunks
                    FROM sources s
                    LEFT JOIN metadata_objects mo ON mo.source_id=s.id
                    LEFT JOIN rag_chunks rc ON rc.object_id=mo.id
                    GROUP BY s.id, s.name ORDER BY s.id
                    """
                )
            ]

    def snapshot_has_index(self, snapshot: str | int | None) -> bool:
        with self.store.connect() as con:
            if snapshot is None:
                row = con.execute("SELECT 1 FROM metadata_objects LIMIT 1").fetchone()
                return row is not None
            snap = _snapshot_row(con, snapshot)
            if snap is None:
                return False
            row = con.execute(
                """
                SELECT 1
                FROM metadata_objects mo
                WHERE mo.source_id IN (SELECT source_id FROM snapshot_layers WHERE snapshot_id=?)
                LIMIT 1
                """,
                (snap["id"],),
            ).fetchone()
            return row is not None

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
            counts = {"sources": 0, "objects": 0, "attributes": 0, "table_parts": 0, "forms": 0, "form_elements": 0, "data_composition_fields": 0, "chunks": 0}
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
                    con.execute("INSERT INTO attributes(object_id,name,type_name,synonym,required,source_path,role,raw_json) VALUES(?,?,?,?,?,?,?,?)", (oid, attr.name, attr.type_name, attr.synonym, attr.required, attr.source_path, attr.role, json_dumps(attr.raw)))
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
                for dcs_field in parsed.data_composition_fields:
                    oid = object_ids.get(dcs_field.object_full_name)
                    if not oid:
                        continue
                    con.execute(
                        "INSERT INTO data_composition_fields(object_id,schema_name,data_path,field,title,expression,dataset_name,field_type,source_path,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (oid, dcs_field.schema_name, dcs_field.data_path, dcs_field.source_field, dcs_field.title, dcs_field.expression, dcs_field.dataset_name, dcs_field.field_type, dcs_field.source_path, json_dumps(dcs_field.raw)),
                    )
                    counts["data_composition_fields"] += 1
                    text_parts = [dcs_field.object_full_name, "СКД", dcs_field.schema_name, dcs_field.data_path, dcs_field.source_field or "", dcs_field.title or "", dcs_field.expression or ""]
                    chunk_text = " ".join(part for part in text_parts if part)
                    cid = _insert_chunk(con, snapshot_id, oid, "dcs_field", chunk_text, dcs_field.source_path, {"source": src["name"], "schema": dcs_field.schema_name})
                    con.execute("INSERT INTO search_fts(text, object_full_name, kind, source_path, chunk_id) VALUES(?,?,?,?,?)", (chunk_text, dcs_field.object_full_name, "СКД", dcs_field.source_path, cid))
                    counts["chunks"] += 1
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
                row["attributes"] = [dict(r) for r in con.execute("SELECT name,type_name,synonym,required,role FROM attributes WHERE object_id=? ORDER BY id", (oid,))]
                row["table_parts"] = [dict(r) for r in con.execute("SELECT id,name,synonym FROM table_parts WHERE object_id=? ORDER BY id", (oid,))]
                for tp in row["table_parts"]:
                    tp["columns"] = [dict(r) for r in con.execute("SELECT name,type_name,synonym,required FROM table_part_columns WHERE table_part_id=? ORDER BY id", (tp["id"],))]
                row["forms"] = [dict(r) for r in con.execute("SELECT id,name,kind,source_path FROM forms WHERE object_id=? ORDER BY id", (oid,))]
                row["data_composition_fields"] = [dict(r) for r in con.execute("SELECT schema_name,data_path,field,title,expression,dataset_name,field_type,source_path FROM data_composition_fields WHERE object_id=? ORDER BY id", (oid,))]
            return {"query": object, "snapshot": snapshot, "matches": rows}

    def lookup_form_element_types(self, snapshot: str | int | None = None) -> dict[str, Any]:
        """Return a compact name/title/data-path index for UI-tree enrichment.

        The 1C test-client API exposes runtime UI element names and captions, but
        not always enough metadata typing.  RAG already indexes form elements,
        attributes and DCS fields from the configuration source, so this helper
        builds a best-effort map usable by server-side MCP tools.

        1C can generate object/list/choice forms automatically from metadata.
        For those generated forms runtime field names usually match metadata
        attribute names/captions even when no explicit ``Form.xml`` exists.
        Therefore the index also exposes synthetic form elements for attributes
        and table-part columns.  They let ``ui_tree`` infer field types on
        auto-generated forms using metadata alone.
        """
        with self.store.connect() as con:
            source_filter = ""
            params: list[Any] = []
            if snapshot is not None:
                snap = _snapshot_row(con, snapshot)
                if snap is None:
                    raise ValueError(f"snapshot not found: {snapshot}")
                source_filter = "AND mo.source_id IN (SELECT source_id FROM snapshot_layers WHERE snapshot_id=?)"
                params.append(snap["id"])
            rows = [
                dict(r)
                for r in con.execute(
                    f"""
                    SELECT mo.full_name AS object_full_name, mo.kind AS object_kind,
                           f.name AS form_name, fe.name, fe.element_type, fe.title,
                           fe.data_path, fe.command_name, fe.parent_name, fe.source_path,
                           a.type_name AS attribute_type, a.role AS attribute_role,
                           dcf.field_type AS dcs_field_type
                    FROM form_elements fe
                    JOIN forms f ON f.id=fe.form_id
                    JOIN metadata_objects mo ON mo.id=f.object_id
                    LEFT JOIN attributes a ON a.object_id=mo.id AND (a.name=fe.data_path OR a.name=fe.name)
                    LEFT JOIN data_composition_fields dcf ON dcf.object_id=mo.id AND (dcf.data_path=fe.data_path OR dcf.field=fe.data_path OR dcf.title=fe.title)
                    WHERE 1=1 {source_filter}
                    ORDER BY mo.full_name, f.name, fe.id
                    """,
                    params,
                )
            ]
            synthetic_rows = [
                dict(r)
                for r in con.execute(
                    f"""
                    SELECT mo.full_name AS object_full_name, mo.kind AS object_kind,
                           '<auto>' AS form_name,
                           a.name AS name,
                           'AutoGeneratedField' AS element_type,
                           COALESCE(a.synonym, a.name) AS title,
                           a.name AS data_path,
                           NULL AS command_name,
                           NULL AS parent_name,
                           a.source_path AS source_path,
                           a.type_name AS attribute_type,
                           a.role AS attribute_role,
                           NULL AS dcs_field_type
                    FROM attributes a
                    JOIN metadata_objects mo ON mo.id=a.object_id
                    WHERE 1=1 {source_filter}
                    ORDER BY mo.full_name, a.id
                    """,
                    params,
                )
            ]
            table_part_rows = [
                dict(r)
                for r in con.execute(
                    f"""
                    SELECT mo.full_name AS object_full_name, mo.kind AS object_kind,
                           '<auto>' AS form_name,
                           (tp.name || '.' || tpc.name) AS name,
                           'AutoGeneratedTablePartField' AS element_type,
                           COALESCE(tpc.synonym, tpc.name) AS title,
                           (tp.name || '.' || tpc.name) AS data_path,
                           NULL AS command_name,
                           tp.name AS parent_name,
                           tpc.source_path AS source_path,
                           tpc.type_name AS attribute_type,
                           'table_part_column' AS attribute_role,
                           NULL AS dcs_field_type
                    FROM table_part_columns tpc
                    JOIN table_parts tp ON tp.id=tpc.table_part_id
                    JOIN metadata_objects mo ON mo.id=tp.object_id
                    WHERE 1=1 {source_filter}
                    ORDER BY mo.full_name, tp.name, tpc.id
                    """,
                    params,
                )
            ]
        items = rows + synthetic_rows + table_part_rows
        return {"snapshot": snapshot, "count": len(items), "items": items}

    def query(self, text: str, snapshot: str | int | None = None, limit: int = 10) -> dict[str, Any]:
        q = normalize_query(text)
        with self.store.connect() as con:
            snapshot_id = None
            if snapshot is not None:
                snap = _snapshot_row(con, snapshot)
                if snap is None:
                    raise ValueError(f"snapshot not found: {snapshot}")
                snapshot_id = snap["id"]
            term_filter = "AND bt.snapshot_id=?" if snapshot_id is not None else ""
            term_params: list[Any] = [f"%{q}%"]
            if snapshot_id is not None:
                term_params.append(snapshot_id)
            term_params.append(limit)
            term_rows = [
                dict(r)
                for r in con.execute(
                    f"""
                    SELECT bt.*, mo.full_name, mo.kind, s.name AS source_name
                    FROM business_terms bt
                    LEFT JOIN metadata_objects mo ON mo.id=bt.object_id
                    LEFT JOIN sources s ON s.id=mo.source_id
                    WHERE bt.normalized_term LIKE ? {term_filter}
                    ORDER BY bt.confidence DESC LIMIT ?
                    """,
                    term_params,
                )
            ]
            fts_query = " ".join(q.split()) or q
            try:
                if snapshot_id is None:
                    fts_rows = [dict(r) for r in con.execute("SELECT text, object_full_name, kind, source_path, bm25(search_fts) AS score FROM search_fts WHERE search_fts MATCH ? ORDER BY score LIMIT ?", (fts_query, limit))]
                else:
                    fts_rows = [
                        dict(r)
                        for r in con.execute(
                            """
                            SELECT search_fts.text, search_fts.object_full_name, search_fts.kind,
                                   search_fts.source_path, bm25(search_fts) AS score
                            FROM search_fts JOIN rag_chunks rc ON rc.id=search_fts.chunk_id
                            WHERE search_fts MATCH ? AND rc.snapshot_id=?
                            ORDER BY score LIMIT ?
                            """,
                            (fts_query, snapshot_id, limit),
                        )
                    ]
            except Exception:
                fts_rows = []
            if len(fts_rows) < limit:
                seen = {(r.get("object_full_name"), r.get("source_path")) for r in fts_rows}
                if snapshot_id is None:
                    like_rows = con.execute("SELECT text, object_full_name, kind, source_path FROM search_fts WHERE lower(text) LIKE ? LIMIT ?", (f"%{q}%", limit))
                else:
                    like_rows = con.execute(
                        """
                        SELECT search_fts.text, search_fts.object_full_name, search_fts.kind, search_fts.source_path
                        FROM search_fts JOIN rag_chunks rc ON rc.id=search_fts.chunk_id
                        WHERE lower(search_fts.text) LIKE ? AND rc.snapshot_id=? LIMIT ?
                        """,
                        (f"%{q}%", snapshot_id, limit),
                    )
                for row in like_rows:
                    item = dict(row)
                    key = (item.get("object_full_name"), item.get("source_path"))
                    if key not in seen:
                        fts_rows.append(item)
                        seen.add(key)
                    if len(fts_rows) >= limit:
                        break
        return {"query": text, "snapshot": snapshot, "terms": term_rows, "fts": fts_rows}


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


def _safe_source_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    return cleaned.strip("_")


def _snapshot_row(con, snapshot: str | int):
    if isinstance(snapshot, int) or str(snapshot).isdigit():
        return con.execute("SELECT * FROM snapshots WHERE id=?", (int(snapshot),)).fetchone()
    return con.execute("SELECT * FROM snapshots WHERE name=?", (snapshot,)).fetchone()


def _insert_chunk(con, snapshot_id, object_id, chunk_type, text, source_path, metadata) -> int:
    cur = con.execute("INSERT INTO rag_chunks(snapshot_id,object_id,chunk_type,text,source_path,metadata_json) VALUES(?,?,?,?,?,?)", (snapshot_id, object_id, chunk_type, text, source_path, json_dumps(metadata)))
    return cur.lastrowid


def _insert_business_term(con, snapshot_id, term, object_id, confidence, evidence) -> None:
    con.execute("INSERT INTO business_terms(snapshot_id,term,normalized_term,object_id,confidence,evidence) VALUES(?,?,?,?,?,?)", (snapshot_id, term, normalize_query(term), object_id, confidence, evidence))
