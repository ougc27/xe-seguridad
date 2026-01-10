from odoo import http, fields
from odoo.http import request

class WhatsAppAssignedPerson(http.Controller):
    
    @http.route('/xe_whatsapp/get_assigned_persons', type='json', auth='user')
    def get_assigned_persons(self, channel_id):
        channel = request.env['discuss.channel'].browse(channel_id)
        assigned_person = channel.assigned_person
        available_persons = request.env['whatsapp.team.members'].sudo().search([
            ('team', '=', channel.assigned_to),
            ('user_id.active', '=', True),
            ('wa_account_id', '=', channel.wa_account_id.id)
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
                if channel.assigned_person:
                    lost_count = request.env['whatsapp.reassignment.log'].sudo().search(
                        [
                            ('lost_by_user_id', '=', channel.assigned_person.id),
                            ('wa_account_id', '=', channel.wa_account_id.id),
                        ],
                        order="id DESC",
                        limit=1
                    ).lost_count

                    if not lost_count:
                        lost_count = 0

                    request.env['whatsapp.reassignment.log'].sudo().create({
                        'lost_by_user_id': channel.assigned_person.id,
                        'assigned_to_user_id': user.id,
                        'whatsapp_number': channel.whatsapp_number,
                        'wa_account_id': channel.wa_account_id.id,
                        'reassigned_at': fields.Datetime.now(),
                        'lost_count': lost_count + 1,
                        'reassignment_type': 'manual'
                    })
                channel.assigned_person = user.id
                channel.whatsapp_partner_id.update_whatsapp_partner(user.id)
                return True
        return False

    @http.route('/xe_whatsapp/reload', type='json', auth='user')
    def reload(self, channel_id):
        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if channel:
            channel.reload()
            channel._broadcast(channel.channel_member_ids.partner_id.ids)
