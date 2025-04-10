from odoo import fields, models, api
from odoo.osv import expression


class ProcurementGroup(models.Model):
    _inherit = 'procurement.group'

    @api.model
    def _get_moves_to_assign_domain(self, company_id):
        moves_domain = [
            ('state', 'in', ['confirmed', 'partially_available']),
            ('product_uom_qty', '!=', 0.0),
            ('picking_id.state', '!=', 'transit'),
            '|',
                ('reservation_date', '<=', fields.Date.today()),
                ('picking_type_id.reservation_method', '=', 'at_confirm'),
        ]
        if company_id and company_id != 4:
            moves_domain = expression.AND([[('company_id', '=', company_id)], moves_domain])
        else:
            exclusion_domain = [('company_id', '!=', 4)]
            moves_domain = expression.AND([moves_domain, exclusion_domain])

        return moves_domain
