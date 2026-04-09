# coding: utf-8

from odoo import api, fields, models, _


class ResCompany(models.Model):
    _inherit = 'res.company'

    soriana_vendor_number = fields.Char(string="Soriana Vendor Number")
