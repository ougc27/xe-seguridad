from odoo import models, api


class MailFollowers(models.Model):
    _inherit = 'mail.followers'

    @api.model_create_multi
    def create(self, vals_list):
        filtered_vals_list = []
        for vals in vals_list:
            existing_follower = self.search([
                ('partner_id', '=', vals.get('partner_id')),
                ('res_id', '=', vals.get('res_id')),
                ('res_model', '=', vals.get('res_model')),
            ], limit=1)

            if not existing_follower:
                filtered_vals_list.append(vals)

        res = super(MailFollowers, self).create(filtered_vals_list)

        res._invalidate_documents(filtered_vals_list)
        return res
