from odoo import api, models, fields, _
from odoo.exceptions import UserError


class PurchaseRequestWizard(models.TransientModel):
    _name = 'purchase.request.wizard'
    _description = 'Generate Purchase Request from Sale Order'

    line_ids = fields.One2many('purchase.request.wizard.line', 'wizard_id', string='Lines')

    sale_order_id = fields.Many2one(
        'sale.order',
        string='Sales Order',
        required=True
    )

    company_id = fields.Many2one(
        'res.company',
        required=True
    )

    requested_by = fields.Many2one(
        'res.users',
        required=True
    )

    def action_confirm(self):
        """Split the original picking into a new partial transfer."""
        self.ensure_one()

        sale_order_id = self.sale_order_id
        sale_company_id = self.sale_order_id.company_id.id

        if any(not line.company_id for line in self.line_ids):
            raise UserError(_('All lines must have a company.'))

        PurchaseRequest = self.env['purchase.request']
        PurchaseRequestLine = self.env['purchase.request.line']

        lines_by_company = {}
        for line in self.line_ids:
            lines_by_company.setdefault(line.company_id, []).append(line)

        created_requests = self.env['purchase.request']

        for company, lines in lines_by_company.items():
            pr = PurchaseRequest.create({
                'company_id': sale_company_id,
                'assigned_to': 29,
                'state': 'to_approve',
                'requested_by': self.requested_by.id,
                'sale_order_id': self.sale_order_id.id,
                'is_intercompany': True,
                'source_company_id': company.id,
                'destination_company_id': sale_company_id,
            })

            for line in lines:
                PurchaseRequestLine.create({
                    'request_id': pr.id,
                    'product_id': line.product_id.id,
                    'name': line.name,
                    'product_qty': line.product_qty,
                    'product_uom_id': line.product_uom_id.id,
                    'company_id': company.id,
                })

            created_requests |= pr

        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Requests'),
            'res_model': 'purchase.request',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', created_requests.ids)],
        }

class PurchaseRequestWizardLine(models.TransientModel):
    _name = 'purchase.request.wizard.line'
    _description = 'Generate Purchase Request Lines from Sale Order'

    wizard_id = fields.Many2one('purchase.request.wizard', string='Wizard', ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Product', required=True)
    name = fields.Char(string='Description', required=True)
    product_qty = fields.Float(string='Quantity', required=True)
    product_uom_id = fields.Many2one('uom.uom', string='UoM', required=True)
    company_id = fields.Many2one('res.company')
