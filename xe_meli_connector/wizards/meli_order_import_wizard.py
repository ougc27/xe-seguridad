from odoo import _, fields, models
from odoo.exceptions import UserError


class MeliOrderImportWizard(models.TransientModel):
    _name = 'meli.order.import.wizard'
    _description = 'Manually Import/Refresh One Mercado Libre Order'

    meli_order_id = fields.Char(
        string='Mercado Libre Order ID', required=True,
        help="The individual order id (not the pack id) — same value "
             "sale.order._meli_import_order itself takes.",
    )

    def action_import(self):
        """Synchronous, one-off manual trigger for
        sale.order._meli_import_order — the same entry point every
        automated recovery path in this module already calls, exposed
        here for on-demand testing/troubleshooting (2026-09-18 user
        request) without needing shell access. Runs inline (no
        with_delay): a person watching this run wants to see the result
        immediately, not a queued job.
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
            config.company_id.id, self.meli_order_id.strip(),
        )
        if not order:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Mercado Libre"),
                    'message': _(
                        "No se creó/actualizó ninguna venta — revisa los "
                        "logs (estatus de la orden en Mercado Libre no es "
                        "'paid' ni 'cancelled', o algún otro error)."
                    ),
                    'type': 'warning',
                },
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }
