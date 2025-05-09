from odoo import api, models
from itertools import groupby
from operator import itemgetter
from datetime import date


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def get_product_info_pos(self, price, quantity, pos_config_id):
        self.ensure_one()
        config = self.env['pos.config'].browse(pos_config_id)

        # Tax related
        taxes = self.taxes_id.compute_all(price, config.currency_id, quantity, self)
        grouped_taxes = {}
        for tax in taxes['taxes']:
            if tax['id'] in grouped_taxes:
                grouped_taxes[tax['id']]['amount'] += tax['amount']/quantity if quantity else 0
            else:
                grouped_taxes[tax['id']] = {
                    'name': tax['name'],
                    'amount': tax['amount']/quantity if quantity else 0
                }

        all_prices = {
            'price_without_tax': taxes['total_excluded']/quantity if quantity else 0,
            'price_with_tax': taxes['total_included']/quantity if quantity else 0,
            'tax_details': list(grouped_taxes.values()),
        }

        # Pricelists
        if config.use_pricelist:
            pricelists = config.available_pricelist_ids
        else:
            pricelists = config.pricelist_id
        price_per_pricelist_id = pricelists._price_get(self, quantity) if pricelists else False
        pricelist_list = [{'name': pl.name, 'price': price_per_pricelist_id[pl.id]} for pl in pricelists]

        # Warehouses
        picking_type_id = config.picking_type_id
        warehouses = config.warehouse_id + picking_type_id.warehouse_id

        if config.warehouse_id == picking_type_id.warehouse_id:
            warehouses = config.warehouse_id
            if config.general_warehouse_id:
                warehouses = config.warehouse_id + config.general_warehouse_id
        
        warehouse_list = []
        for w in warehouses:
            transit_moves = self.env['stock.move.line'].search([
                ('product_id', '=', self.id),
                ('lot_id', '!=', False),
                ('location_id', '=', picking_type_id.default_location_src_id.id),
                ('picking_id.state', '=', 'transit'),
                ('company_id', '=', config.company_id.id),
            ])

            reserved_in_transit = sum(transit_moves.mapped('quantity'))

            warehouse_data = {
                'name': w.name,
                'available_quantity': self.with_context({'warehouse': w.id}).qty_available - reserved_in_transit,
                'forecasted_quantity': self.with_context({'warehouse': w.id}).virtual_available,
                'uom': self.uom_name,
            }

            warehouse_list.append(warehouse_data)

        # Suppliers
        key = itemgetter('partner_id')
        supplier_list = []
        for key, group in groupby(sorted(self.seller_ids, key=key), key=key):
            for s in list(group):
                if not((s.date_start and s.date_start > date.today()) or (s.date_end and s.date_end < date.today()) or (s.min_qty > quantity)):
                    supplier_list.append({
                        'id': s.id,
                        'name': s.partner_id.name,
                        'delay': s.delay,
                        'price': s.price
                    })
                    break

        # Variants
        variant_list = [{'name': attribute_line.attribute_id.name,
                         'values': list(map(lambda attr_name: {'name': attr_name, 'search': '%s %s' % (self.name, attr_name)}, attribute_line.value_ids.mapped('name')))}
                        for attribute_line in self.attribute_line_ids]

        optional_products = [
            {'name': p.name, 'price': min(p.product_variant_ids.mapped('lst_price'))}
            for p in self.optional_product_ids.filtered_domain(self._optional_product_pos_domain())
        ]

        return {
            'all_prices': all_prices,
            'pricelists': pricelist_list,
            'warehouses': warehouse_list,
            'suppliers': supplier_list,
            'variants': variant_list,
            'optional_products': optional_products
        }

    @api.model
    def get_product_quantity(self, picking_id, product_id):
        warehouse_id = self.env['stock.picking.type'].browse(picking_id).warehouse_id
        transit_moves = self.env['stock.move.line'].search([
            ('product_id', '=', product_id),
            ('lot_id', '!=', False),
            ('location_id', '=', warehouse_id.lot_stock_id.id),
            ('picking_id.state', '=', 'transit'),
            ('company_id', '=', warehouse_id.company_id.id),
        ])
        reserved_in_transit = sum(transit_moves.mapped('quantity'))
        qty_available = self.browse(product_id).with_context(
            {'warehouse': warehouse_id.id}).qty_available - reserved_in_transit
        virtual_available = self.browse(product_id).with_context(
            {'warehouse': warehouse_id.id}).virtual_available
        return qty_available, virtual_available
