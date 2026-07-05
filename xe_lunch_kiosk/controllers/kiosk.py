from odoo import http, fields
from odoo.http import request


class LunchKioskController(http.Controller):

    # ──────────────────────────────────────────────────────────────────────
    # Public kiosk page  (no login required)
    # ──────────────────────────────────────────────────────────────────────
    @http.route('/lunch/kiosk', type='http', auth='public', website=False)
    def kiosk_index(self, **kwargs):
        """Render the standalone kiosk page (accessible without login)."""
        config = request.env['xe.lunch.config'].sudo().get_singleton()
        return request.render('xe_lunch_kiosk.kiosk_template', {
            'barcode_source': config.barcode_source,   # 'scanner' | 'front' | 'back'
        })

    # ──────────────────────────────────────────────────────────────────────
    # Public scan endpoint  (no login required)
    # ──────────────────────────────────────────────────────────────────────
    @http.route('/lunch/kiosk/scan', type='json', auth='public', csrf=False)
    def kiosk_scan(self, barcode, **kwargs):
        """
        Register a meal for the employee identified by *barcode*.

        Runs entirely with sudo() because the route is public.  No sensitive
        data is exposed: the response only contains a status and the
        employee's display name.
        """
        if not barcode or not barcode.strip():
            return {'status': 'not_found'}

        env = request.env(su=True)   # Odoo 17 recommended way to sudo

        employee = env['hr.employee'].search(
            [('barcode', '=', barcode.strip())], limit=1)
        if not employee:
            return {'status': 'not_found'}

        today = fields.Date.context_today(employee)

        existing = env['xe.lunch.register'].search([
            ('employee_id', '=', employee.id),
            ('date', '=', today),
        ], limit=1)
        if existing:
            return {
                'status': 'already_registered',
                'name': employee.name,
            }

        env['xe.lunch.register'].create({
            'employee_id': employee.id,
            'date': today,
            'timestamp': fields.Datetime.now(),
            'company_id': employee.company_id.id,
        })

        return {
            'status': 'ok',
            'name': employee.name,
        }
