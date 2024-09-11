import logging
from odoo import fields, models, api

_logger = logging.getLogger(__name__)

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
                rec['name'] = rec.whatsapp_number + ' ' + rec.whatsapp_partner_id.name
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
        return res
