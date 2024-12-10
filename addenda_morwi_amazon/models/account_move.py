from odoo import models, fields


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    def get_amazon_po_fixed(self):
        """
            return : Devuelve un valor estático para la addenda
        """
        return 'AmazonPO'