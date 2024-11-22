# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from werkzeug.exceptions import Forbidden

from odoo import http, fields, _
from odoo.exceptions import ValidationError
from odoo.http import request, route


class ForecastView(http.Controller):

    @route('/xe_customs/forecast/get_picking_locked', type='json', auth='user')
    def get_picking_locked(self, picking_id):
        picking_id = request.env['stock.picking'].browse(picking_id)
        return {
            'is_locked': picking_id.is_locked,
        }
