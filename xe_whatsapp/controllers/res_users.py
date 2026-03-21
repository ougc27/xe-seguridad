from odoo import http
from odoo.http import request

class ResUsers(http.Controller):

    @http.route('/xe_whatsapp/user/sudo_write_is_available', type='json', auth='user')
    def toggle_user_status(self, user_id, is_available):
        user = request.env['res.users'].sudo().browse(user_id)
        telemarketing_leader = request.env['crm.team'].sudo().search_count(
            [('user_id', '=', user.id), ('id', '=', 12)])
        if telemarketing_leader:
            user.write({'is_available': is_available})
