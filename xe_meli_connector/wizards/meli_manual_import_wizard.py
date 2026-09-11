from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.queue_job.exception import RetryableJobError


class MeliManualImportWizard(models.TransientModel):
    _name = 'meli.manual.import.wizard'
    _description = 'Manually Import a Mercado Libre Order'

    order_id = fields.Char(string='Mercado Libre Order ID', required=True)

    def action_import(self):
        """Runs synchronously (no queue_job) — this is a one-off manual
        action a person is actively waiting on, for exactly the case
        where the webhook and the polling cron both missed an order
        (found in practice 2026-08-28) and someone needs it in Odoo right
        now instead of waiting to diagnose why.
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.env.company.id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            raise UserError(_(
                "There is no active Mercado Libre connection for this company."
            ))
        try:
            order = self.env['sale.order'].sudo()._meli_import_order(
                config.company_id.id, self.order_id.strip(),
            )
        except RetryableJobError:
            # Fix 2026-09-10: shipping-detail fetch failures now raise
            # this instead of silently creating the order without
            # knowing its shipping details (see
            # sale.order._meli_fetch_shipment_records). There's no queue
            # job here to retry it automatically, so translate it into a
            # plain, actionable message instead of a raw traceback.
            raise UserError(_(
                "Could not reach Mercado Libre to verify this order's "
                "shipping details. Try again in a moment."
            ))
        if not order:
            raise UserError(_(
                "Mercado Libre order %s could not be imported — it may not "
                "be 'paid' yet. Check its status directly on Mercado Libre."
            ) % self.order_id)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }
