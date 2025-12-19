import json
import logging
from markupsafe import Markup
from werkzeug.exceptions import Forbidden

from odoo.addons.whatsapp.controller.main import Webhook
from odoo import http, _
from odoo.http import request

_logger = logging.getLogger(__name__)

class WebhookInherit(Webhook):

    @http.route('/whatsapp/webhook/', methods=['POST'], type="json", auth="public")
    def webhookpost(self):
        data = json.loads(request.httprequest.data)
        request.env['ir.logging'].sudo().create({
            'name': 'Whatsapp Webhook',
            'type': 'server',
            'dbname': request.env.cr.dbname,
            'level': 'INFO',
            'message': data,
            'path': 'xe_whatsapp.controllers.main.py',
            'func': 'webhookpost',
            'line': '0',
        })
        for entry in data['entry']:
            account_id = entry['id']
            account = request.env['whatsapp.account'].sudo().search(
                [('account_uid', '=', account_id)])
            if not self._check_signature(account):
                raise Forbidden()

            for changes in entry.get('changes', []):
                value = changes['value']
                phone_number_id = value.get('metadata', {}).get('phone_number_id', {})
                if not phone_number_id:
                    phone_number_id = value.get('whatsapp_business_api_data', {}).get('phone_number_id', {})
                if phone_number_id:
                    wa_account_id = request.env['whatsapp.account'].sudo().search([
                        ('phone_uid', '=', phone_number_id), ('account_uid', '=', account_id)])
                    if wa_account_id:
                        # Process Messages and Status webhooks
                        if changes['field'] == 'messages':
                            request.env['whatsapp.message']._process_statuses(value)
                            wa_account_id._process_messages(value)
                    else:
                        _logger.warning("There is no phone configured for this whatsapp webhook : %s ", data)

                # Process Template webhooks
                if value.get('message_template_id'):
                    # There is no user in webhook, so we need to SUPERUSER_ID to write on template object
                    template = request.env['whatsapp.template'].sudo().with_context(active_test=False).search([('wa_template_uid', '=', value['message_template_id'])])
                    if template:
                        if changes['field'] == 'message_template_status_update':
                            template.write({'status': value['event'].lower()})
                            if value['event'].lower() == 'rejected':
                                body = _("Your Template has been rejected.")
                                description = value.get('other_info', {}).get('description') or value.get('reason')
                                if description:
                                    body += Markup("<br/>") + _("Reason : %s", description)
                                template.message_post(body=body)
                            continue
                        if changes['field'] == 'message_template_quality_update':
                            new_quality_score = value['new_quality_score'].lower()
                            new_quality_score = {'unknown': 'none'}.get(new_quality_score, new_quality_score)
                            template.write({'quality': new_quality_score})
                            continue
                        if changes['field'] == 'template_category_update':
                            template.write({'template_type': value['new_category'].lower()})
                            continue
                        _logger.warning("Unknown Template webhook : %s ", value)
                    else:
                        _logger.warning("No Template found for this webhook : %s ", value)
