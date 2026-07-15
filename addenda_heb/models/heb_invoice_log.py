# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class HebInvoiceLog(models.Model):
    _name = "heb.invoice.log"
    _description = "HEB Submission Log"
    _order = "create_date desc"

    move_id = fields.Many2one(
        "account.move",
        string="Invoice",
        ondelete="cascade",
        index=True,
        help="Related invoice. Empty for connection tests (getMessage).",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    operation = fields.Char(string="Operation")
    accepted = fields.Boolean(string="Accepted")
    document_status = fields.Char(string="Document Status")
    error_code = fields.Char(string="Code")
    error_text = fields.Char(string="Message")
    reference = fields.Char(string="Acknowledgment Reference")
    transaction_id = fields.Char(string="Transaction ID")
    request_xml = fields.Text(string="Sent Request")
    response_xml = fields.Text(string="HEB Response")

    @api.depends("move_id", "operation", "create_date")
    def _compute_display_name(self):
        for log in self:
            if log.move_id:
                log.display_name = log.move_id.display_name
            else:
                log.display_name = log.operation or _("HEB log")
