# -*- coding: utf-8 -*-
from odoo import models, api, fields


class ResCompany(models.Model):
    _inherit = 'res.company'
    
    addenda_coppel_party_id = fields.Char(string='ID Addenda Coppel')
