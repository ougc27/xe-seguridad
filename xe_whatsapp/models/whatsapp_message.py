from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta  
from odoo import fields, models, api, Command
from odoo.addons.whatsapp.tools import phone_validation as wa_phone_validation
from odoo.tools import groupby
from odoo.exceptions import ValidationError, UserError
from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError
from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
import markupsafe


class WhatsappMessage(models.Model):
    _inherit = 'whatsapp.message'

    body = fields.Html(related='mail_message_id.body', string="Body", related_sudo=False, index=True, store=True)

    """def change_channel_members(self, channel, team_members):
        wp_partner = channel.channel_member_ids.filtered(lambda rec: not rec.partner_id.user_ids).partner_id
        team_members = team_members + wp_partner
        members_to_keep = channel.channel_member_ids.filtered(lambda rec: rec.partner_id in team_members)
        channel.write({'channel_member_ids': [(6, 0, members_to_keep.ids)]})"""

    def assign_member_to_chat(self, channel, user_id):
        channel.write({
            'assigned_person': user_id,
            'first_assigned_person': user_id
        })
        return user_id.id

    @api.model
    def create(self, vals):
        """Modify method of create record.

        Calculate minutes between responses of salesperson and customer

        :param vals: Dictionary of values to create new record.
        :param type: dict

        :returns: Created record
        :rtype: whatsapp.message
        """
        records = super().create(vals)
        for rec in records:
            first_messages = self.env['whatsapp.message'].search([
                ('mobile_number', '=', rec.mobile_number)
            ], order='create_date ASC', limit=2)
            channel = self.env['discuss.channel'].search(
                [('whatsapp_number', '=', rec.mobile_number_formatted)])
            
            if len(first_messages) == 1 and rec.state == 'received':
                team = 'sales_team'
                channel.write({
                    'assigned_to': team,
                    'first_respond_message': datetime.now()
                })
                user_id = self.env['whatsapp.team.members'].assign_person_to_chat_round_robin(
                    rec.wa_account_id, team)
                if user_id:
                    assigned_person_id = self.assign_member_to_chat(channel, user_id)
                    channel.whatsapp_partner_id.update_whatsapp_partner(assigned_person_id)
                continue
            if rec.state != 'received' and channel.assigned_person == rec.create_uid:
                agent_replied  = self.env['whatsapp.message'].search([
                    ('mobile_number', '=', rec.mobile_number),
                    ('create_uid', '!=', 4),
                ], limit=2)
                if len(agent_replied) == 1:
                    first_message = first_messages[0]
                    self.env['whatsapp.response.metric'].sudo().create({
                        'user_id': rec.create_uid.id,
                        'whatsapp_number': rec.mobile_number,
                        'wa_account_id': rec.wa_account_id.id,
                        'first_customer_message_at': first_message.create_date,
                        'first_agent_message_at': rec.create_date,
                    })
            inactivity_template_enabled = self.env['ir.config_parameter'].sudo().get_param(
                'inactivity_template_enabled', None)
            if rec.state == 'received' and inactivity_template_enabled == "1":
                existing_template_message = self.env['whatsapp.message'].search([
                    ('mobile_number', '=', rec.mobile_number),
                    ('wa_template_id', '!=', False),
                ], limit=1)
                if not existing_template_message:
                    template_name='periodo_inactividad_z'
                    if rec.wa_account_id == 8:
                        template_name = 'periodo_inactividad_t'
                    rec.send_automated_respond(template_name)
        return records

    def _get_html_preview_whatsapp(self, rec, wa_template_id):
        """This method is used to get the html preview of the whatsapp message."""
        self.ensure_one()
        tmpl_vars = wa_template_id.variable_ids
        template_variables_value = tmpl_vars._get_variables_value(rec)
        text_vars = tmpl_vars.filtered(lambda var: var.field_type == 'free_text')
        number_of_free_text_button = len(tmpl_vars.filtered(lambda var: var.field_type == 'free_text' and var.line_type == 'button'))
        return wa_template_id._get_formatted_body(variable_values=template_variables_value)

    def _send_message(self, with_commit=False):
        """ Prepare json data for sending messages, attachments and templates."""
        # init api
        user_id = self.env['res.users'].browse(180)
        message_to_api = {}
        for account, messages in groupby(self, lambda msg: msg.wa_account_id):
            if not account:
                messages = self.env['whatsapp.message'].concat(*messages)
                messages.write({
                    'failure_type': 'unknown',
                    'failure_reason': 'Missing whatsapp account for message.',
                    'state': 'error',
                })
                self -= messages
                continue
            wa_api = WhatsAppApi(account)
            for message in messages:
                message_to_api[message] = wa_api

        for whatsapp_message in self:
            wa_api = message_to_api[whatsapp_message]
            whatsapp_message = whatsapp_message.with_user(user_id)
            if whatsapp_message.state != 'outgoing':
                #_logger.info("Message state in %s state so it will not sent.", whatsapp_message.state)
                continue
            msg_uid = False
            try:
                parent_message_id = False
                body = whatsapp_message.body
                if isinstance(body, markupsafe.Markup):
                    # If Body is in html format so we need to remove html tags before sending message.
                    body = body.striptags()
                number = whatsapp_message.mobile_number_formatted
                if not number:
                    raise WhatsAppError(failure_type='phone_invalid')
                if self.env['phone.blacklist'].sudo().search([('number', 'ilike', number), ('active', '=', True)]):
                    raise WhatsAppError(failure_type='blacklisted')
                if whatsapp_message.wa_template_id:
                    message_type = 'template'
                    #if whatsapp_message.wa_template_id.status != 'approved' or whatsapp_message.wa_template_id.quality in ('red', 'yellow'):
                        #raise WhatsAppError(failure_type='template')
                    whatsapp_message.message_type = 'outbound'
                    if whatsapp_message.mail_message_id.model != whatsapp_message.wa_template_id.model:
                        raise WhatsAppError(failure_type='template')

                    RecordModel = self.env[whatsapp_message.mail_message_id.model].with_user(user_id)
                    from_record = RecordModel.browse(whatsapp_message.mail_message_id.res_id)
                    send_vals, attachment = whatsapp_message.wa_template_id._get_send_template_vals(
                        record=from_record, free_text_json=whatsapp_message.free_text_json,
                        attachment=whatsapp_message.mail_message_id.attachment_ids)
                    if attachment:
                        # If retrying message then we need to remove previous attachment and add new attachment.
                        if whatsapp_message.mail_message_id.attachment_ids and whatsapp_message.wa_template_id.header_type == 'document' and whatsapp_message.wa_template_id.report_id:
                            whatsapp_message.mail_message_id.attachment_ids.unlink()
                        if attachment not in whatsapp_message.mail_message_id.attachment_ids:
                            whatsapp_message.mail_message_id.attachment_ids = [Command.link(attachment.id)]
                elif whatsapp_message.mail_message_id.attachment_ids:
                    attachment_vals = whatsapp_message._prepare_attachment_vals(whatsapp_message.mail_message_id.attachment_ids[0], wa_account_id=whatsapp_message.wa_account_id)
                    message_type = attachment_vals.get('type')
                    send_vals = attachment_vals.get(message_type)
                    if whatsapp_message.body:
                        send_vals['caption'] = body
                else:
                    message_type = 'text'
                    send_vals = {
                        'preview_url': True,
                        'body': body,
                    }
                # Tagging parent message id if parent message is available
                if whatsapp_message.mail_message_id and whatsapp_message.mail_message_id.parent_id:
                    parent_id = whatsapp_message.mail_message_id.parent_id.wa_message_ids
                    if parent_id:
                        parent_message_id = parent_id[0].msg_uid
                msg_uid = wa_api._send_whatsapp(number=number, message_type=message_type, send_vals=send_vals, parent_message_id=parent_message_id)
            except WhatsAppError as we:
                whatsapp_message._handle_error(whatsapp_error_code=we.error_code, error_message=we.error_message,
                                               failure_type=we.failure_type)
            except (UserError, ValidationError) as e:
                whatsapp_message._handle_error(failure_type='unknown', error_message=str(e))
            else:
                if not msg_uid:
                    whatsapp_message._handle_error(failure_type='unknown')
                else:
                    if message_type == 'template':
                        whatsapp_message._post_message_in_active_channel()
                    whatsapp_message.sudo().write({
                        'state': 'sent',
                        'msg_uid': msg_uid
                    })
                if with_commit:
                    self.sudo()._cr.commit()

    def send_automated_respond(self, template_name):
        message_vals = []
        for record in self:
            formatted_number_wa = wa_phone_validation.wa_phone_format(
                record, number=record.mobile_number,
                force_format="WHATSAPP",
            )
            if not formatted_number_wa:
                continue
  
            wa_template = self.env['whatsapp.template'].search(
                [('template_name', '=', template_name)], limit=1)

            body = self._get_html_preview_whatsapp(rec=record.mail_message_id.author_id, wa_template_id=wa_template)

            post_values = {
                'author_id': 94012,
                'attachment_ids': wa_template.header_attachment_ids,
                'body': body,
                'message_type': 'whatsapp_message',
                'partner_ids': hasattr(record, '_mail_get_partners') and record._mail_get_partners()[record.id].ids or record._whatsapp_get_responsible().partner_id.ids,
            }

            message = self.env['mail.message'].create(
                    dict(post_values, res_id=record.id, model='whatsapp.message',
                         subtype_id=self.env['ir.model.data']._xmlid_to_res_id("mail.mt_note"))
            )

            user_id = self.env['res.users'].browse(180)

            message_vals.append({
                    'create_uid': user_id.id,
                    'write_uid': user_id.id,
                    'mail_message_id': message.id,
                    'mobile_number': record.mobile_number,
                    'mobile_number_formatted': formatted_number_wa,
                    'free_text_json': {},
                    'wa_template_id': wa_template.id,
                    'wa_account_id': wa_template.wa_account_id.id,
            })
        if message_vals:
            message = record.env['whatsapp.message'].create(message_vals)
            message.sudo()._send(force_send_by_cron=False)

    @api.autovacuum
    def _gc_whatsapp_messages(self):
        """ To avoid bloating the database, we remove old whatsapp.messages that have been correctly
        received / sent and are older than 15 days.

        We use these messages mainly to tie a customer answer to a certain document channel, but
        only do so for the last 15 days (see '_find_active_channel').

        After that period, they become non-relevant as the real content of conversations is kept
        inside discuss.channel / mail.messages (as every other discussions).

        Impact of GC when using the 'reply-to' function from the WhatsApp app as the customer:
          - We could loose the context that a message is 'a reply to' another one, implying that
          someone would reply to a message after 15 days, which is unlikely.
          (To clarify: we will still receive the message, it will just not give the 'in-reply-to'
          context anymore on the discuss channel).
          - We could also loose the "right channel" in that case, and send the message to a another
          (or a new) discuss channel, but it is again unlikely to answer more than 15 days later. """

        date_threshold = fields.Datetime.now() - timedelta(
            days=self.env['whatsapp.message']._ACTIVE_THRESHOLD_DAYS)
        self.env['whatsapp.message'].search([
            ('create_date', '<', date_threshold),
            ('state', 'in', ['error', 'cancel'])
        ]).unlink()

    @api.model
    def get_messages(self, content):
        today = fields.Date.today()
        three_months_ago = today - relativedelta(months=3)
        query = """
            SELECT mobile_number_formatted, body 
            FROM whatsapp_message
            WHERE create_date >= %s
            AND body ILIKE %s
            ORDER BY create_date DESC
            LIMIT 10
        """
        self.env.cr.execute(query, (three_months_ago, f"%{content}%"))
        mobile_numbers = self.env.cr.fetchall()
        mobile_numbers_list = [num[0] for num in mobile_numbers]
        body_list = [num[1] for num in mobile_numbers]

        if not mobile_numbers:
            return []

        channels = self.env['discuss.channel'].search_read(
            domain=[('whatsapp_number', 'in', mobile_numbers_list)],
            fields=['name'],
            limit=10
        )
        
        return channels
