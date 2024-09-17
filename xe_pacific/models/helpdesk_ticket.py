from odoo import models, fields, api


class HelpdeskTicket(models.Model):
    _inherit = 'helpdesk.ticket'

    name = fields.Char(
        compute="_compute_ticket_type_name",
        string='Subject',
        index=True,
        tracking=True
    )

    ticket_type = fields.Selection([
        ('administration', 'Administración'),
        ('reinstallation', 'Albañilería / Reinstalación'),
        ('information', 'Asesoría / Información'),
        ('ci_change', 'Cambio Ci'),
        ('door_change', 'Cambio de puerta'),
        ('return', 'Devolución / Cancelación'),
        ('survey', 'Encuesta'),
        ('delivery', 'Entrega'),
        ('wrong_part', 'Faltante / Pieza equivocada'),
        ('consumption_output', 'Salida por consumo'),
        ('installation', 'Instalación'),
        ('mechanism', 'Mecanismo'),
        ('other', 'Otro'),
        ('paint', 'Pinura'),
        ('complaint', 'Queja'),
        ('sale', 'Venta'),
        ('technical_visit', 'VT'),
    ], required=True)

    def action_generate_fsm_task(self):
        self.ensure_one()
        if not self.partner_id:
            self.partner_id = self._find_or_create_partner(self.partner_name, self.partner_email, self.company_id.id)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Create a Field Service task'),
            'res_model': 'helpdesk.create.fsm.task',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'use_fsm': True,
                'default_helpdesk_ticket_id': self.id,
                'default_user_id': False,
                'default_partner_id': self.partner_id.id,
                'default_name': self.display_name,
                'default_project_id': self.team_id.fsm_project_id.id,
                'dialog_size': 'medium',
            }
        }

    @api.depends('name', 'ticket_type')
    def _compute_ticket_type_name(self):
        for ticket in self:
            ticket.name = dict(self._fields['ticket_type'].selection).get(ticket.ticket_type)
