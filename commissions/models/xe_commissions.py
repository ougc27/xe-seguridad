# coding: utf-8

from odoo import api, fields, models


class XeCommissions(models.Model):
    _name = 'xe.commissions'
    _description = 'Commissions'
    _order = 'customer_id, collected_date, payment_id desc, pos_order_id, position'

    position = fields.Integer(
        string='Position',
    )
    agent_id = fields.Many2one(
        comodel_name='xe.agent',
        string='Agent',
    )
    move_ids = fields.Many2many(
        comodel_name='account.move',
        string='Invoices',
    )
    collected_date = fields.Date(
        string='Collected date',
    )
    customer_id = fields.Many2one(
        comodel_name='res.partner',
        string='Customer',
    )
    collected = fields.Monetary(
        string='Collected'
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
    )
    commission = fields.Monetary(
        string='Commissions',
    )
    paid = fields.Boolean(
        string='Paid',
    )
    payment_commission_id = fields.Many2one(
        comodel_name='xe.payment.commissions',
        string='Payment commissions',
    )
    sale_order_ids = fields.Many2many(
        comodel_name='sale.order',
        string='Sale orders',
    )
    pos_order_id = fields.Many2one(
        comodel_name='pos.order',
        string='PoS orders',
    )
    payment_id = fields.Many2one(
        comodel_name='account.payment',
        string='Number',
    )
    agent_per = fields.Float(
        string='Agent percentage',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        default=lambda self: self.env.company,
    )

    def action_delete(self):
        if self._context.get('params', False):
            model = self._context['params'].get('model', False)
            if model and model == 'xe.payment.commissions' and id:
                self.payment_commission_id.write({
                    'commissions_ids': [(3, self.id)]
                })
                self.payment_commission_id = False

    def get_sales(self):
        self.sale_order_ids = [(6, 0, self.move_ids.line_ids.sale_line_ids.order_id.ids)]
