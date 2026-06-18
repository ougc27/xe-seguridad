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

    _sql_constraints = [
        (
            'unique_employee_per_day',
            'UNIQUE(employee_id, date)',
            'This employee already has a meal service record for today.',
        )
    ]
