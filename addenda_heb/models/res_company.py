# -*- coding: utf-8 -*-
from odoo import fields, models, _


class ResCompany(models.Model):
    _inherit = "res.company"

    heb_ws_url_invoice = fields.Char(
        string="HEB - Invoice Service URL",
        default="https://recepcionfeV33.heb.com.mx:50012/MexicoDigitalInvoiceService",
        help="Endpoint for CFDI 4.0 invoice submission. In the test environment "
            "it uses port 50012 (setDigitalInvoice).",
    )

    heb_ws_username = fields.Char(
        string="HEB - Username"
    )

    heb_ws_password = fields.Char(
        string="HEB - Password"
    )

    heb_ws_verify_ssl = fields.Boolean(
        string="HEB - Verify SSL",
        default=False,
        help="Disable in the test environment if the server certificate cannot "
            "be validated.",
    )

    def action_heb_test_connection(self):
        """getMessage: check connectivity/security. Shows a notification and
        keeps a record in the HEB log. Nothing is posted to any chatter."""
        self.ensure_one()
        result = self.env["heb.invoice.ws"].test_connection(self)
        reachable = result.get("reachable")

        self.env["heb.invoice.log"].create({
            "company_id": self.id,
            "operation": result.get("operation") or "getMessage",
            "accepted": bool(reachable),
            "document_status": "OK" if reachable else "NO_RESPONSE",
            "request_xml": result.get("request"),
            "response_xml": result.get("raw"),
        })

        if reachable:
            message = _("Connection OK (HTTP %s). Service available.") \
                % result.get("status")
        else:
            message = _("No expected response (HTTP %s). Check the log.") \
                % result.get("status")

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success" if reachable else "warning",
                "title": _("HEB"),
                "message": message,
                "sticky": False,
            },
        }
