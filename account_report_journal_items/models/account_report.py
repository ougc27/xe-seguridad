# -*- coding: utf-8 -*-
# Copyright 2023 Morwi Encoders Consulting SA de CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, models, _

class AccountReport(models.Model):
	_inherit = 'account.report'

	def _get_caret_options(self):
		if self.custom_handler_model_id:
			return self.env[self.custom_handler_model_name]._caret_options_initializer()
		return self._caret_options_initializer_default()

	def _caret_options_initializer_default(self):
		vals = super(AccountReport, self)._caret_options_initializer_default()
		vals['account.account'].append({
			'name': _("Journal Items"), 'action': 'caret_option_open_journal_items'}
		)
		return vals

	def caret_option_open_journal_items(self, options, params):
		record_id = None
		for dummy, model, model_id in reversed(self._parse_line_id(params['line_id'])):
			if model == 'account.account':
				record_id = model_id
				break

		if record_id is None:
			raise UserError(_("'Open Journal Items' caret option is only available form report lines targetting accounts."))


		action_vals = self.env['ir.actions.actions']._for_xml_id('account.action_account_moves_all')
		action_vals['domain'] = """[
			('display_type', 'not in', ('line_section', 'line_note')),
			('parent_state', '!=', 'cancel'),
			('account_id', '=', {}),
			('date', '>=', '{}'),
			('date', '<=', '{}'),
		]""".format(model_id, options['date']['date_from'], options['date']['date_to'])

		return action_vals