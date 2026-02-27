# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class LoyaltyGenerateWizard(models.TransientModel):
    _inherit = 'loyalty.generate.wizard'

    def _get_user_allowed_warehouses(self):
        user = self.env.user

        pos_configs = self.env['pos.config'].search([
            ('res_user_ids', 'in', user.id)
        ])

        warehouses = pos_configs.mapped('warehouse_id')
        return warehouses

    def _default_warehouse_id(self):
        warehouses = self._get_user_allowed_warehouses()

        if len(warehouses) == 1:
            return warehouses.id
        return False

    def _get_warehouse_domain(self):
        warehouses = self._get_user_allowed_warehouses()

        if warehouses:
            return [('id', 'in', warehouses.ids)]
        return []

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        domain=[('available_in_pos', '=', True), ('pos_categ_ids.name', 'ilike', 'PH')]
    )

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        ondelete='restrict',
        default=lambda self: self._default_warehouse_id(),
        domain=lambda self: self._get_warehouse_domain()
    )

    damage_type = fields.Selection(
        [
            ('damage_1', 'Damage 1'),
            ('damage_2', 'Damage 2'),
        ],
        string="Damage Type",
        help="Defines if this loyalty program applies to damaged doors."
    )

    pricelist_id = fields.Many2one(
        'product.pricelist',
        string="Pricelist",
        help="Pricelist associated with this loyalty program."
    )

    def _get_coupon_values(self, partner):
        self.ensure_one()
        warehouse_id = self.warehouse_id.id
        pos = self.env['pos.config'].search(
            [('warehouse_id', '=', warehouse_id)],
            limit=1
        )
        return {
            'program_id': self.program_id.id,
            'points': self.points_granted,
            'expiration_date': self.valid_until,
            'partner_id': partner.id if self.mode == 'selected' else False,
            'product_id': self.product_id.id,
            'warehouse_id': warehouse_id,
            'pos_config_id': pos.id,
            'damage_type': self.damage_type,
            'pricelist_id': self.pricelist_id.id
        }

    def generate_coupons(self):
        if any(not wizard.program_id for wizard in self):
            raise ValidationError(_("Can not generate coupon, no program is set."))
        if any(wizard.program_id.externally_managed for wizard in self):
            raise ValidationError(_("Can not generate coupon, program is externally managed."))
        if any(wizard.coupon_qty <= 0 for wizard in self):
            raise ValidationError(_("Invalid quantity."))
        coupon_create_vals = []
        for wizard in self:
            customers = wizard._get_partners() or range(wizard.coupon_qty)
            for partner in customers:
                coupon_create_vals.append(wizard._get_coupon_values(partner))
        self.env['loyalty.card'].create(coupon_create_vals)
        return True
