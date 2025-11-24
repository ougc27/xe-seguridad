from odoo import http
from odoo.http import request
from markupsafe import Markup
import base64
import requests
from datetime import datetime
from werkzeug.exceptions import NotFound
from odoo.addons.mail.controllers.thread import ThreadController
from odoo.addons.mail.models.discuss.mail_guest import add_guest_to_context
from odoo.exceptions import ValidationError, UserError


class ThreadControllerInherit(ThreadController):

    @http.route("/mail/message/post", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def mail_message_post(self, thread_model, thread_id, post_data, context=None):
        guest = request.env["mail.guest"]._get_guest_from_context()
        guest.env["ir.attachment"].browse(post_data.get("attachment_ids", []))._check_attachments_access(
            post_data.get("attachment_tokens")
        )
        for attachment_id in post_data.get('attachment_ids'):
            attachment = request.env["ir.attachment"].sudo().browse(attachment_id)
            attachment.download_file_from_gcs()
        if context:
            request.update_context(**context)
        canned_response_ids = tuple(cid for cid in post_data.pop('canned_response_ids', []) if isinstance(cid, int))
        if canned_response_ids:
            # Avoid serialization errors since last used update is not
            # essential and should not block message post.
            request.env.cr.execute("""
                UPDATE mail_shortcode SET last_used=%(last_used)s
                WHERE id IN (
                    SELECT id from mail_shortcode WHERE id IN %(ids)s
                    FOR NO KEY UPDATE SKIP LOCKED
                )
            """, {
                'last_used': datetime.now(),
                'ids': canned_response_ids,
            })
        thread = request.env[thread_model].with_context(active_test=False).search([("id", "=", thread_id)])
        thread = thread.with_context(active_test=True)
        if not thread:
            raise NotFound()
        if "body" in post_data:
            post_data["body"] = Markup(post_data["body"])  # contains HTML such as @mentions
        new_partners = []
        if "partner_emails" in post_data:
            new_partners = [record.id for record in request.env["res.partner"]._find_or_create_from_emails(
                post_data["partner_emails"], post_data.get("partner_additional_values", {})
            )]
        post_data["partner_ids"] = list(set((post_data.get("partner_ids", [])) + new_partners))
        message_data = thread.message_post(
            **{key: value for key, value in post_data.items() if key in self._get_allowed_message_post_params()}
        ).message_format()[0]
        if "temporary_id" in request.context:
            message_data["temporary_id"] = request.context["temporary_id"]
        return message_data
