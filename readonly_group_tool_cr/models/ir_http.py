# -*- coding: utf-8 -*-
# Part of Odoo Module Developed by Candidroot Solutions Pvt. Ltd.
# See LICENSE file for full copyright and licensing details.
from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        result = super().session_info()
        group_ids = self.env['res.groups'].search([('users', '=', result.get('uid'))])
        data = []
        xml_ids = models.Model._get_external_ids(group_ids)
        for id in xml_ids:
            data.append(xml_ids.get(id)[0])
        result['user_has_groups_readonly'] = data
        return result
