from odoo import http, _
from odoo.http import request
from markupsafe import Markup


class WhatsAppAssignedPerson(http.Controller):
    
    @http.route('/xe_whatsapp/get_assigned_persons', type='json', auth='user')
    def get_assigned_persons(self, channel_id):
        channel = request.env['discuss.channel'].browse(channel_id)
        assigned_person = channel.assigned_person
        available_persons = request.env['whatsapp.team.members'].search([
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
        channel = request.env['discuss.channel'].browse(channel_id)
        user = request.env['res.users'].browse(person_id)
        original_assigned = channel.assigned_person
        if channel and user:
            if user:
                channel.assigned_person = user.id
                notification = Markup('<div class="o_mail_notification">%s</div>') % (
                    _('Last person assigned %s / New person assigned %s') % (
                        original_assigned.partner_id.name, 
                        user.partner_id.name
                    )
                )                
                channel.message_post(body=notification, message_type="notification", subtype_xmlid="mail.mt_comment")
                return True
        return False