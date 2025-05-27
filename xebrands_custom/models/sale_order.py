# -*- coding: utf-8 -*-

from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    @api.depends('order_line')
    def _compute_to_billing(self):
        for record in self:
            amount_to_billing = 0
            dates = []
            for line in record.order_line.filtered(
                lambda o: not o.product_id.not_calculate_for_billing
            ):
                qty = line.qty_delivered - line.qty_invoiced
                qty_pending = qty if qty > 0 else 0
                qty_delivered = 0
                if qty_pending:
                    tax_results = record.env['account.tax'].with_company(line.company_id)._compute_taxes([
                        line._convert_to_tax_base_line_dict_xebrands(qty_pending)
                    ])
                    totals = list(tax_results['totals'].values())[0]
                    amount_untaxed = totals['amount_untaxed']
                    amount_to_billing += amount_untaxed

                    if record.team_id == record.env.ref('__export__.crm_team_6_c580c7a3'):
                        invoice_picking_ids = record.invoice_ids.picking_ids
                        picking_ids = record.picking_ids.filtered(
                            lambda o: o.state == 'done' and
                                      o.id not in invoice_picking_ids.ids
                        ).sorted(lambda o: o.date_done)
                        for picking_id in picking_ids:
                            dates.append(picking_id.date_done)
                    else:
                        picking_ids = record.picking_ids.filtered(
                            lambda o: o.state == 'done'
                        ).sorted(lambda o: o.date_done)
                        for picking_id in picking_ids:
                            move_id = picking_id.move_ids_without_package.filtered(
                                lambda o: o.product_id == line.product_id and o.sale_line_id.id == line.id
                            )
                            if move_id:
                                qty_delivered += move_id.quantity
                                if qty_delivered > line.qty_invoiced:
                                    if picking_id.date_done not in dates:
                                        dates.append(picking_id.date_done)
                                    break
            if dates:
                dates = sorted(dates)
                record.picking_date_to_billing = dates[0]
            else:
                record.picking_date_to_billing = False
            record.amount_to_billing = amount_to_billing
            record.billing_pending = bool(amount_to_billing)

    billing_pending = fields.Boolean(
        string='Pending billing',
        compute='_compute_to_billing',
    )
    amount_to_billing = fields.Monetary(
        string='Amount to billing',
    )
    picking_date_to_billing = fields.Datetime(
        string='Picking date to billing',
    )
