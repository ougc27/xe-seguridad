from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MeliClaimRecoveryWizard(models.TransientModel):
    _name = 'meli.claim.recovery.wizard'
    _description = 'Recover Mercado Libre Claims for a Date Range'

    date_from = fields.Datetime(
        string='From', required=True,
        default=lambda self: fields.Datetime.subtract(fields.Datetime.now(), hours=24),
    )
    date_to = fields.Datetime(
        string='To', required=True, default=fields.Datetime.now,
    )

    @api.constrains('date_from', 'date_to')
    def _check_date_range(self):
        for wizard in self:
            if wizard.date_from >= wizard.date_to:
                raise UserError(_("'From' must be earlier than 'To'."))

    def action_recover(self):
        """One-off recovery for a date range someone picks by hand —
        typically the window of an outage already diagnosed elsewhere
        (a bug, a webhook that silently failed). Runs synchronously up
        to the point of enqueueing: a person is actively waiting to
        know how many claims were found, but the actual re-import of
        each one still goes through queue_job like every other claim
        import, so a wide date range can't block the request for long
        or overload a single transaction.
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.env.company.id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            raise UserError(_(
                "There is no active Mercado Libre connection for this company."
            ))
        claim_ids = config._meli_search_claim_ids(self.date_from, self.date_to)
        Claim = self.env['meli.claim'].sudo()
        for claim_id in claim_ids:
            Claim.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                description=f"Import Mercado Libre claim {claim_id} (recovery)",
                identity_key=f"meli_import_claim_{claim_id}",
            )._meli_import_claim(config.company_id.id, claim_id)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "%(count)s claim(s) found and queued for re-import."
                ) % {'count': len(claim_ids)},
                'type': 'success',
            },
        }
