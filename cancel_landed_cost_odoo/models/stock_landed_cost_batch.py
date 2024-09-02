# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
# Copyright MORWI ENCODERS CONSULTING, S.A DE C.V. -- All Right Reserved

from odoo import models


class StockLandedCostsBatch(models.Model):
    _inherit = 'stock.landed.costs.batch'

    
    def button_cancel_validated(self):
        self.landed_costs_ids.action_landed_cost_cancel_form()
        state_value = 'cancel'
        if self.landed_costs_ids:
            state_value = self.landed_costs_ids[0].state
        self.write({'state': state_value})
