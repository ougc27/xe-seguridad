from odoo import fields, models, api
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    heb_branch_id = fields.Many2one('addenda_heb.heb_branch', string="HEB Branch",
        help="Branch information for HEB addenda.")
