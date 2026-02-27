import json
from odoo import api, fields, models, _


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

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

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        default=lambda self: self._default_warehouse_id(),
        domain=lambda self: self._get_warehouse_domain()
    )

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        domain=[('available_in_pos', '=', True), ('pos_categ_ids.name', 'ilike', 'PH')],
    )

    pos_config_id = fields.Many2one(
        'pos.config',
        string='POS',
        readonly=True,
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

    price_from_pricelist = fields.Float(
        compute="_compute_price_from_pricelist",
        readonly=True,
        store=True
    )

    used_date = fields.Datetime(
        string="Used On",
        related="source_pos_order_id.date_order",
        store=True,
        readonly=True,
    )

    externally_managed = fields.Boolean(
        string="Externally Managed Coupon",
        help="If enabled, coupons for this coupon are managed externally and "
            "cannot be generated or edited manually."
    )

    program_name = fields.Char(compute="_compute_program_name")

    @api.depends('program_id.name', 'damage_type')
    def _compute_program_name(self):
        for record in self:
            base_name = record.program_id.name
            record.program_name = (
                f"{base_name} - {_(record.damage_type.replace('_', ' ').title())}"
                if record.damage_type
                else base_name
            )

    @api.depends('pricelist_id', 'product_id')
    def _compute_price_from_pricelist(self):
        for record in self:
            if record.pricelist_id and record.product_id:
                record.price_from_pricelist = record.pricelist_id._get_product_price(
                    record.product_id,
                    1.0,
                    False,
                )
            else:
                record.price_from_pricelist = 0.0

    @api.model
    def create(self, vals):
        records = super().create(vals)
        for rec in records:
            if rec.program_id.externally_managed:
                rec.sudo().write({'externally_managed': True})
        return records
