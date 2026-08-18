import json
from odoo import api, fields, models, _
from odoo.exceptions import AccessError


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    def _get_user_allowed_warehouses(self):
        """Return warehouses"""
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
    
    manual_price = fields.Float(
        string="Manual Price",
        groups="pos_restrict_product_stock.group_loyalty_manager",
        help="Tax-included unit price set manually for a specific use case. "
            "Only visible/editable by users with the Loyalty Manager "
            "access rights. When both a pricelist price and a manual price "
            "are set, the lowest of the two is applied.",
    )

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
                record.price_from_pricelist = record._get_pricelist_price()
            else:
                record.price_from_pricelist = 0.0

    def _get_pricelist_price(self):
        """Price used by the coupon when it carries its own pricelist_id.

        Normally this is the standard (tax-excluded) price computed by
        product.pricelist. However, when the pricelist has the
        'POS Price Tax Included' flag (pos_price_included) enabled, the POS
        sends/expects tax-included unit prices (see product.js get_price /
        pos_price_incl), so the matching pricelist.item's pos_price_incl is
        used instead, keeping both values in the same unit. If that field is
        not set on the matching rule, we fall back to the standard price.
        """
        self.ensure_one()
        pricelist = self.pricelist_id
        product = self.product_id
        standard_price = pricelist._get_product_price(product, 1.0, False)

        if not pricelist.pos_price_included:
            return standard_price

        price_rule = pricelist._compute_price_rule([(product, 1.0, False)])
        rule_id = price_rule.get(product.id, (0, False))[1]
        if not rule_id:
            return standard_price

        item = self.env['product.pricelist.item'].browse(rule_id)
        return item.pos_price_incl or standard_price

    def _check_manager_coupon_restriction(self, vals):
        """Block manual coupon creation on restricted programs.

        When a loyalty program has ``restrict_coupon_creation`` enabled, only
        users in the ``group_loyalty_manager`` group may create its coupons.
        Automated / technical flows (POS issuance, reward generation, data
        loading) run in sudo and are never blocked; only manual creation by
        non-manager users is restricted.
        """
        if self.env.su:
            return
        if self.env.user.has_group(
                'pos_restrict_product_stock.group_loyalty_manager'):
            return
        vals_list = vals if isinstance(vals, list) else [vals]
        program_ids = [v['program_id'] for v in vals_list if v.get('program_id')]
        if not program_ids:
            return
        restricted = self.env['loyalty.program'].browse(program_ids).filtered(
            'restrict_coupon_creation')
        if restricted:
            raise AccessError(_(
                "Coupon creation for this loyalty program is restricted. Only "
                "users in the 'Loyalty & Coupon Programs Manager' group are "
                "allowed to create coupons."
            ))

    @api.model
    def create(self, vals):
        self._check_manager_coupon_restriction(vals)
        records = super().create(vals)
        for rec in records:
            if rec.program_id.externally_managed:
                rec.sudo().write({'externally_managed': True})
        return records
