# coding: utf-8

from odoo import api, fields, models, _


class AccountAccount(models.Model):
    _inherit = 'account.account'

    generates_commission = fields.Boolean(
        string='Generates commission',
    )
