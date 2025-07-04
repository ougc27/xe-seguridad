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

    assignment_count = fields.Integer(string="Number of conversations assigned")

    is_assigned = fields.Boolean(string="Has assigned equipment", default=False)
    
    is_available = fields.Boolean(compute="_compute_is_available")

    @api.depends('user_id')
    def _compute_is_available(self):
        for rec in self:
            rec.is_available = rec.user_id.is_available

    def assign_person_to_chat_round_robin(self, wa_account_id, team):
        Config = self.env['ir.config_parameter'].sudo()

        members = self.env['whatsapp.team.members'].search([
            ('team', '=', team),
            ('wa_account_id', '=', wa_account_id.id),
            ('is_assigned', '=', False),
            ('user_id.is_available', '=', True),
            ('user_id.active', '=', 'active')
        ])

        if not members:
            return None

        members = members.sorted(lambda m: m.user_id.id)
        user_ids = members.mapped('user_id.id')

        config_key = f"last_assigned_whatsapp_user_{team}_{wa_account_id.id}"
        last_user_id_str = Config.get_param(config_key)
        last_user_id = int(last_user_id_str) if last_user_id_str and last_user_id_str.isdigit() else None

        if last_user_id in user_ids:
            last_index = user_ids.index(last_user_id)
            next_index = (last_index + 1) % len(user_ids)
        else:
            next_index = 0

        next_user_id = user_ids[next_index]

        Config.set_param(config_key, str(next_user_id))

        user_id = self.env['res.users'].browse(next_user_id)
        team_member = self.env['whatsapp.team.members'].search([('user_id', '=', user_id.id)])
        assignment_count = team_member.assignment_count + 1
        team_member.sudo().write({'assignment_count': assignment_count})
        return user_id
