# -*- coding: utf-8 -*-
# Copyright 2023 Morwi Encoders Consulting SA de CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, models, _

class AccountReportCustomHandler(models.AbstractModel):
	_inherit = 'account.report.custom.handler'

	def _caret_options_initializer_default(self):
		return {
			'account.account': [
				{'name': _("General Ledger"), 'action': 'caret_option_open_general_ledger'},
				{'name': _("Journal Items"), 'action': 'caret_option_open_general_ledger'},
			],

			'account.move': [
				{'name': _("View Journal Entry"), 'action': 'caret_option_open_record_form'},
				{'name': _("Journal Items"), 'action': 'caret_option_open_general_ledger'},
			],

			'account.move.line': [
				{'name': _("View Journal Entry"), 'action': 'caret_option_open_record_form', 'action_param': 'move_id'},
				{'name': _("Journal Items"), 'action': 'caret_option_open_general_ledger'},
			],

			'account.payment': [
				{'name': _("View Payment"), 'action': 'caret_option_open_record_form', 'action_param': 'payment_id'},
				{'name': _("Journal Items"), 'action': 'caret_option_open_general_ledger'},
			],

			'account.bank.statement': [
				{'name': _("View Bank Statement"), 'action': 'caret_option_open_statement_line_reco_widget'},
				{'name': _("Journal Items"), 'action': 'caret_option_open_general_ledger'},
			],

			'res.partner': [
				{'name': _("View Partner"), 'action': 'caret_option_open_record_form'},
				{'name': _("Journal Items"), 'action': 'caret_option_open_general_ledger'},
			],
		}

	def caret_option_open_general_ledger(self, options, params):
		record_id = None
		for dummy, model, model_id in reversed(self._parse_line_id(params['line_id'])):
			if model == 'account.account':
				record_id = model_id
				break

		if record_id is None:
			raise UserError(_("'Open General Ledger' caret option is only available form report lines targetting accounts."))

		account_line_id = self._get_generic_line_id('account.account', record_id)
		gl_options = self.env.ref('account_reports.general_ledger_report')._get_options(options)
		gl_options['unfolded_lines'] = [account_line_id]

		action_vals = self.env['ir.actions.actions']._for_xml_id('account_reports.action_account_report_general_ledger')
		action_vals['params'] = {
			'options': gl_options,
			'ignore_session': 'read',
		}

		return action_vals