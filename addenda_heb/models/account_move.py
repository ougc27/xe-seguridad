# -*- coding: utf-8 -*-
from lxml import etree

from odoo import fields, models, _
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    heb_branch_id = fields.Many2one(
        "heb.branch",
        string="HEB Branch",
        tracking=True,
        help="Branch for the HEB addenda.")

    heb_send_state = fields.Selection(
        selection=[
            ("to_send", "To Send"),
            ("sent", "Accepted"),
            ("error", "Rejected / Error"),
        ],
        string="HEB - Status",
        copy=False,
        tracking=True,
    )

    heb_reference = fields.Char(
        string="HEB - Acknowledgment Reference",
        copy=False,
        help="Acknowledgment number (referenceIdentification) returned by HEB when "
            "the document is accepted.",
    )

    heb_transaction_id = fields.Char(
        string="HEB - Transaction ID",
        copy=False,
        help="Internal transaction identifier on the HEB side "
            "(uniqueCreatorIdentification). Used to track the submission with "
            "HEB support.",
    )

    def _heb_get_signed_cfdi(self):
        """Signed CFDI bytes (with the addenda already embedded by l10n_mx_edi)."""
        self.ensure_one()
        attachment = self.l10n_mx_edi_cfdi_attachment_id
        if not attachment:
            raise UserError(_(
                "Invoice %s has no signed CFDI. Stamp the invoice before "
                "sending it to HEB.") % self.display_name)
        if not attachment.datas and hasattr(attachment, "download_file_from_gcs"):
            attachment.download_file_from_gcs()
        xml_bytes = attachment.raw
        if not xml_bytes:
            raise UserError(_(
                "The signed CFDI content of invoice %s could not be retrieved."
            ) % self.display_name)
        return xml_bytes

    def _heb_check_sendable(self):
        self.ensure_one()
        if self.move_type not in ("out_invoice", "out_refund"):
            raise UserError(_("Only customer invoices can be sent to HEB."))
        if self.state != "posted":
            raise UserError(_("The invoice must be posted."))
        if self.l10n_mx_edi_cfdi_state != "sent":
            raise UserError(_(
                "The invoice must be stamped (CFDI sent) before sending it "
                "to HEB."))

    def _heb_verify_addenda_present(self, xml_bytes):
        """Confirm that the CFDI contains the HEB addenda (requestForPayment)."""
        try:
            root = etree.fromstring(xml_bytes)
        except Exception:
            return
        for el in root.iter():
            if el.tag is etree.Comment:
                continue
            if etree.QName(el).localname == "requestForPayment":
                return
        raise UserError(_(
            "The signed CFDI of invoice %s does not contain the HEB addenda "
            "(requestForPayment node).\nMake sure the customer has the HEB "
            "addenda assigned and stamp the invoice again.") % self.display_name)

    def action_send_addenda_heb(self):
        """Send the CFDI with addenda to the HEB reception web service."""
        ws = self.env["heb.invoice.ws"]
        for move in self:
            move._heb_check_sendable()
            xml_bytes = move._heb_get_signed_cfdi()
            move._heb_verify_addenda_present(xml_bytes)

            result = ws.send_invoice(move.company_id, xml_bytes)
            move._heb_process_result(result)
        return True

    def _heb_process_result(self, result):
        """Update the status, keep a submission log and post a short note."""
        self.ensure_one()
        accepted = bool(result.get("ok"))
        if accepted:
            self.write({
                "heb_send_state": "sent",
                "heb_reference": result.get("reference") or self.heb_reference,
                "heb_transaction_id": result.get("transaction_id")
                or self.heb_transaction_id,
            })
        else:
            self.write({"heb_send_state": "error"})

        # Full technical detail (request + response) goes to the log model,
        # not to the chatter. The chatter only keeps a short status note.
        self._heb_create_log(result)

        if accepted:
            self.message_post(body=_(
                "\u2705 <b>Invoice accepted by HEB.</b> "
                "Acknowledgment reference: <b>%s</b>"
            ) % (result.get("reference") or _("(not returned)")))
        else:
            detail = (result.get("error_text")
                      or result.get("error_code")
                      or _("Unrecognized response."))
            self.message_post(body=_(
                "\u274c <b>HEB rejected or did not accept the document.</b> %s"
            ) % detail)

    def _heb_create_log(self, result):
        """Store the full submission detail in the heb.invoice.log model."""
        self.ensure_one()
        return self.env["heb.invoice.log"].create({
            "move_id": self.id,
            "operation": result.get("operation"),
            "accepted": bool(result.get("ok")),
            "document_status": result.get("document_status"),
            "error_code": result.get("error_code"),
            "error_text": result.get("error_text"),
            "reference": result.get("reference"),
            "transaction_id": result.get("transaction_id"),
            "request_xml": result.get("request"),
            "response_xml": result.get("raw"),
        })
