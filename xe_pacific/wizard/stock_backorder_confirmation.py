from odoo import models


class StockBackorderConfirmation(models.TransientModel):
    _inherit = 'stock.backorder.confirmation'

    def send_backorder_mail(self):
        template = self.env.ref('xe_pacific.email_template_backorder_to_salesperson')
        template.send_mail(self.id, force_send=True)

    def process(self):      
        res = super().process()
        self.send_backorder_mail()
        return res
