from odoo import models, fields, api


class WhatsappReassignmentLog(models.Model):
    _name = "whatsapp.reassignment.log"
    _description = "Log of lost conversations due to automatic reassignment"
    _order = "id desc"

    lost_by_user_id = fields.Many2one(
        "res.users",
        string="Lost By User",
        required=True,
        index=True,
    )

    assigned_to_user_id = fields.Many2one(
        "res.users",
        string="Reassigned To User",
        required=True,
        index=True,
    )

    whatsapp_number = fields.Char(
        string="WhatsApp Number",
        required=True,
        index=True,
    )

    reassigned_at = fields.Datetime(
        string="Reassigned At",
        default=fields.Datetime.now,
        required=True,
    )

    lost_count = fields.Integer(
        string="Lost Count",
        help="Incremented each time this user loses a conversation due to automatic reassignment.",
    )

    wa_account_id = fields.Many2one(
        comodel_name='whatsapp.account',
        string="WhatsApp Business Account")

    reassignment_type = fields.Selection(
        selection=[
            ('auto', 'Automatic'),
            ('manual', 'Manual'),
        ],
        string="Reassignment Type",
        default='auto'
    )
