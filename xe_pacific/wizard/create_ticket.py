# Part of Odoo. See LICENSE file for full copyright and licensing details.
from pprint import pformat
from urllib.parse import urlparse, parse_qs
from odoo import api, fields, models, _

import logging
_logger = logging.getLogger(__name__)

QUESTION_REPRODUCE_ISSUE_STEPS_TXT = _("What are the steps to reproduce your issue?")
QUESTION_CURRENT_BEHAVIOR_TXT = _("What is the current behavior that you observe?")

class CreateTicketAnywhere(models.TransientModel):

    _name = 'create.ticket.anywhere'
    _description = '''Wizard used to allow creting ticket anywhere in the backend'''

    ticket_type_id = fields.Many2one(
        'helpdesk.ticket.type', 'Ticket Type', required=True)

    technical_info = fields.Text(
        help='Info added automatically using environment info(Session, Model, Views, Action).\n'
        ' This info will be useful to IT team at the momento to solve this ticket.')

    ticket_id = fields.Many2one(
        'helpdesk.ticket', 'Ticket',
        help='The new ticket created from this wizard')

    attachment_ids = fields.Many2many(
        'ir.attachment', string='Attachments',
        help='Add files you consider will help us understanding and fixing your problem faster. '
        'Anything can be helpful, image, video, gif, etc.')

    current_model = fields.Char(help='Field to save the current model if there is one')

    current_id = fields.Integer(help='Field to save the current id if there is one')

    question_reproduce_issue_steps = fields.Text(
        help="Field to save answer to question: What are the steps to reproduce your issue?",
        string="What are the steps to reproduce your issue?")

    question_current_behavior = fields.Text(
        help="Field to save answer to question: What is the current behavior that you observe?",
        string="What is the current behavior that you observe?")
    
    ticket_type_id_domain = fields.Binary(compute="_compute_ticket_type_id_domain")

    external_url = fields.Char(
        help="Enter the URL where the issue is occurring.")

    show_external_url = fields.Boolean(compute="_compute_show_external_url")

    ticket_priority = fields.Boolean(help='Field to save the current id if there is one')

    impact_level = fields.Selection([
        ('0', 'Low – Only affects me.'),
        ('1', 'Medium – Affects my entire team.'),
        ('2', 'High – Stops my team and other departments from working.'),
        ('3', 'Critical – Affects the entire company’s operations.'),
    ], help="Select the urgency and scope of the issue based on its operational impact.")

    @api.depends('ticket_type_id')
    def _compute_show_external_url(self):
        for rec in self:
            ticket_type_id = rec.ticket_type_id
            if ticket_type_id:
                rec.show_external_url = ticket_type_id.is_url_needed
                continue
            rec.show_external_url = False

    @api.depends('technical_info')
    def _compute_ticket_type_id_domain(self):
        for rec in self:
            team_id = self.env['ir.model.data'].sudo()._xmlid_to_res_id('xe_pacific.helpdesk_team_support_it')
            team = self.env['helpdesk.team'].sudo().browse(team_id)
            if team and team.ticket_type_ids:
                rec.ticket_type_id_domain = [('id', 'in', team.ticket_type_ids.ids)]
            else:
                rec.ticket_type_id_domain = []

    @api.model
    def default_get(self, nfields):
        """Adding the info sent from the widget to the new wizard"""
        res = super().default_get(nfields)
        _logger.info("Context values in default_get of CreateTicketAnywhere: %s", self.env.context)
        if not self.env.context.get('env_values'):
            return res
        url_params = {}
        current_url = self.sudo().env.context['env_values']['current_url']
        url_object = urlparse(current_url)
        url_params.update(parse_qs(url_object.fragment))
        url_params.update(parse_qs(url_object.query))
        current_model = url_params.get('model', [])
        current_model = ''.join(current_model)
        current_id = url_params.get('id', [])
        current_id = int(current_id[0]) if current_id else False
        self.sudo().env.context['env_values'].update({'url_info': url_params})
        technical_info = pformat(self.sudo().env.context['env_values'], width=80)
        res.update({
            'technical_info': technical_info,
            'current_model': current_model,
            'current_id': current_id,
        })
        return res
    
    def filling_description(self):
        description_lines = []

        detail_fields = [
            ('question_reproduce_issue_steps', _(QUESTION_REPRODUCE_ISSUE_STEPS_TXT)),
            ('question_current_behavior', _(QUESTION_CURRENT_BEHAVIOR_TXT)),
            ('technical_info', _('Technical Info')),
        ]

        for field_path, label in detail_fields:
            value = self
            for attr in field_path.split('.'):
                value = getattr(value, attr, None)
                if not value:
                    break
            if value:
                description_lines.append(f"{label}:")
                description_lines.append(f"{value}")
                description_lines.append('')

        return "<br />".join(description_lines)

    def done(self):
        if self.ticket_id:
            return
        ticket_type_id = self.ticket_type_id
        description = self.filling_description()
        company_it_team = self.env['helpdesk.team'].sudo().search([('is_it_team', '=', True), ('company_id', '=', self.env.company.id)]).id
        payload = {
            'name': 'TI',
            'team_id':  company_it_team,
            'partner_id': self.env.user.partner_id.id,
            'description': description,
            'ticket_type_id': ticket_type_id.id,
            'user_id': ticket_type_id.user_id.id,
            'external_url': self.external_url,
            'priority': self.impact_level
        }
        ticket = self.env['helpdesk.ticket'].sudo().create(payload)

        if self.attachment_ids:
            ticket.sudo().message_post(attachment_ids=self.attachment_ids.ids)

        self.ticket_id = ticket.id

        if self.current_model and self.current_id:
            record = self.env[self.current_model].browse(int(self.current_id))
            self.post_message_ticket(ticket, record)
            ticket.sudo().write({
                'reference': '%s,%s' % (self.current_model, self.current_id)
            })

        action = self.sudo().env.ref('xe_pacific.action_create_ticket_wizard').read()[0]
        action['res_id'] = self.id
        return action

    def post_message_ticket(self, ticket, record):
        """ Method to post a message on any record
        """

        body = self.env['ir.qweb']._render(
            'xe_pacific.ticket_message_origin_link',
            {
                'origin': ticket,
            }
        )
        record.sudo().message_post(
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
