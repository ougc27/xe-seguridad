import pytz
from odoo import fields, models, api, _
from odoo.tools import html2plaintext
from datetime import datetime, timedelta, time


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

    is_open_for_all_users = fields.Boolean(string="Esta abierto a todos los usuarios")

    assigned_person = fields.Many2one(
        'res.users',
        help='Person assigned to the channel'
    )

    whatsapp_channel_active = fields.Boolean('Is Whatsapp Channel Active', compute="_compute_whatsapp_channel_active")

    last_wa_mail_message_id = fields.Many2one(comodel_name="mail.message", string="Last WA Partner Mail Message", index='btree_not_null')

    is_reassigned_computed = fields.Boolean(default=False)

    is_reassigned = fields.Boolean(default=False)

    out_of_working_hours = fields.Boolean(default=False)

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
        if 'assigned_person' in vals:
            for rec in self:
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

    def _get_channels_pending_reassign(self, only_today=True, minutes=10, limit=200):
        """Returns the channels that must be checked:
        - they have a first_respond_message (first customer message)
        - first_respond_message <= now - minutes
        - is_reassigned_computed == False
        - optionally, only today’s channels (only_today=True)
        """
        now = fields.Datetime.now()
        cutoff = now - timedelta(minutes=minutes)
        domain = [
            ('first_respond_message', '!=', False),
            ('first_respond_message', '<=', cutoff),
            ('is_reassigned_computed', '=', False),
            ('out_of_working_hours', '=', False),
            ('assigned_person', '!=', False),
        ]
        if only_today:
            today = fields.Date.today()
            domain.append(('first_respond_message', '>=', today))

        return self.sudo().search(domain, limit=limit, order="create_date desc")

    def _agent_replied_within_window(self, channel, minutes=10):
        """Returns True if the assigned_person sent at least one message within the interval:
        [first_respond_message, first_respond_message + minutes]
        This attempts to cover several message integration schemas (user_id, author_id, from_user).
        """
        start = channel.first_respond_message
        end_dt = fields.Datetime.from_string(start) + timedelta(minutes=minutes)
        Message = self.env['whatsapp.message']
        domain = [
            ('mobile_number', 'ilike', channel.whatsapp_number),
            ('create_uid', '=', channel.assigned_person.id),
            ('create_date', '>=', start),
            ('create_date', '<=', end_dt),
        ]
        return bool(Message.sudo().search(domain, limit=1))

    def _process_single_channel_for_reassign(self, channel, minutes=10):
        """For a channel:
        - if the assigned_person replied within the time window -> mark is_reassigned_computed=True
        - if they did NOT reply -> reassign using assign_person_to_chat_round_robin(...)
            and mark is_reassigned_computed=True
        """
        try:
            agent_replied = self._agent_replied_within_window(channel, minutes=minutes)
        except Exception:
            agent_replied = True

        if agent_replied:
            channel.sudo().write({'is_reassigned_computed': True})
            return

        wa_account = getattr(channel, 'wa_account_id', False)
        user = None
        if wa_account:
            user = self.env['whatsapp.team.members'].assign_person_to_chat_round_robin(wa_account, 'sales_team')
        
        if user == channel.assigned_person:
            user = self.env['whatsapp.team.members'].assign_person_to_chat_round_robin(
                wa_account, 'sales_team'
            )

        vals = {'is_reassigned_computed': True}
        if user:
            vals['assigned_person'] = user.id
            vals['is_reassigned'] = True

            lost_count = self.env['whatsapp.reassignment.log'].sudo().search(
                [
                    ('lost_by_user_id', '=', channel.assigned_person.id),
                    ('wa_account_id', '=', wa_account.id),
                ],
                order="id DESC",
                limit=1
            ).lost_count

            if not lost_count:
                lost_count = 0

            self.env['whatsapp.reassignment.log'].sudo().create({
                'lost_by_user_id': channel.assigned_person.id,
                'assigned_to_user_id': user.id,
                'whatsapp_number': channel.whatsapp_number,
                'wa_account_id': wa_account.id,
                'reassigned_at': fields.Datetime.now(),
                'lost_count': lost_count + 1,
            })
            channel._broadcast(channel.channel_member_ids.partner_id.ids)

        channel.sudo().write(vals)

    def _cron_reassign_unanswered(self, only_today=True, batch_limit=100, minutes=10):
        env = self.with_context(tz='America/Mexico_City')
        now = fields.Datetime.context_timestamp(
            env,
            fields.Datetime.now()
        ).time()
        start = time(8, 40)
        end = time(18, 30)

        if not (start <= now <= end):
            return
        candidates = self._get_channels_pending_reassign(only_today=only_today, minutes=minutes, limit=batch_limit)
        if not candidates:
            return
        for ch in candidates:
            self._process_single_channel_for_reassign(ch, minutes=minutes)

    def _get_channels_out_of_working(self, only_today=True, limit=200):
        """Returns the channels that must be checked:
        - they have a first_respond_message (first customer message)
        - first_respond_message <= now
        - is_reassigned_computed == False
        - optionally, only today’s channels (only_today=True)
        """
        domain = [
            ('first_respond_message', '!=', False),
            ('is_reassigned_computed', '=', False),
            ('out_of_working_hours', '=', True),
            ('assigned_person', '!=', False),
        ]

        if only_today:
            today = fields.Date.today()
            domain.append(('first_respond_message', '>=', today))

        return self.sudo().search(domain, limit=limit, order="create_date desc")

    def _agent_replied_out_of_working(self, channel):
        Message = self.env['whatsapp.message']

        return bool(Message.sudo().search([
            ('mobile_number', 'ilike', channel.whatsapp_number),
            ('create_uid', '=', channel.assigned_person.id),
            ('create_date', '>=', channel.first_respond_message),
            ('create_date', '<=', fields.Datetime.now()),
        ], limit=1))

    def _process_single_channel_out_of_working(self, channel):
        """For a channel:
        - if the assigned_person replied within the time window -> mark is_reassigned_computed=True
        - if they did NOT reply -> reassign using assign_person_to_chat_round_robin(...)
            and mark is_reassigned_computed=True
        """
        try:
            agent_replied = self._agent_replied_out_of_working(channel)
        except Exception:
            agent_replied = True

        if agent_replied:
            channel.sudo().write({'is_reassigned_computed': True})
            return

        wa_account = getattr(channel, 'wa_account_id', False)
        user = None
        if wa_account:
            user = self.env['whatsapp.team.members'].assign_person_to_chat_round_robin(wa_account, 'sales_team')
        
        if user == channel.assigned_person:
            user = self.env['whatsapp.team.members'].assign_person_to_chat_round_robin(
                wa_account, 'sales_team'
            )

        vals = {'is_reassigned_computed': True}
        if user:
            vals['assigned_person'] = user.id
            vals['is_reassigned'] = True

            lost_count = self.env['whatsapp.reassignment.log'].sudo().search(
                [
                    ('lost_by_user_id', '=', channel.assigned_person.id),
                    ('wa_account_id', '=', wa_account.id),
                ],
                order="id DESC",
                limit=1
            ).lost_count

            if not lost_count:
                lost_count = 0

            self.env['whatsapp.reassignment.log'].sudo().create({
                'lost_by_user_id': channel.assigned_person.id,
                'assigned_to_user_id': user.id,
                'whatsapp_number': channel.whatsapp_number,
                'wa_account_id': wa_account.id,
                'reassigned_at': fields.Datetime.now(),
                'lost_count': lost_count + 1,
            })
            channel._broadcast(channel.channel_member_ids.partner_id.ids)

        channel.sudo().write(vals)

    def _cron_reassign_out_of_working(self, only_today=True, batch_limit=100):
        candidates = self._get_channels_out_of_working(only_today=only_today, limit=batch_limit)
        if not candidates:
            return
        for ch in candidates:
            self._process_single_channel_out_of_working(ch)

    def _channel_basic_info(self):
        self.ensure_one()
        res = super()._channel_basic_info()
        res["is_reassigned"] = self.is_reassigned
        return res
