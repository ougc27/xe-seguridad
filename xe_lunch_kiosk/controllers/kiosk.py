from odoo import http, fields
from odoo.http import request
from odoo.exceptions import AccessError


class LunchKioskController(http.Controller):

    @http.route('/lunch/kiosk', type='http', auth='user', website=False)
    def kiosk_index(self, **kwargs):
        """
        Kiosk page – restricted to Lunch Kiosk Administrators.
        Regular users (group_xe_lunch_kiosk_user) are redirected to /web.
        """
        if not request.env.user.has_group(
                'xe_lunch_kiosk.group_xe_lunch_kiosk_admin'):
            return request.redirect('/web')
        return request.render('xe_lunch_kiosk.kiosk_template', {})

    @http.route('/lunch/kiosk/scan', type='json', auth='user', csrf=False)
    def kiosk_scan(self, barcode, **kwargs):
        """
        Barcode scan endpoint – restricted to Lunch Kiosk Administrators.
        The kiosk page is only reachable by admins, so this is a second
        layer of defence.
        """
        if not request.env.user.has_group(
                'xe_lunch_kiosk.group_xe_lunch_kiosk_admin'):
            raise AccessError(
                "You must be a Lunch Kiosk Administrator to use this feature."
            )

        if not barcode:
            return {'status': 'not_found'}

        employee = request.env['hr.employee'].sudo().search(
            [('barcode', '=', barcode.strip())], limit=1)
        if not employee:
            return {'status': 'not_found'}

        today = fields.Date.context_today(employee)
        Register = request.env['xe.lunch.register'].sudo()

        existing = Register.search([
            ('employee_id', '=', employee.id),
            ('date', '=', today),
        ], limit=1)
        if existing:
            return {
                'status': 'already_registered',
                'name': employee.name,
            }

        Register.create({
            'employee_id': employee.id,
            'date': today,
            'timestamp': fields.Datetime.now(),
        })

        return {
            'status': 'ok',
            'name': employee.name,
        }
