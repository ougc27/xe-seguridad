from odoo import models, api


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model
    def get_pos_permission(self, user_id):
        user = self.sudo().browse(user_id)
        response = False
        if user.has_group('pos_restrict_product_stock.group_pos_advanced_user'):
            response = True
        return response
