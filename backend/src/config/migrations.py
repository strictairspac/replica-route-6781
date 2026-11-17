"""Idempotent column additions for databases created before a feature landed.

``metadata.create_all`` creates missing tables but never alters existing ones,
so a pre-existing ``trials`` table needs the new topology pointer added here.
"""
import logging
from sqlalchemy import inspect, text
from src.config.database import engine

ADDITIONS = {
    'trials': {'topology_version_id': 'INTEGER NULL'},
}


def ensure_schema():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in ADDITIONS.items():
            if table not in existing_tables:
                continue
            present = {col['name'] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in present:
                    continue
                logging.info('adding column %s.%s', table, name)
                conn.execute(text('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, ddl)))
