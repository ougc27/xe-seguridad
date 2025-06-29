from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    heb_seller = fields.Char(string="Seller")
