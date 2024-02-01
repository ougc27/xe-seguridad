# -*- coding: utf-8 -*-

from odoo import fields, models

    
class AccountTax(models.Model):
    _inherit = 'account.tax'

    impuesto = fields.Selection(selection=[('002', 'IVA'),
                                           ('003', ' IEPS'),
                                           ('001', 'ISR'),], string='Impuesto')
