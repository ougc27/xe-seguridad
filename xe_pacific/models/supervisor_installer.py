from odoo import fields, models, api


class SupervisorInstaller(models.Model):
    _name = 'supervisor.installer'
    _description = 'Supervisors and Installers'

    name = fields.Char(
        compute="_compute_employee_name",
        string='Subject',
        index=True,
        store=True,
    )

    employee_id = fields.Many2one('hr.employee', string="Employee", help="Employee that is supervisor or installer", required=True)

    installer_number = fields.Integer(string="Installer Number", help="Unique identifier for the installer")
                                     
    supervisor_number = fields.Integer(string="Supervisor Number", help="Unique identifier for the supervisor")

    @api.depends('employee_id')
    def _compute_employee_name(self):
        for line in self:
            line.name = line.employee_id.name
