# -*- coding: utf-8 -*-
from odoo import models, api, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'
    
    addenda_home_depot_seller_id = fields.Char(string='Seller Assigned ID')
