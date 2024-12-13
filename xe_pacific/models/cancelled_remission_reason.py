from odoo import fields, models


class CancelledRemissionReason(models.Model):
    _name = 'cancelled.remission.reason'
    _description = 'Cancelled Remission Reasons'

    name = fields.Char('Reason Name', index='trigram', required=True)
    team = fields.Selection([
        ('construction', 'Construction'),
        ('final_client', 'Final Client')],
        string='Applicable To', required=True
    )
