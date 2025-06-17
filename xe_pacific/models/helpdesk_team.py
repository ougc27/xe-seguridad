from odoo import fields, models, api
from odoo.osv import expression


class HelpdeskTeam(models.Model):
    _inherit = 'helpdesk.team'

    total_tickets = fields.Integer(string='Total Tickets', compute='_compute_total_tickets')
    active_tickets = fields.Integer(compute='_compute_active_tickets')
    inactive_tickets = fields.Integer(compute='_compute_inactive_tickets')
    ticket_type_ids = fields.Many2many(
        'helpdesk.ticket.type', string='Ticket types')

    def _compute_ticket_closed(self):
        ticket_data = self.env['helpdesk.ticket']._read_group([
            ('team_id', 'in', self.ids),
            ('stage_id.name', 'ilike', 'Finalizado')],
            ['team_id'], ['__count'])
        mapped_data = {team.id: count for team, count in ticket_data}
        for team in self:
            team.ticket_closed = mapped_data.get(team.id, 0)

    def _compute_total_tickets(self):
        for team in self:
            team.total_tickets = len(team.ticket_ids)

    def _compute_active_tickets(self):
        ticket_data = self.env['helpdesk.ticket']._read_group([
            ('team_id', 'in', self.ids), ('stage_id.name', 'not ilike', 'Inactivo'), ('stage_id.name', 'not ilike', 'Finalizado')
        ], ['team_id'], ['__count'])
        mapped_data = {team.id: count for team, count in ticket_data}
        for team in self:
            team.active_tickets = mapped_data.get(team.id, 0)
        
    def _compute_inactive_tickets(self):
        ticket_data = self.env['helpdesk.ticket']._read_group([
            ('team_id', 'in', self.ids), ('stage_id.name', 'ilike', 'Inactivo')
        ], ['team_id'], ['__count'])
        mapped_data = {team.id: count for team, count in ticket_data}
        for team in self:
            team.inactive_tickets = mapped_data.get(team.id, 0)

    def action_view_active_tickets(self):
        action = self.action_view_ticket()
        action.update({
            'domain': [
                ('team_id', '=', self.id),
                ('stage_id.name', 'not ilike', 'Inactivo'),
                ('stage_id.name', 'not ilike', 'Finalizado')
            ]
        })
        return action

    def action_view_inactive_ticket(self):
        action = self.action_view_ticket()
        action.update({
            'domain': [
                ('team_id', '=', self.id),
                ('stage_id.name', 'ilike', 'Inactivo'),
            ]
        })
        return action

    def action_view_closed_ticket(self):
        action = self.action_view_ticket()
        action.update({
            'domain': [
                ('team_id', '=', self.id),
                ('stage_id.name', 'ilike', 'Finalizado'),
            ]
        })
        return action