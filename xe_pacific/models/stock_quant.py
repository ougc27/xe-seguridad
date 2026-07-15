from odoo import fields, models


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    lot_blocked = fields.Boolean(
        related='lot_id.blocked',
        string="Blocked",
        store=True,
    )

    def _gather(self, product_id, location_id, lot_id=None, package_id=None,
                owner_id=None, strict=False, qty=0):
        quants = super()._gather(
            product_id, location_id, lot_id=lot_id, package_id=package_id,
            owner_id=owner_id, strict=strict, qty=qty,
        )
        # Exclude blocked lots from reservation candidates, unless the caller
        # explicitly asked for that specific lot (strict move with a lot set).
        if lot_id and lot_id.blocked:
            return quants
        return quants.filtered(lambda q: not q.lot_blocked)
