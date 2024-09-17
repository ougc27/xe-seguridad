from odoo import models, api
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    @api.onchange('purchase_order_reference')
    def _onchange_purchase_order_reference(self):
        addenda_id = self.env.ref('addenda_morwi_ace_hadware.l10n_mx_edi_addenda_ace_hardware')
        if self.purchase_order_reference and self.partner_id.l10n_mx_edi_addenda:
            if self.partner_id.l10n_mx_edi_addenda.id == addenda_id.id:
                if not self.purchase_order_reference.isnumeric():
                    raise UserError("Solo puede agregar valor númerico")