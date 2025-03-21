from odoo import models


class TeamMember(models.Model):
    _inherit = 'crm.team.member'

    def get_team_member(self, user_id):
        team_member = self.env['crm.team.member'].sudo().search_read(
            [('user_id', '=', user_id)],
            fields=['company_id', 'crm_team_id'],
            limit=1
        )
        if team_member:
            return team_member[0]
        return False