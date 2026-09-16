from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MeliInvoiceRecoveryWizard(models.TransientModel):
    _name = 'meli.invoice.recovery.wizard'
    _description = 'Recover Mercado Libre Invoice Documents for a Date Range'

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
        """Enqueues a single job and returns immediately — no order
        enumeration happens in this request at all (found in practice
        2026-09-08: even a loop that only calls with_delay() per order
        made this wizard hang for minutes over a real date range, since
        a busy day can have 1,100+ orders). The actual search and
        per-order enqueueing now happens inside
        meli.invoice.document._meli_recover_invoices_in_range, entirely
        in the background.
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.env.company.id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            raise UserError(_(
                "There is no active Mercado Libre connection for this company."
            ))
        self.env['meli.invoice.document'].with_delay(
            priority=8, channel='root.meli_sales', max_retries=8,
            description=(
                f"Recover Mercado Libre invoices from {self.date_from} "
                f"to {self.date_to}"
            ),
            identity_key=(
                f"meli_recover_invoices_{config.company_id.id}_"
                f"{self.date_from}_{self.date_to}"
            ),
        )._meli_recover_invoices_in_range(
            config.company_id.id, self.date_from, self.date_to,
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "Se encoló la búsqueda de facturas para el rango "
                    "seleccionado — revisa 'Facturación Mercado Libre' "
                    "en unos minutos."
                ),
                'type': 'success',
            },
        }
