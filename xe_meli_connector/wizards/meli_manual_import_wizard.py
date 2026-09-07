from odoo import _, fields, models
from odoo.exceptions import UserError


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
        order = self.env['sale.order'].sudo()._meli_import_order(
            config.company_id.id, self.order_id.strip(),
        )
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
