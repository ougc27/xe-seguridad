# -*- coding: utf-8 -*-
#############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author:Anjhana A K(<https://www.cybrosys.com>)
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    """Inherited res configuration setting for adding fields for
                restricting out-of-stock products"""
    _inherit = 'res.config.settings'

    is_display_stock = fields.Boolean(related="pos_config_id.is_display_stock",
                                      string="Display Stock in POS",
                                      readonly=False,
                                      help="Enable if you want to show the "
                                           "quantity of products.")
    is_restrict_product = fields.Boolean(
        related="pos_config_id.is_restrict_product",
        string="Restrict Product Out of Stock in POS", readonly=False,
        help="Enable if you want restrict of stock product from POS")
    stock_type = fields.Selection(related="pos_config_id.stock_type",
                                  string="Stock Type", readonly=False,
                                  help="In which quantity type you"
                                       "have to restrict and display in POS")

    pos_general_warehouse_id = fields.Many2one(
        related='pos_config_id.general_warehouse_id')

    pos_delivery_partner_id = fields.Many2one(related='pos_config_id.delivery_partner_id')

    user_ids =  fields.Many2many(related='pos_config_id.res_user_ids', readonly=False)

    pos_allow_outlet2 = fields.Boolean(
        string="Allow Outlet 2",
        related='pos_config_id.allow_outlet2',
        help="If enabled, this point of sale will be able to show the Outlet 2 option."
    )

    pos_tax_id = fields.Many2one(
        'account.tax',
        related='pos_config_id.tax_id',
        readonly=False,
        check_company=True,
        help="Enter the sales tax for the Point of Sale."
    )
