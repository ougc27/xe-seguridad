# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models, tools, api
from odoo.osv import expression

import re

class ProductProduct(models.Model):
    _inherit = "product.product"

    def _prepare_clients(self, params=False):
        return self.client_ids.filtered(lambda s: s.name.active).sorted(lambda s: (s.sequence, -s.min_qty, s.price, s.id))

    def _get_filtered_clients(self, partner_id=False, quantity=0.0, date=None, uom_id=False, params=False):
        self.ensure_one()
        if date is None:
            date = fields.Date.context_today(self)
        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')

        clients_filtered = self._prepare_clients(params)
        clients_filtered = clients_filtered.filtered(lambda s: not s.company_id or s.company_id.id == self.env.company.id)
        clients = self.env['product.clientinfo']
        for client in clients_filtered:
            # Set quantity in UoM of client
            quantity_uom_client = quantity
            if quantity_uom_client and uom_id and uom_id != client.product_uom:
                quantity_uom_client = uom_id._compute_quantity(quantity_uom_client, client.product_uom)

            if client.date_start and client.date_start > date:
                continue
            if client.date_end and client.date_end < date:
                continue
            if partner_id and client.name not in [partner_id, partner_id.parent_id]:
                continue
            if quantity is not None and tools.float_compare(quantity_uom_client, client.min_qty, precision_digits=precision) == -1:
                continue
            if client.product_id and client.product_id != self:
                continue
            clients |= client
        return clients

    def _select_client(self, partner_id=False, quantity=0.0, date=None, uom_id=False, params=False):
        clients = self._get_filtered_clients(partner_id=partner_id, quantity=quantity, date=date, uom_id=uom_id, params=params)
        res = self.env['product.clientinfo']
        for client in clients:
            if not res or res.name == client.name:
                res |= client
        return res and res.sorted('price')[:1]

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        domain = domain or []

        product_ids = super(ProductProduct, self)._name_search(name, domain, operator, limit, order)

        if name and self._context.get('partner_id'):
            clients_ids = self.env['product.clientinfo'].search([
                ('name', '=', self._context.get('partner_id')),
                '|',
                ('product_code', operator, name),
                ('product_name', operator, name)
            ]).ids

            if clients_ids:
                additional_ids = self._search(
                    [('product_tmpl_id.client_ids', 'in', clients_ids)], 
                    limit=limit, 
                    order=order
                )
                product_ids = list(set(product_ids) | set(additional_ids))

        return product_ids
