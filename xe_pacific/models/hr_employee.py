from odoo import models, fields


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    employee_number = fields.Integer()


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    employee_number = fields.Integer(related='employee_id.employee_number', readonly=True)
