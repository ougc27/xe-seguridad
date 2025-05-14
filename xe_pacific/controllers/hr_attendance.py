from odoo import http
from odoo.http import request
from odoo.tools import float_round
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
