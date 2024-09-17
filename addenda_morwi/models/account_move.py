from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    purchase_order_reference = fields.Char(copy=False)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'
    
    discount_amount_addenda = fields.Float(copy=False, compute='_compute_discount_amount_addenda')
    
    def _compute_discount_amount_addenda(self):
        for record in self:
            discount_amount_addenda = 0.0
            if record.move_type == 'out_invoice' and record.partner_id and record.partner_id.l10n_mx_edi_addenda:
                    discount_amount_addenda = record.currency_id.round(record.price_subtotal * (record.discount / 100.0))
            record.discount_amount_addenda = discount_amount_addenda