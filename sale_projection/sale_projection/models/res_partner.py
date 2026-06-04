# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    """
    Extends res.partner to support construction site (obra) hierarchy and SKU portfolio.
    - A construction company (constructora) is the parent partner.
    - Construction sites (obras) are child partners with is_obra = True.
    - Each obra can have a dedicated warehouse and a portfolio of suggested SKUs.
    """
    _inherit = 'res.partner'

    # ------------------------------------------------------------------
    # Construction site identifier
    # ------------------------------------------------------------------
    is_obra = fields.Boolean(
        string='Is Construction Site (Obra)',
        default=False,
        help='Mark this contact as a construction site (obra). '
             'The parent contact should be the construction company.',
    )

    # ------------------------------------------------------------------
    # Warehouse assigned to this obra for automatic SO routing
    # ------------------------------------------------------------------
    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse',
        string='Default Warehouse',
        help='When a Sales Order is created for this obra, '
             'this warehouse will be used as the default dispatch warehouse.',
    )

    # ------------------------------------------------------------------
    # SKU portfolio: suggested products for this obra/partner
    # Acts as a "suggested portfolio" — not a hard restriction.
    # ------------------------------------------------------------------
    projection_product_ids = fields.Many2many(
        comodel_name='product.product',
        relation='partner_projection_product_rel',
        column1='partner_id',
        column2='product_id',
        string='Projection SKU Portfolio',
        domain=[('sale_ok', '=', True)],
        help='Products included in the weekly projection board for this partner. '
             'This is a suggested portfolio; additional SKUs can be added on the fly.',
    )

    # ------------------------------------------------------------------
    # Shortcut: count of active projection records
    # ------------------------------------------------------------------
    projection_count = fields.Integer(
        string='Projections',
        compute='_compute_projection_count',
    )

    def _compute_projection_count(self):
        """Count projection records linked to this partner."""
        ProjectionLine = self.env['sale.projection.line']
        for partner in self:
            partner.projection_count = ProjectionLine.search_count(
                [('partner_id', '=', partner.id)]
            )

    def action_view_projections(self):
        """Open the projection board filtered by this partner."""
        return {
            'type': 'ir.actions.act_window',
            'name': f'Projection – {self.name}',
            'res_model': 'sale.projection.line',
            'view_mode': 'tree,form',
            'domain': [('partner_id', '=', self.id)],
            'context': {
                'default_partner_id': self.id,
                'search_default_partner_id': self.id,
            },
        }
