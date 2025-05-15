from odoo import models, fields


class SurveyUserInput(models.Model):
    _inherit = 'survey.user_input'

    sale_order_id = fields.Many2one('sale.order', readonly=True)

    def get_start_url(self):
        self.ensure_one()
        aditional_param = ''
        sale_id = self.env.context.get('active_id')
        model_name = self.env.context.get('active_model')
        if model_name == 'sale.order' and sale_id:
            access_token = self.access_token + f'?sale_id={sale_id}'
            aditional_param = f'?sale_id={sale_id}'
        return '%s?answer_token=%s' % (self.survey_id.get_start_url(), access_token)

    def _mark_done(self):
        super()._mark_done()
        order_id = self.sale_order_id
        if order_id:
            order_id.write({'user_input_id': self.id})
