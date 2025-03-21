from odoo import fields, models, api


class WhatsappTeamMember(models.Model):
    _name = 'whatsapp.team.members'
    _description = 'WhatsApp Team Members'

    user_id = fields.Many2one(
        'res.users',
        help='Users assigned to the whatsapp account',
        required=True)

    wa_account_id = fields.Many2one(
        comodel_name='whatsapp.account',
        string="WhatsApp Business Account",
        required=True)

    team = fields.Selection([
        ('sales_team', 'Sales team'),
        ('support_team', 'Support team')],
        required=True,
        help="Team where the user is assigned")

    #can_see_all_conversations = fields.Boolean(
        #default=False,
        #help="Can see all the conversations of the whatsapp account")

    assignment_count = fields.Integer(string="Number of conversations assigned")

    is_assigned = fields.Boolean(string="Has assigned equipment", default=False)

    """def assign_person_to_chat_aleatory(self, wa_account_id, sales_team):
        records = self.env['whatsapp.team.members'].search([
            ('team', '=', sales_team),
            ('is_assigned', '=', False)
        ])

        user_ids = records.mapped('user_id.id')
        attendance_open = self.env['hr.attendance'].search([
            ('employee_id.user_id', 'in', user_ids),
            ('check_out', '=', False)
        ]).mapped('employee_id.user_id.id')
    
        active_records = records.filtered(lambda rec: rec.user_id.id in attendance_open)
        
        if not active_records:
            return None
    
        next_register = active_records.sorted(key=lambda r: r.assignment_count)[0]
        next_register.assignment_count += 1
    
        master_ids = self.env['whatsapp.team.members'].search([
            ('team', '=', sales_team),
            ('is_assigned', '=', True)
        ]).mapped('user_id.partner_id')

        partners = next_register.user_id.partner_id + master_ids
        
        return partners"""

    def assign_person_to_chat_aleatory(self, wa_account_id, sales_team):
        records = self.env['whatsapp.team.members'].search([
            ('team', '=', sales_team),
            ('is_assigned', '=', False)
        ])

        user_ids = records.mapped('user_id.id')
        attendance_open = self.env['hr.attendance'].search([
            ('employee_id.user_id', 'in', user_ids),
            ('check_out', '=', False)
        ]).mapped('employee_id.user_id.id')
    
        active_records = records.filtered(lambda rec: rec.user_id.id in attendance_open)
        
        if not active_records:
            return None
    
        next_register = active_records.sorted(key=lambda r: r.assignment_count)[0]
        next_register.assignment_count += 1

        return next_register.user_id.id
        