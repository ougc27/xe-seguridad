# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import http, _
from odoo.http import request, route


class ForecastView(http.Controller):

    @route('/xe_customs/forecast/get_picking_locked', type='json', auth='user')
    def get_picking_locked(self, picking_id):
        return {
            'is_locked': request.env['stock.picking'].browse(picking_id).is_locked,
        }
