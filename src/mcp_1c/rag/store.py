from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3


class RagStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info(version INTEGER NOT NULL);
                DELETE FROM schema_info;
                INSERT INTO schema_info(version) VALUES (3);

                CREATE TABLE IF NOT EXISTS sources(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL UNIQUE,
                  path TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  format TEXT NOT NULL,
                  repo_url TEXT,
                  branch TEXT,
                  commit_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS snapshots(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS source_roots(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  path TEXT NOT NULL UNIQUE,
                  recursive INTEGER NOT NULL DEFAULT 1,
                  max_depth INTEGER NOT NULL DEFAULT 4,
                  prefix TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS snapshot_layers(
                  snapshot_id INTEGER NOT NULL,
                  source_id INTEGER NOT NULL,
                  layer_order INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  PRIMARY KEY(snapshot_id, source_id),
                  FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
                  FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS metadata_objects(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_id INTEGER NOT NULL,
                  full_name TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  name TEXT NOT NULL,
                  synonym TEXT,
                  source_path TEXT,
                  source_format TEXT,
                  is_hierarchical INTEGER,
                  owner_types_json TEXT,
                  raw_json TEXT,
                  UNIQUE(source_id, full_name),
                  FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS attributes(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_id INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  type_name TEXT,
                  synonym TEXT,
                  required INTEGER,
                  source_path TEXT,
                  role TEXT NOT NULL DEFAULT 'attribute',
                  raw_json TEXT,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS table_parts(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_id INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  synonym TEXT,
                  source_path TEXT,
                  raw_json TEXT,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS table_part_columns(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  table_part_id INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  type_name TEXT,
                  synonym TEXT,
                  required INTEGER,
                  source_path TEXT,
                  raw_json TEXT,
                  FOREIGN KEY(table_part_id) REFERENCES table_parts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS forms(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_id INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  kind TEXT,
                  source_path TEXT,
                  raw_json TEXT,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS form_elements(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  form_id INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  element_type TEXT,
                  title TEXT,
                  data_path TEXT,
                  command_name TEXT,
                  parent_name TEXT,
                  source_path TEXT,
                  raw_json TEXT,
                  FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS data_composition_fields(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_id INTEGER NOT NULL,
                  schema_name TEXT NOT NULL,
                  data_path TEXT NOT NULL,
                  field TEXT,
                  title TEXT,
                  expression TEXT,
                  dataset_name TEXT,
                  field_type TEXT,
                  source_path TEXT,
                  raw_json TEXT,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS navigation_links(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_id INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  url TEXT NOT NULL,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS business_terms(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  snapshot_id INTEGER,
                  term TEXT NOT NULL,
                  normalized_term TEXT NOT NULL,
                  object_id INTEGER,
                  relation TEXT NOT NULL DEFAULT 'maps_to',
                  confidence REAL NOT NULL DEFAULT 0.5,
                  evidence TEXT,
                  FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS rag_chunks(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  snapshot_id INTEGER,
                  object_id INTEGER,
                  chunk_type TEXT NOT NULL,
                  text TEXT NOT NULL,
                  source_path TEXT,
                  metadata_json TEXT,
                  FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
                  FOREIGN KEY(object_id) REFERENCES metadata_objects(id) ON DELETE CASCADE
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                  text,
                  object_full_name UNINDEXED,
                  kind UNINDEXED,
                  source_path UNINDEXED,
                  chunk_id UNINDEXED
                );

                CREATE INDEX IF NOT EXISTS idx_objects_source_name ON metadata_objects(source_id, full_name);
                CREATE INDEX IF NOT EXISTS idx_objects_kind ON metadata_objects(kind);
                CREATE INDEX IF NOT EXISTS idx_attrs_object ON attributes(object_id);
                CREATE INDEX IF NOT EXISTS idx_forms_object ON forms(object_id);
                CREATE INDEX IF NOT EXISTS idx_form_elements_name ON form_elements(form_id, name);
                CREATE INDEX IF NOT EXISTS idx_dcs_fields_object ON data_composition_fields(object_id, schema_name);
                CREATE INDEX IF NOT EXISTS idx_terms_norm ON business_terms(normalized_term);
                CREATE INDEX IF NOT EXISTS idx_layers_snapshot ON snapshot_layers(snapshot_id, layer_order);
                """
            )
            columns = {row["name"] for row in con.execute("PRAGMA table_info(attributes)")}
            if "role" not in columns:
                con.execute("ALTER TABLE attributes ADD COLUMN role TEXT NOT NULL DEFAULT 'attribute'")

    def clear_index_for_source(self, con: sqlite3.Connection, source_id: int) -> None:
        con.execute(
            """
            DELETE FROM search_fts
            WHERE chunk_id IN (
              SELECT rc.id
              FROM rag_chunks rc JOIN metadata_objects mo ON mo.id=rc.object_id
              WHERE mo.source_id=?
            )
            """,
            (source_id,),
        )
        con.execute("DELETE FROM metadata_objects WHERE source_id=?", (source_id,))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)
