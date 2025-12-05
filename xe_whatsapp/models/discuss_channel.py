from odoo import fields, models, api, _
from odoo.tools import html2plaintext
from datetime import timedelta


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    assigned_to = fields.Selection([
        ('sales_team', 'Sales team'),
        ('support_team', 'Support team'),
        ('spam', 'Spam'),
        ('closed', 'Closed')],
         help="Message assigned to", default='sales_team')

    first_assigned_person = fields.Many2one(
        'res.users',
        help='First user assigned to the channel'
    )

    first_respond_message = fields.Datetime(string="Primer mensaje cliente", readonly=True)

    user_respond_in_time = fields.Boolean(string="El usuario respondio a tiempo")

    is_open_for_all_users = fields.Boolean(string="Esta abierto a todos los usuarios")

    assigned_person = fields.Many2one(
        'res.users',
        help='Person assigned to the channel'
    )

    whatsapp_channel_active = fields.Boolean('Is Whatsapp Channel Active', compute="_compute_whatsapp_channel_active")

    last_wa_mail_message_id = fields.Many2one(comodel_name="mail.message", string="Last WA Partner Mail Message", index='btree_not_null')
    
    @api.depends('last_wa_mail_message_id')
    def _compute_whatsapp_channel_valid_until(self):
        for channel in self:
            channel.whatsapp_channel_valid_until = channel.last_wa_mail_message_id.create_date + timedelta(hours=24) \
                if channel.channel_type == "whatsapp" and channel.last_wa_mail_message_id else False

    @api.depends('whatsapp_channel_valid_until')
    def _compute_whatsapp_channel_active(self):
        for channel in self:
            channel.whatsapp_channel_active = channel.whatsapp_channel_valid_until and \
                channel.whatsapp_channel_valid_until > fields.Datetime.now()

    @api.model
    def create(self, vals):
        """Modify method of create record.

        Modified the discuss channel name

        :param vals: Dictionary of values to create new record.
        :param type: dict

        :returns: Created record
        :rtype: discuss.channel
        """
        records = super().create(vals)
        for rec in records:
            if rec.channel_type == 'whatsapp':
                rec['name'] = rec.whatsapp_partner_id.name + ' ' + rec.whatsapp_number
        return records

    def write(self, vals):
        res = super().write(vals)
        if vals.get('is_open_for_all_users'):
            for rec in self:
                member_ids = self.env['whatsapp.team.members'].search([
                    ('wa_account_id', '=', rec.wa_account_id.id), 
                    ('team', '=', rec.assigned_to)
                ]).user_id.partner_id.ids
                for channel_member_id in member_ids:
                    if not channel_member_id in rec.channel_partner_ids.ids:
                        self.env['discuss.channel.member'].sudo().create([{
                            'partner_id': channel_member_id,
                            'channel_id': rec.id,
                        }])
        if 'assigned_person' in vals and rec.channel_type == "whatsapp":
            assigned_person = rec.assigned_person
            if assigned_person:
                assigned_partner = assigned_person.partner_id
                for member in rec.channel_member_ids:
                    if member.partner_id == assigned_partner:
                        member.write({'custom_notifications': None})
                    else:
                        member.write({'custom_notifications': 'no_notif'})
            else:
                for member in rec.channel_member_ids:
                    member.write({'custom_notifications': 'no_notif'})
        return res

    def _channel_info(self):
        """
        Override to add visitor information on the mail channel infos.
        This will be used to display a banner with visitor informations
        at the top of the livechat channel discussion view in discuss module.
        """
        channel_infos = super()._channel_info()
        channel_infos_dict = dict((c['id'], c) for c in channel_infos)
        for channel in self:
            if channel.channel_type == 'whatsapp':
                if channel.assigned_person:
                    channel_infos_dict[channel.id]["assigned_person"] = [channel.assigned_person.id, channel.assigned_person.name]
                    channel_infos_dict[channel.id]["is_open"] = False
                else:
                    channel_infos_dict[channel.id]["assigned_person"] = False
                    channel_infos_dict[channel.id]["is_open"] = False
        return list(channel_infos_dict.values())

    def message_post(self, *, message_type='notification', **kwargs):
        new_msg = super().message_post(message_type=message_type, **kwargs)
        if new_msg.author_id == self.whatsapp_partner_id:
            self.last_wa_mail_message_id = new_msg
        return new_msg

    def execute_command_person(self, **kwargs):
        channel = self.env['discuss.channel'].browse(self.id)
        if channel.assigned_person:
            msg = _("Assigned person in this channel: %(person)s", person=channel.assigned_person.name)
        else:
            msg = _("No person assigned to this channel")
        self._send_transient_message(self.env.user.partner_id, msg)

    def _convert_visitor_to_lead(self, partner, key):
        """ Create a lead from channel /lead command
        :param partner: internal user partner (operator) that created the lead;
        :param key: operator input in chat ('/lead Lead about Product')
        """
        # if public user is part of the chat: consider lead to be linked to an
        # anonymous user whatever the participants. Otherwise keep only share
        # partners (no user or portal user) to link to the lead.
        customers = self.env['res.partner']
        for customer in self.with_context(active_test=False).channel_partner_ids.filtered(lambda p: p != partner and p.partner_share):
            if customer.is_public:
                customers = self.env['res.partner']
                break
            else:
                customers |= customer

        utm_source = self.env.ref('crm_livechat.utm_source_livechat', raise_if_not_found=False)

        assigned_person_id = self.assigned_person.id
        company_id = False
        team_id = False
        team_member = self.env['crm.team.member'].get_team_member(assigned_person_id)
        if team_member:
            company_id = team_member['company_id'][0]
            team_id = team_member['crm_team_id'][0]
        
        source_id = self.env['utm.source'].search([('name', 'ilike', 'whatsapp')], limit=1).id
        medium_id = self.env['utm.medium'].search([('name', 'ilike', 'whatsapp')], limit=1).id

        return self.env['crm.lead'].create({
            'name': html2plaintext(key[5:]),
            'partner_id': customers[0].id if customers else False,
            'user_id': assigned_person_id,
            'team_id': team_id,
            'company_id': company_id,
            'description': self._get_channel_history(),
            'referred': partner.name,
            'source_id': source_id,
            'medium_id': medium_id
        })
