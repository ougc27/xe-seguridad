# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models

class StockPicking(models.Model):
    _inherit = "stock.picking"

    supervisor_id = fields.Many2one('res.users', 'Supervisor', help="Define the supervisor of the picking.")
    installer_id = fields.Many2one('res.users', 'Installer', help="Define the installer of the picking.")
