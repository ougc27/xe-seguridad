from odoo import models, fields, api
from odoo.exceptions import ValidationError


class LunchRegister(models.Model):
    _name = 'xe.lunch.register'
    _description = 'Meal Service Record'
    _order = 'date desc, timestamp desc'

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
        required=True,
        ondelete='restrict',
    )
    date = fields.Date(
        string='Date',
        required=True,
        default=fields.Date.context_today,
    )
    timestamp = fields.Datetime(
        string='Registration Time',
        required=True,
        default=fields.Datetime.now,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    meal_price = fields.Float(
        string='Meal Cost',
        digits=(16, 2),
        readonly=True,
        copy=False,
        help='Meal unit price captured at the moment the record was created. '
             'It is a snapshot: later changes to the configured price do not '
             'affect existing records.',
    )

    _sql_constraints = [
        (
            'unique_employee_per_day',
            'UNIQUE(employee_id, date)',
            'This employee already has a meal service record for today.',
        )
    ]

    @api.onchange('employee_id')
    def _onchange_employee_id(self):
        """When the badge is scanned (or selected) on the list/form view,
        align the record's company with the employee's company."""
        if self.employee_id and self.employee_id.company_id:
            self.company_id = self.employee_id.company_id

    @api.model_create_multi
    def create(self, vals_list):
        """Snapshot the configured meal price on each new record.

        The price is only set at creation time (never via a field default),
        so existing records are left untouched and later changes to the
        configured price only affect records created afterwards.
        """
        default_price = None
        for vals in vals_list:
            if not vals.get('meal_price'):
                if default_price is None:
                    default_price = self.env['xe.lunch.config'].get_meal_price()
                vals['meal_price'] = default_price
        return super().create(vals_list)
