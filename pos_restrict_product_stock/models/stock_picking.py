from odoo import models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def action_print_conformity_ticket(self):
        return self.env.ref('pos_restrict_product_stock.action_report_ticket_acceptance_document').report_action(self)
