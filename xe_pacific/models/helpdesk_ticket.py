from odoo import models, fields, api, _


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
        ('paint', 'Pintura'),
        ('complaint', 'Queja'),
        ('sale', 'Venta'),
        ('technical_visit', 'VT'),
    ], required=True)

    call_ids = fields.One2many('helpdesk.call', 'ticket_id', string='Calls')

    remission_id = fields.Many2one('stock.picking',
        string="Folio de remisión",
        context={'from_helpdesk_ticket': True},
        tracking=True
    )

    sale_order_id = fields.Many2one(
        'sale.order', 
        tracking=True,
        domain="[('company_id', '=', company_id)]",
        groups="sales_team.group_sale_salesman,account.group_account_invoice"
    )

    ticket_ref = fields.Char(string='Ticket IDs Sequence', readonly=False, copy=False, index=True)

    warehouse_id = fields.Many2one('stock.warehouse',
        tracking=True,
        help='Warehouse where the ticket will be processed')

    zone = fields.Char()

    channel = fields.Char()

    xe_priority = fields.Selection([
        ('normal', 'Normal'),
        ('low', 'Low'),
        ('high', 'High'),
        ('very_high', 'Very High'),
    ], help="""1 Star: First low-impact report, such as a scratch on the door.
               2 Stars: A reprogramming by XE.
               3 Stars: Urgent, client locked up for example, to be attended to on the same day.""",
     tracking=True)

    remission_date = fields.Datetime(
        related='remission_id.date_done', 
        readonly=True,
        help="Date at which the transfer has been processed or cancelled."
    )

    is_incidence = fields.Boolean()

    contact_address = fields.Char(
        related='partner_id.contact_address_complete',
        string='Complete Contact Address',
        readonly=True,
        help="Complete contact address of the partner."
    )

    partner_phone = fields.Char(
        related='partner_id.phone',
        string='Partner Phone',
        readonly=True,
        help="Phone number of the partner."
    )

    product_id = fields.Many2one('product.template', string="Reported product", tracking=True)

    living_type = fields.Selection([
        ('for_delivery', 'For Delivery'),
        ('final_customer', 'Final Customer'),
        ('on_construction', 'On Construction'),
        ('post_sale', 'Post Sale')
    ])

    architect_name = fields.Char()

    construction_phone = fields.Char(string="Phone")

    batch = fields.Char()

    block = fields.Char()

    house_number = fields.Char()

    mechanism_review = fields.Boolean()

    pending_part_delivery = fields.Boolean()

    pending_part_installation = fields.Boolean()

    scratched_or_rusted_steel_profile = fields.Boolean()

    poor_masonry_work = fields.Boolean()

    door_reinstallation = fields.Boolean()

    door_installation = fields.Boolean()

    door_change = fields.Boolean()

    smart_lock_replacement = fields.Boolean()

    collection = fields.Boolean()

    client_access_issue = fields.Boolean()

    determination = fields.Boolean()

    paint_review = fields.Boolean()

    patch_and_paint = fields.Boolean()

    paint_touch_up = fields.Boolean()

    painting_service = fields.Boolean()

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
                'default_description': self.description,
                'dialog_size': 'medium',
            }
        }

    @api.depends('name', 'ticket_type')
    def _compute_ticket_type_name(self):
        for ticket in self:
            ticket.name = dict(self._fields['ticket_type'].selection).get(ticket.ticket_type)

    def _check_tags_and_update_stage(self, ticket_numbers):
        """Checks if tags contain specific keywords and updates the stage."""
        keywords = ['Soporte CI', 'Dictamen']
        for index, rec in enumerate(self):
            if rec.tag_ids.filtered(lambda record: any(keyword in record.name for keyword in keywords)):
                stage_name = 'Dictamen'
            else:
                stage_name = 'Activo por Programar'

            stage = self.env['helpdesk.stage'].search(
                [('name', 'ilike', stage_name)], limit=1
            )
            if stage:
                rec.write({'stage_id': stage.id})
            if ticket_numbers:
                rec.write({'ticket_ref': ticket_numbers[index]})

    @api.model_create_multi
    def create(self, list_value):
        now = fields.Datetime.now()
        # determine user_id and stage_id if not given. Done in batch.
        teams = self.env['helpdesk.team'].browse([vals['team_id'] for vals in list_value if vals.get('team_id')])
        team_default_map = dict.fromkeys(teams.ids, dict())
        for team in teams:
            team_default_map[team.id] = {
                'stage_id': team._determine_stage()[team.id].id,
                'user_id': team._determine_user_to_assign()[team.id].id
            }

        # Manually create a partner now since '_generate_template_recipients' doesn't keep the name. This is
        # to avoid intrusive changes in the 'mail' module
        # TDE TODO: to extract and clean in mail thread
        for vals in list_value:
            partner_id = vals.get('partner_id', False)
            partner_name = vals.get('partner_name', False)
            partner_email = vals.get('partner_email', False)
            if partner_name and partner_email and not partner_id:
                company = False
                if vals.get('team_id'):
                    team = self.env['helpdesk.team'].browse(vals.get('team_id'))
                    company = team.company_id.id
                vals['partner_id'] = self._find_or_create_partner(partner_name, partner_email, company).id

        # determine partner email for ticket with partner but no email given
        partners = self.env['res.partner'].browse([vals['partner_id'] for vals in list_value if 'partner_id' in vals and vals.get('partner_id') and 'partner_email' not in vals])
        partner_email_map = {partner.id: partner.email for partner in partners}
        partner_name_map = {partner.id: partner.name for partner in partners}
        company_per_team_id = {t.id: t.company_id for t in teams}
        ticket_numbers = []
        for vals in list_value:
            company = company_per_team_id.get(vals.get('team_id', False))
            if vals.get('ticket_ref'):
                ticket_numbers.append(vals['ticket_ref'])
            vals['ticket_ref'] = self.env['ir.sequence'].with_company(company).sudo().next_by_code('helpdesk.ticket')
            if vals.get('team_id'):
                team_default = team_default_map[vals['team_id']]
                if 'stage_id' not in vals:
                    vals['stage_id'] = team_default['stage_id']
                # Note: this will break the randomly distributed user assignment. Indeed, it will be too difficult to
                # equally assigned user when creating ticket in batch, as it requires to search after the last assigned
                # after every ticket creation, which is not very performant. We decided to not cover this user case.
                if 'user_id' not in vals:
                    vals['user_id'] = team_default['user_id']
                if vals.get('user_id'):  # if a user is finally assigned, force ticket assign_date and reset assign_hours
                    vals['assign_date'] = fields.Datetime.now()
                    vals['assign_hours'] = 0

            # set partner email if in map of not given
            if vals.get('partner_id') in partner_email_map:
                vals['partner_email'] = partner_email_map.get(vals['partner_id'])
            # set partner name if in map of not given
            if vals.get('partner_id') in partner_name_map:
                vals['partner_name'] = partner_name_map.get(vals['partner_id'])

            if vals.get('stage_id'):
                vals['date_last_stage_update'] = now
            vals['oldest_unanswered_customer_message_date'] = now

        # context: no_log, because subtype already handle this
        tickets = super(HelpdeskTicket, self).create(list_value)

        # make customer follower
        for ticket in tickets:
            if ticket.partner_id:
                ticket.message_subscribe(partner_ids=ticket.partner_id.ids)

            ticket._portal_ensure_token()

        # apply SLA
        tickets.sudo()._sla_apply()
        tickets._check_tags_and_update_stage(ticket_numbers)
        return tickets

    def write(self, vals):
        res = super().write(vals)
        if 'tag_ids' in vals:
            self._check_tags_and_update_stage(False)
        return res
