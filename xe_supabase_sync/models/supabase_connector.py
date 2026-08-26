# -*- coding: utf-8 -*-
"""Transport to Supabase: direct Postgres connection (psycopg2), no REST API.

A direct TCP connection (via Supabase's connection pooler) is used instead of
the REST/PostgREST API because the expected volume (stock.quant, sale.order,
pos.order -> millions of rows in the historical load) makes thousands of HTTP
requests far more expensive than a bulk INSERT over the native Postgres
protocol.

Credentials never live in this module's code or in the repo: this class only
ever receives an already-built DSN string. Where that DSN comes from (Odoo's
own ir.config_parameter, resolved per-database) is the caller's
responsibility — see SupabaseSyncJob._get_supabase_dsn().
"""
import re

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


class SupabaseConnector:
    """Connection and write helpers for Supabase's Postgres database."""

    def __init__(self, dsn):
        self.dsn = dsn

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = False
        return conn

    # ------------------------------------------------------------------
    # Safe identifiers (table/column come from configuration, not from an
    # end-user value, but they are validated anyway before being
    # interpolated into dynamic SQL)
    # ------------------------------------------------------------------
    @staticmethod
    def _ident(dotted_name):
        parts = dotted_name.split('.')
        for part in parts:
            if not IDENTIFIER_RE.match(part):
                raise ValueError("Invalid table/column name: %r" % (dotted_name,))
        return sql.SQL('.').join(sql.Identifier(p) for p in parts)

    # ------------------------------------------------------------------
    # Incremental write (upsert)
    # ------------------------------------------------------------------
    def upsert_batch(self, conn, table, columns, rows, conflict_column):
        """conflict_column is normally a single column ("odoo_id"), but can
        be a comma-separated composite ("modelo,odoo_id") when a table
        unifies more than one Odoo model and a single id is not enough to
        tell rows apart."""
        if not rows:
            return
        conflict_cols = [c.strip() for c in conflict_column.split(',') if c.strip()]
        for col in list(columns) + conflict_cols:
            self._ident(col)

        update_cols = [c for c in columns if c not in conflict_cols]
        set_clause = sql.SQL(', ').join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in update_cols
        )
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s "
            "ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}"
        ).format(
            table=self._ident(table),
            cols=sql.SQL(', ').join(sql.Identifier(c) for c in columns),
            conflict=sql.SQL(', ').join(sql.Identifier(c) for c in conflict_cols),
            set_clause=set_clause,
        )
        with conn.cursor() as cur:
            execute_values(cur, query, rows, page_size=max(len(rows), 1))

    # ------------------------------------------------------------------
    # Plain write (only ever used against a freshly created, empty staging
    # table, for full_snapshot mode)
    # ------------------------------------------------------------------
    def insert_batch(self, conn, table, columns, rows):
        if not rows:
            return
        for col in columns:
            self._ident(col)
        query = sql.SQL("INSERT INTO {table} ({cols}) VALUES %s").format(
            table=self._ident(table),
            cols=sql.SQL(', ').join(sql.Identifier(c) for c in columns),
        )
        with conn.cursor() as cur:
            execute_values(cur, query, rows, page_size=max(len(rows), 1))

    # ------------------------------------------------------------------
    # Full snapshot: staging table + atomic swap, so the "live" table is
    # never seen empty or half-loaded.
    # ------------------------------------------------------------------
    def recreate_staging(self, conn, staging_table, like_table):
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {t}").format(t=self._ident(staging_table)))
            cur.execute(sql.SQL("CREATE TABLE {t} (LIKE {like} INCLUDING ALL)").format(
                t=self._ident(staging_table), like=self._ident(like_table),
            ))
        conn.commit()

    def swap_tables(self, conn, live_table, staging_table):
        live_name = live_table.split('.')[-1]
        old_name = live_name + '__old'
        schema_prefix = live_table.rsplit('.', 1)[0] + '.' if '.' in live_table else ''
        old_qualified = self._ident(schema_prefix + old_name)

        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {t}").format(t=old_qualified))
            cur.execute(sql.SQL("ALTER TABLE {t} RENAME TO {n}").format(
                t=self._ident(live_table), n=sql.Identifier(old_name),
            ))
            cur.execute(sql.SQL("ALTER TABLE {t} RENAME TO {n}").format(
                t=self._ident(staging_table), n=sql.Identifier(live_name),
            ))
            cur.execute(sql.SQL("DROP TABLE {t}").format(t=old_qualified))
        conn.commit()
