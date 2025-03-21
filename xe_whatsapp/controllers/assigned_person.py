from odoo import http
from odoo.http import request

class WhatsAppAssignedPerson(http.Controller):
    
    @http.route('/xe_whatsapp/get_assigned_persons', type='json', auth='user')
    def get_assigned_persons(self, channel_id):
        channel = request.env['discuss.channel'].browse(channel_id)
        assigned_person = channel.assigned_person
        available_persons = request.env['whatsapp.team.members'].sudo().search([
            ('team', '=', channel.assigned_to),
            ('user_id.active', '=', True)
        ]).mapped(lambda member: {
            'id': member.user_id.id,
            'name': member.user_id.name
        })

        return {
            'assigned_person': {'id': assigned_person.id, 'name': assigned_person.name} if assigned_person else None,
            'available_persons': available_persons
        }

    @http.route('/xe_whatsapp/set_assigned_person', type='json', auth='user')
    def set_assigned_person(self, channel_id, person_id):
        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        user = request.env['res.users'].sudo().browse(person_id)
        if channel and user:
            if user:
                channel.assigned_person = user.id
                channel.whatsapp_partner_id.update_whatsapp_partner(user.id)
                return True
        return False

    @http.route('/xe_whatsapp/reload', type='json', auth='user')
    def reload(self, channel_id):
        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if channel:
            channel._broadcast(channel.channel_member_ids.partner_id.ids)
