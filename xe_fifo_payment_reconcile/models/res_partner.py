from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Bandera genérica por contacto: el proceso la lee siempre sobre el
    # contacto comercial del pago, nunca con IDs fijos en el código.
    fifo_auto_reconcile = fields.Boolean(
        string='FIFO Auto-Reconcile', tracking=True, copy=False,
        help="When enabled on the commercial partner, its released customer "
             "payments are applied automatically against its oldest open "
             "PUE invoices (FIFO), in background batches.",
    )
