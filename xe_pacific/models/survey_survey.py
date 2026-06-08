import random
from odoo import models, fields, api


class Survey(models.Model):
    _inherit = 'survey.survey'

    questions_selection = fields.Selection(
        selection_add=[('random_all', 'Randomized (All questions)')],
        ondelete={'random_all': 'set default'}
    )


    def _get_next_page_or_question(self, user_input, page_or_question_id, go_back=False):
        if self.questions_selection == 'random_all':
            Question = self.env['survey.question']
            
            all_questions = self.question_ids.filtered(lambda q: not q.is_page)
            q_ids = all_questions.ids

            if not q_ids:
                return Question

            random.seed(user_input.id)
            random.shuffle(q_ids)

            if not go_back and page_or_question_id == 0:
                return Question.browse(q_ids[0])

            try:
                current_idx = q_ids.index(page_or_question_id)
            except ValueError:
                return Question.browse(q_ids[0])

            if (go_back and current_idx == 0) or (not go_back and current_idx == len(q_ids) - 1):
                return Question

            next_idx = current_idx - 1 if go_back else current_idx + 1
            return Question.browse(q_ids[next_idx])

        return super(Survey, self)._get_next_page_or_question(user_input, page_or_question_id, go_back=go_back)
