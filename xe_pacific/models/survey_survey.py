from odoo import models, _


class Survey(models.Model):
    _inherit = 'survey.survey'

    def action_send_survey(self):
        """ Open a window to compose an email, pre-filled with the survey message """
        self.check_validity()

        template = self.env.ref('survey.mail_template_user_input_invite', raise_if_not_found=False)

        partner_id = self.env.context.get('partner_id')
        partner_invoice_id = self.env.context.get('partner_invoice_id')
        partner_shipping_id = self.env.context.get('partner_shipping_id')

        partner_ids = {
            partner_id,
            partner_invoice_id,
            partner_shipping_id
        }

        partner_ids = [pid for pid in partner_ids if pid]

        local_context = dict(
            self.env.context,
            default_survey_id=self.id,
            default_template_id=template and template.id or False,
            default_email_layout_xmlid='mail.mail_notification_light',
            default_send_email=(self.access_mode != 'public'),
            default_partner_ids=partner_ids,
            default_existing_mode=self.env.context.get('existing_mode')
        )

        return {
            'type': 'ir.actions.act_window',
            'name': _("Share a Survey"),
            'view_mode': 'form',
            'res_model': 'survey.invite',
            'target': 'new',
            'context': local_context,
        }
