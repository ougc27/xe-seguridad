from odoo import models, fields


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    employee_number = fields.Integer()
