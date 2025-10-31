from odoo import fields, models, api, _
from odoo.tools import frozendict
from odoo.exceptions import UserError

import logging
_logger = logging.getLogger()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'
    
    x_order_id = fields.Many2one('sale.order',
        related='sale_line_ids.order_id',
        store=True,
        readonly=True
    )

    @api.depends('account_id', 'partner_id', 'product_id', 'x_order_id')
    def _compute_analytic_distribution(self):
        cache = {}
        for line in self:
            if line.display_type == 'product' or not line.move_id.is_invoice(include_receipts=True):
                partner_id = line.partner_id
                if partner_id.parent_id:
                    partner_id = partner_id.parent_id
                team_id = line.move_id.team_id.id
                if line.move_id.purchase_order_count:
                    team_id = None
                warehouse_id = line.x_order_id.warehouse_id.id
                if not line.x_order_id:
                    order = self.env['sale.order'].search([
                        ('name', '=', line.move_id.invoice_origin),
                        ('company_id', '=', line.move_id.company_id.id)
                    ])
                    if order:
                        warehouse_id = order.warehouse_id.id
                    else:
                        warehouse_id = line.move_id.warehouse_id.id
                arguments = frozendict({
                    "product_id": line.product_id.id,
                    "product_categ_id": line.product_id.categ_id.id,
                    "partner_id": partner_id.id,
                    "partner_category_id": partner_id.category_id.ids,
                    "account_prefix": line.account_id.code,
                    "company_id": line.company_id.id,
                    "warehouse_id": warehouse_id,
                    "team_id": team_id
                })
                if arguments not in cache:
                    cache[arguments] = self.env['account.analytic.distribution.model']._get_distribution(arguments)
                line.analytic_distribution = cache[arguments] or line.analytic_distribution

    """def _check_amls_exigibility_for_reconciliation(self, shadowed_aml_values=None):
        if not self:
            return
    
        unreconciled_amls = self.filtered(lambda aml: not aml.reconciled)
    
        if unreconciled_amls:
            if any(aml.parent_state != 'posted' for aml in unreconciled_amls):
                raise UserError(_("You can only reconcile posted entries."))
    
            accounts = unreconciled_amls.mapped(
                lambda x: x._get_reconciliation_aml_field_value('account_id', shadowed_aml_values)
            )
            if len(accounts) > 1:
                raise UserError(_(
                    "Entries are not from the same account: %s",
                    ", ".join(accounts.mapped('display_name')),
                ))
    
            if len(unreconciled_amls.mapped('company_id').root_id) > 1:
                raise UserError(_(
                    "Entries don't belong to the same company: %s",
                    ", ".join(unreconciled_amls.mapped('company_id.display_name')),
                ))
    
            account = accounts[0]
            if not account.reconcile and account.account_type not in ('asset_cash', 'liability_credit_card'):
                raise UserError(_(
                    "Account %s does not allow reconciliation. First change the configuration of this account "
                    "to allow it.",
                    account.display_name,
                ))
    
        else:
            _logger.info("All journal items are already reconciled. Skipping reconciliation checks but allowing invoice creation.")"""
    