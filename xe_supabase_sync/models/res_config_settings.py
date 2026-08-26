# -*- coding: utf-8 -*-
import psycopg2

from odoo import fields, models, _
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    supabase_db_url = fields.Char(
        string='Supabase Connection String',
        config_parameter='xe_supabase_sync.db_url',
        help="Full Postgres connection string for Supabase's pooler, used "
             "by every Supabase Sync job on this database. Get it from the "
             "Supabase dashboard: Project Settings > Database > Connection "
             "string > Transaction pooler.")

    def action_test_supabase_connection(self):
        """Bare connectivity/auth check: no job, no target table needed.
        Always tests the SAVED value (ir.config_parameter), so save the
        settings first if the field was just edited."""
        self.ensure_one()
        dsn = self.env['ir.config_parameter'].sudo().get_param('xe_supabase_sync.db_url')
        if not dsn:
            raise UserError(_("Save a connection string first, then test it."))
        try:
            conn = psycopg2.connect(dsn, connect_timeout=10)
            try:
                with conn.cursor() as cur:
                    cur.execute("select current_database(), current_user, now()")
                    dbname, user, now = cur.fetchone()
            finally:
                conn.close()
        except Exception as exc:
            raise UserError(_("Connection failed: %s") % exc)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Supabase connection OK'),
                'message': _(
                    "Connected as '%(user)s' to database '%(db)s' "
                    "(server time: %(now)s)."
                ) % {'user': user, 'db': dbname, 'now': now},
                'type': 'success',
                'sticky': False,
            },
        }
