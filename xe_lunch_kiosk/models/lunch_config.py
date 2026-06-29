from odoo import models, fields, api, _


class LunchConfig(models.Model):
    _name = 'xe.lunch.config'
    _description = 'Meal Service Configuration'

    name = fields.Char(
        string='Name',
        default='Meal Service Configuration',
        readonly=True,
    )
    meal_price = fields.Float(
        string='Meal Price',
        digits=(16, 2),
        required=True,
        default=47.0,
        help='Unit price applied to each new meal service record. '
             'Changing this value only affects records created afterwards; '
             'existing records keep the price they were created with.',
    )

    @api.model
    def get_singleton(self):
        """Return the single configuration record, creating it on first use."""
        config = self.sudo().search([], limit=1)
        if not config:
            config = self.sudo().create({})
        return config

    @api.model
    def get_meal_price(self):
        """Return the currently configured meal unit price."""
        return self.get_singleton().meal_price

    @api.model
    def action_open_settings(self):
        """Open the (singleton) configuration form."""
        config = self.get_singleton()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Meal Service Configuration'),
            'res_model': 'xe.lunch.config',
            'view_mode': 'form',
            'res_id': config.id,
            'target': 'current',
        }
