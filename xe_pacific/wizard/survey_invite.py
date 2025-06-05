import werkzeug
from odoo import fields, models, api


class SurveyInvite(models.TransientModel):
    _inherit = 'survey.invite'

    @api.depends('survey_id.access_token')
    def _compute_survey_start_url(self):
        for invite in self:
            sale_id = self.env.context.get('active_id')
            model_name = self.env.context.get('active_model')
            if invite.survey_id:
                url = werkzeug.urls.url_join(invite.survey_id.get_base_url(), invite.survey_id.get_start_url())
                if model_name == 'sale.order' and sale_id:
                    url = url + f'?sale_id={sale_id}'
                invite.survey_start_url = url
                continue
            invite.survey_start_url = False
