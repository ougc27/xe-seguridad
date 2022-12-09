# -*- coding: utf-8 -*-

import json
from lxml import etree

from odoo import api, models


class BaseModel(models.AbstractModel):
    _inherit = 'base'

    @api.model
    def _fields_view_get(
            self, view_id=None, view_type='form', toolbar=False, submenu=False):
        result = super(BaseModel, self)._fields_view_get(
            view_id=view_id, view_type=view_type, toolbar=toolbar,
            submenu=submenu
        )

        if view_type != 'form' or not self._can_force_readonly():
            return result

        doc = etree.XML(result['arch'])
        for node in doc.xpath("//tree//field[@name='price_unit']"):
            node.set("readonly", "1")
            node.set("force_save", "1")
            node.set('attrs', "{}")
        result['arch'] = etree.tostring(doc, encoding='unicode')
        return result

    def _can_force_readonly(self):
        module_name = 'itechgroup_readonly_unit_price'
        group_mapping = {
            'sale.order': '%s.group_sale_order_line_readonly',
            'purchase.order': '%s.group_purchase_order_line_readonly',
            'account.move': '%s.group_invoice_line_readonly'
        }
        group = group_mapping.get(self._name, False)
        if group and self.env.user.has_group(group % module_name):
            return True
        return False
