# -*- coding: utf-8 -*-
from odoo import models, api, fields

from lxml import etree


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    addenda_heb_store_id = fields.Many2one(
        'heb.store',
        'HEB Ship to',
    )
