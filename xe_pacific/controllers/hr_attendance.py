from odoo import http
from odoo.http import request
from odoo.tools import float_round
from odoo.tools.image import image_data_uri
from odoo.addons.hr_attendance.controllers.main import HrAttendance


class HrAttendanceInherit(HrAttendance):

    @staticmethod
    def _get_user_attendance_data(employee):
        response = {}
        if employee:
            display_systray = False
            if request.env.user.has_group('xe_pacific.can_see_assistance_button_group'):
                display_systray = employee.company_id.attendance_from_systray
            response = {
                'id': employee.id,
                'hours_today': float_round(employee.hours_today, precision_digits=2),
                'hours_previously_today': float_round(employee.hours_previously_today, precision_digits=2),
                'last_attendance_worked_hours': float_round(employee.last_attendance_worked_hours, precision_digits=2),
                'last_check_in': employee.last_check_in,
                'attendance_state': employee.attendance_state,
                'display_systray': display_systray
            }
        return response

    @http.route('/hr_attendance/attendance_user_data', type="json", auth="user")
    def user_attendance_data(self):
        employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        return self._get_user_attendance_data(employee)

    @http.route('/hr_attendance/attendance_barcode_scanned', type="json", auth="public")
    def scan_barcode(self, token, barcode):
        company = self._get_company(token)
        if company:
            employee = request.env['hr.employee'].sudo().search([('barcode', '=', barcode)], limit=1)
            if employee:
                employee._attendance_action_change(self._get_geoip_response('kiosk'))
                return self._get_employee_info_response(employee)
        return {}

    @http.route(["/hr_attendance/<token>"], type='http', auth='public', website=True, sitemap=True)
    def open_kiosk_mode(self, token):
        company = self._get_company(token)
        if not company:
            return request.not_found()
        else:
            employee_list = [{"id": e["id"],
                              "name": e["name"],
                              "avatar": image_data_uri(e["avatar_256"]),
                              "job": e["job_id"][1] if e["job_id"] else False,
                              "department": {"id": e["department_id"][0] if e["department_id"] else False,
                                             "name": e["department_id"][1] if e["department_id"] else False
                                             }
                              } for e in request.env['hr.employee'].sudo().search_read(fields=["id",
                                                                                               "name",
                                                                                               "avatar_256",
                                                                                               "job_id",
                                                                                               "department_id"])]
            departement_list = [{'id': dep["id"],
                                 'name': dep["name"],
                                 'count': dep["total_employee"]
                                 } for dep in request.env['hr.department'].sudo().search_read(fields=["id",
                                                                                                      "name",
                                                                                                      "total_employee"])]
            request.session.logout(keep_db=True)
            return request.render(
                'hr_attendance.public_kiosk_mode',
                {
                    'kiosk_backend_info': {
                        'token': token,
                        'company_id': company.id,
                        'company_name': company.name,
                        'employees': employee_list,
                        'departments': departement_list,
                        'kiosk_mode': company.attendance_kiosk_mode,
                        'barcode_source': company.attendance_barcode_source,
                        'lang': company.partner_id.lang,
                    },
                }
            )
