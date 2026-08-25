# -*- coding: utf-8 -*-
import json
import logging
import os
from datetime import datetime
from html import escape

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval

from .supabase_connector import SupabaseConnector

_logger = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1)

# Number of consecutive failures after which a job disables itself instead of
# failing silently run after run.
MAX_CONSECUTIVE_ERRORS = 5


class SupabaseSyncJob(models.Model):
    _name = 'supabase.sync.job'
    _description = 'Odoo -> Supabase sync job'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # ------------------------------------------------------------------
    # Extraction: where and how the data is read from Odoo
    # ------------------------------------------------------------------
    extraction_mode = fields.Selection([
        ('sql', 'Direct SQL'),
        ('orm', 'ORM (computed / non-stored fields)'),
    ], string='Extraction Mode', default='sql', required=True,
        help="Direct SQL: fast, ideal for columns that actually live in the "
             "database (most of stock.quant/sale.order/pos.order). "
             "ORM: slower but required to read computed fields with "
             "store=False, since those values don't exist as a real column "
             "and only Odoo can compute them.")

    source_sql = fields.Text(
        string='Extraction Query (SQL)',
        help="SELECT against Odoo's own database. Must include "
             "%(last_id)s and %(batch_size)s (and %(last_write_date)s if "
             "applicable), and end with ORDER BY the same checkpoint "
             "column(s). Column aliases (AS) define the final column name "
             "in Supabase.")

    orm_model = fields.Char(
        string='Odoo Model (technical name)',
        help="E.g. sale.order. Only used in ORM mode.")
    orm_domain = fields.Char(
        string='Extra Domain', default='[]',
        help="Standard Odoo domain, e.g. [('state','=','sale')]. The "
             "pagination filter (id, or write_date+id for incremental "
             "sync) is appended automatically.")
    orm_field_map = fields.Text(
        string='Column Mapping (JSON)',
        help='{"supabase_column": "python_expression"}, evaluated with '
             '"record" in context. E.g.: {"odoo_id": "record.id", '
             '"margin": "record.margin", "salesperson": "record.user_id.name"}. '
             'For incremental_upsert, must also include a key matching '
             'Write Date Column (e.g. "fecha_modificacion_odoo": '
             '"record.write_date") — otherwise the checkpoint cannot '
             'advance and updates never get re-synced.')

    # ------------------------------------------------------------------
    # Target in Supabase
    # ------------------------------------------------------------------
    target_table = fields.Char(
        string='Target Table', required=True,
        help="Schema-qualified destination table. E.g. odoo_sync.sale_orders")
    conflict_column = fields.Char(
        string='Conflict Column', default='odoo_id', required=True,
        help="Column with a PRIMARY KEY/UNIQUE constraint in Supabase, used "
             "for the upsert. Comma-separated for a composite key (e.g. "
             "'modelo,odoo_id' when unifying more than one Odoo model into "
             "the same table) — put the auto-increment id-like column LAST, "
             "since it's also the one used for checkpoint pagination.")
    write_date_column = fields.Char(
        string='Write Date Column', default='write_date_odoo',
        help="Column (among the ones returned by the query/mapping) that "
             "carries Odoo's write_date. Used together with the id for the "
             "incremental checkpoint, so updates get captured too, not just "
             "new records.")

    sync_mode = fields.Selection([
        ('incremental_upsert', 'Incremental (checkpoint upsert)'),
        ('full_snapshot', 'Full snapshot (replaces the whole table every run)'),
    ], string='Sync Mode', default='incremental_upsert', required=True,
        help="Full snapshot: meant for tables where Odoo constantly deletes "
             "and creates rows (e.g. stock.quant) — avoids leaving orphaned "
             "rows in Supabase. Uses a staging table plus an atomic swap, so "
             "the table is never seen empty or half-loaded.")

    batch_size = fields.Integer(string='Batch Size', default=1000, required=True)
    frequency_minutes = fields.Integer(
        string='Frequency (minutes)', default=15, required=True,
        help="How often this job is considered 'due' in the module's single "
             "cron tick (no need to create a separate ir.cron per table).")

    # ------------------------------------------------------------------
    # State / checkpoint
    # ------------------------------------------------------------------
    last_sync_id = fields.Integer(string='Last Synced ID', default=0, readonly=True, copy=False)
    last_sync_write_date = fields.Datetime(
        string='Last Synced Write Date', readonly=True, copy=False)
    last_run_at = fields.Datetime(string='Last Run At', readonly=True, copy=False)
    last_status = fields.Selection([
        ('success', 'OK'),
        ('error', 'Error'),
        ('running', 'Running'),
    ], string='Last Status', readonly=True, copy=False)
    last_row_count = fields.Integer(string='Last Row Count', readonly=True, copy=False)
    consecutive_errors = fields.Integer(
        string='Consecutive Errors', default=0, readonly=True, copy=False)

    log_ids = fields.One2many('supabase.sync.log', 'job_id', string='History')

    test_extraction_result = fields.Html(
        string='Test Extraction Result', readonly=True, copy=False, sanitize=False)

    # ------------------------------------------------------------------
    # conflict_column can be a single column ("odoo_id") or a comma-
    # separated composite ("modelo,odoo_id") for tables that unify more
    # than one Odoo model. The LAST part is always the id-like column
    # used for checkpoint pagination — the composite as a whole is only
    # needed for the Supabase upsert's ON CONFLICT target.
    # ------------------------------------------------------------------
    def _conflict_columns(self):
        return [c.strip() for c in (self.conflict_column or '').split(',') if c.strip()]

    def _conflict_id_column(self):
        cols = self._conflict_columns()
        return cols[-1] if cols else self.conflict_column

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('extraction_mode', 'source_sql', 'orm_model', 'orm_field_map',
                     'conflict_column', 'sync_mode', 'write_date_column')
    def _check_extraction_config(self):
        for job in self:
            if job.extraction_mode == 'sql':
                sql_text = job.source_sql or ''
                if '%(last_id)s' not in sql_text or '%(batch_size)s' not in sql_text:
                    raise ValidationError(_(
                        "The extraction SQL must include %%(last_id)s and "
                        "%%(batch_size)s for batch pagination to work "
                        "(job: %s)."
                    ) % job.name)
            else:
                if not job.orm_model:
                    raise ValidationError(_(
                        "Missing the technical Odoo model for ORM mode (job: %s)."
                    ) % job.name)
                if not job.orm_field_map:
                    raise ValidationError(_(
                        "Missing the column mapping for ORM mode (job: %s)."
                    ) % job.name)
                try:
                    mapping = json.loads(job.orm_field_map)
                except ValueError as exc:
                    raise ValidationError(_(
                        "The column mapping is not valid JSON (job: %s): %s"
                    ) % (job.name, exc))
                missing = [c for c in job._conflict_columns() if c not in mapping]
                if missing:
                    raise ValidationError(_(
                        "The mapping must include the conflict column(s) "
                        "'%s' (job: %s)."
                    ) % (', '.join(missing), job.name))
                if (job.sync_mode == 'incremental_upsert'
                        and job.write_date_column not in mapping):
                    raise ValidationError(_(
                        "For incremental sync in ORM mode, the mapping "
                        "must include the write date column '%s' (job: "
                        "%s) — otherwise the checkpoint can never move "
                        "past the epoch and the same batch repeats forever."
                    ) % (job.write_date_column, job.name))

    # ------------------------------------------------------------------
    # "Test" button — same idea as sdlc_psql_query_execute: a small read
    # (5 rows), never touching Supabase, to validate before activating.
    # Result renders as an HTML table (Preview tab), not a text popup.
    # ------------------------------------------------------------------
    def action_test_extraction(self):
        self.ensure_one()
        params = {'last_id': 0, 'last_write_date': EPOCH, 'batch_size': 5}
        columns, rows = self._extract_batch(params)
        self.test_extraction_result = self._build_preview_table(columns, rows)
        # Reopening the same record (instead of a display_notification client
        # action) is what reliably forces the form to reload with the fresh
        # Preview table — a notification alone does not refresh the form.
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @staticmethod
    def _build_preview_table(columns, rows):
        def cell(value):
            text = '' if value is None else str(value)
            return escape(text)

        head = ''.join('<th>%s</th>' % escape(str(c)) for c in columns)
        if rows:
            body = ''.join(
                '<tr>%s</tr>' % ''.join('<td>%s</td>' % cell(v) for v in row)
                for row in rows
            )
        else:
            body = '<tr><td colspan="%s"><em>%s</em></td></tr>' % (
                max(len(columns), 1), escape(_('(no rows)')))
        return (
            '<div class="table-responsive">'
            '<table class="table table-sm table-bordered table-striped">'
            '<thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
        ) % (head, body)

    def action_run_now(self):
        for job in self:
            job._run_sync_safe()
        return True

    def _get_supabase_dsn(self):
        """Resolve the Postgres connection string for Supabase.

        Primary source: ir.config_parameter — same mechanism already used
        elsewhere in this codebase (xe_whatsapp, xe_pacific, muk_mcp) for
        this exact kind of secret. It is set once per Odoo *database*
        (local, Odoo.sh staging, Odoo.sh production each have their own),
        through Settings > Technical > Parameters > System Parameters —
        never in code, never in the git repo, and it works identically on
        Odoo.sh, which has no simple way to add custom OS environment
        variables per branch.

        An environment variable fallback is kept only as a convenience for
        ad-hoc local testing without touching the database.
        """
        icp = self.env['ir.config_parameter'].sudo()
        url = icp.get_param('xe_supabase_sync.db_url') or os.environ.get('SUPABASE_DB_URL')
        if url:
            return url

        host = icp.get_param('xe_supabase_sync.db_host') or os.environ.get('SUPABASE_DB_HOST')
        password = (icp.get_param('xe_supabase_sync.db_password')
                    or os.environ.get('SUPABASE_DB_PASSWORD'))
        if not host or not password:
            raise UserError(_(
                "Missing Supabase credentials. Set them under Settings > "
                "Technical > Parameters > System Parameters: either "
                "xe_supabase_sync.db_url (full connection string), or "
                "xe_supabase_sync.db_host + xe_supabase_sync.db_password "
                "(optionally also xe_supabase_sync.db_port, "
                "xe_supabase_sync.db_name, xe_supabase_sync.db_user)."
            ))
        port = icp.get_param('xe_supabase_sync.db_port') or os.environ.get('SUPABASE_DB_PORT', '6543')
        dbname = (icp.get_param('xe_supabase_sync.db_name')
                  or os.environ.get('SUPABASE_DB_NAME', 'postgres'))
        user = icp.get_param('xe_supabase_sync.db_user') or os.environ.get('SUPABASE_DB_USER', 'postgres')
        return (
            "host={host} port={port} dbname={dbname} user={user} "
            "password={password} sslmode=require connect_timeout=10"
        ).format(host=host, port=port, dbname=dbname, user=user, password=password)

    def action_reset_checkpoint(self):
        """For testing: forces the next run to start from scratch. Safe to
        use even against a Supabase table that already has data — the
        upsert (ON CONFLICT) means replayed rows get updated in place,
        never duplicated. Does not touch the consecutive-error counter."""
        self.write({
            'last_sync_id': 0,
            'last_sync_write_date': False,
        })

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def _extract_batch(self, params):
        self.ensure_one()
        if self.extraction_mode == 'sql':
            return self._extract_batch_sql(params)
        return self._extract_batch_orm(params)

    def _extract_batch_sql(self, params):
        """Read directly from Odoo's own database via self.env.cr (same
        technique as sdlc_psql_query_execute). Keyset pagination
        (id/write_date > last checkpoint), never OFFSET, so it stays fast
        with millions of rows."""
        self.env.cr.execute(self.source_sql, params)
        columns = [desc[0] for desc in self.env.cr.description]
        rows = self.env.cr.fetchall()
        return columns, rows

    def _extract_batch_orm(self, params):
        """Read via the ORM so computed, non-stored fields can be included
        (they don't exist as a real column, so only Odoo can compute them).
        The whole batch is built from a single `search()` call so Odoo's
        prefetch computes the field for the batch together, not record by
        record.

        For incremental_upsert, pagination mirrors SQL mode's (write_date,
        id) keyset — expressed as an OR domain, since Odoo domains have no
        row-constructor comparison — so updates to already-synced records
        get picked up too, not just new ones. full_snapshot keeps plain
        id-only pagination: it re-reads everything every run anyway, and
        using write_date there (always EPOCH) would match virtually every
        record regardless of id, breaking the "next batch" progression."""
        model = self.env[self.orm_model].sudo()
        mapping = json.loads(self.orm_field_map)
        columns = list(mapping.keys())
        extra_domain = safe_eval(self.orm_domain or '[]')

        if self.sync_mode == 'incremental_upsert':
            last_write_date = params.get('last_write_date') or EPOCH
            keyset_domain = [
                '|', ('write_date', '>', last_write_date),
                '&', ('write_date', '=', last_write_date),
                ('id', '>', params['last_id']),
            ]
            order = 'write_date asc, id asc'
        else:
            keyset_domain = [('id', '>', params['last_id'])]
            order = 'id asc'

        records = model.search(extra_domain + keyset_domain, order=order,
                                limit=params['batch_size'])

        rows = []
        for record in records:
            row = []
            for expr in mapping.values():
                try:
                    row.append(safe_eval(expr, {'record': record}))
                except Exception as exc:
                    raise UserError(_(
                        "Error evaluating '%(expr)s' on record id=%(id)s: %(err)s"
                    ) % {'expr': expr, 'id': record.id, 'err': exc})
            rows.append(tuple(row))

        # Clear the ORM cache after each batch: without this, a run over
        # millions of records with computed fields keeps everything in RAM
        # and can crash the Odoo.sh container (OOM).
        self.env.invalidate_all()
        return columns, rows

    # ------------------------------------------------------------------
    # Run orchestration
    # ------------------------------------------------------------------
    def _run_sync_safe(self):
        self.ensure_one()
        log = self.env['supabase.sync.log'].create({
            'job_id': self.id,
            'started_at': fields.Datetime.now(),
            'status': 'running',
        })
        # Committed right away, so there's a record that the run started
        # even if it crashes later (including a container failure mid-run).
        self.env.cr.commit()

        try:
            connector = SupabaseConnector(self._get_supabase_dsn())
            conn = connector.connect()
            try:
                if self.sync_mode == 'full_snapshot':
                    total = self._run_full_snapshot(connector, conn)
                else:
                    total = self._run_incremental(connector, conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - logged, doesn't stop the tick
            _logger.exception("supabase.sync.job '%s' failed", self.name)
            new_errors = self.consecutive_errors + 1
            vals = {
                'last_status': 'error',
                'last_run_at': fields.Datetime.now(),
                'consecutive_errors': new_errors,
            }
            if new_errors >= MAX_CONSECUTIVE_ERRORS:
                vals['active'] = False
            self.write(vals)
            log.write({
                'status': 'error',
                'finished_at': fields.Datetime.now(),
                'error_message': str(exc),
            })
            if new_errors >= MAX_CONSECUTIVE_ERRORS:
                _logger.error(
                    "supabase.sync.job '%s' auto-disabled after %s consecutive "
                    "failures. Check supabase.sync.log before reactivating it.",
                    self.name, MAX_CONSECUTIVE_ERRORS)
            self.env.cr.commit()
            return False
        else:
            self.write({
                'last_status': 'success',
                'last_run_at': fields.Datetime.now(),
                'last_row_count': total,
                'consecutive_errors': 0,
            })
            log.write({
                'status': 'success',
                'finished_at': fields.Datetime.now(),
                'rows_synced': total,
            })
            self.env.cr.commit()
            return True

    def _run_incremental(self, connector, conn):
        total = 0
        batch_size = self.batch_size or 1000
        while True:
            params = {
                'last_id': self.last_sync_id or 0,
                'last_write_date': self.last_sync_write_date or EPOCH,
                'batch_size': batch_size,
            }
            columns, rows = self._extract_batch(params)
            if not rows:
                break

            # Real upsert: if the odoo_id already exists in Supabase it gets
            # updated, never duplicated — even if this batch is retried.
            connector.upsert_batch(conn, self.target_table, columns, rows, self.conflict_column)
            conn.commit()

            last_row = dict(zip(columns, rows[-1]))
            checkpoint_vals = {}
            id_column = self._conflict_id_column()
            if id_column in last_row:
                checkpoint_vals['last_sync_id'] = last_row[id_column]
            if self.write_date_column in last_row and last_row[self.write_date_column]:
                checkpoint_vals['last_sync_write_date'] = last_row[self.write_date_column]
            if checkpoint_vals:
                self.write(checkpoint_vals)
            # The checkpoint must survive even if the NEXT batch fails, so
            # the next run resumes here instead of starting from scratch.
            self.env.cr.commit()

            total += len(rows)
            _logger.info("supabase.sync.job '%s': %s rows synced so far", self.name, total)

            if len(rows) < batch_size:
                break
        return total

    def _run_full_snapshot(self, connector, conn):
        staging_table = "%s__staging" % self.target_table
        connector.recreate_staging(conn, staging_table, self.target_table)

        total = 0
        last_id = 0
        batch_size = self.batch_size or 2000
        while True:
            params = {'last_id': last_id, 'last_write_date': EPOCH, 'batch_size': batch_size}
            columns, rows = self._extract_batch(params)
            if not rows:
                break

            connector.insert_batch(conn, staging_table, columns, rows)
            conn.commit()

            row_dict = dict(zip(columns, rows[-1]))
            last_id = row_dict.get(self._conflict_id_column(), last_id)
            total += len(rows)
            _logger.info("supabase.sync.job '%s' (snapshot): %s rows staged", self.name, total)

            if len(rows) < batch_size:
                break

        # Atomic swap: the "live" table flips from the old version to the
        # new one in one shot. It is never seen empty or half-loaded.
        connector.swap_tables(conn, live_table=self.target_table, staging_table=staging_table)
        return total

    # ------------------------------------------------------------------
    # Single cron for every job — adding a new table means adding a
    # supabase.sync.job record, never a new ir.cron.
    # ------------------------------------------------------------------
    @api.model
    def _cron_tick(self):
        now = fields.Datetime.now()
        for job in self.search([('active', '=', True)]):
            if job.last_run_at:
                elapsed_minutes = (now - job.last_run_at).total_seconds() / 60.0
                if elapsed_minutes < job.frequency_minutes:
                    continue
            job._run_sync_safe()
