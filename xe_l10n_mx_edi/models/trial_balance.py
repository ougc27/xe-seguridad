from odoo import models, fields
from odoo.exceptions import UserError

from collections import defaultdict
from lxml import etree


class TrialBalanceCustomHandler(models.AbstractModel):
    _inherit = 'account.trial.balance.report.handler'

    def _l10n_mx_get_sat_values(self, options):

        report = self.env['account.report'].browse(options['report_id'])
        sat_options = self._l10n_mx_get_sat_options(options)
        report_lines = report._get_lines(sat_options)

        account_lines = []
        parents = defaultdict(lambda: defaultdict(int))
        for line in [line for line in report_lines if line.get('level') == 4]:
            dummy, res_id = report._get_model_info_from_id(line['id'])
            account = self.env['account.account'].browse(res_id)
            is_credit_account = any([account.account_type.startswith(acc_type) for acc_type in ['liability', 'equity', 'income']])
            balance_sign = -1 if is_credit_account else 1
            cols = line.get('columns', [])
            # Initial Debit - Initial Credit = Initial Balance
            initial = balance_sign * (cols[0].get('no_format', 0.0) - cols[1].get('no_format', 0.0))
            # Debit and Credit of the selected period
            debit = cols[2].get('no_format', 0.0)
            credit = cols[3].get('no_format', 0.0)
            # End Debit - End Credit = End Balance
            end = balance_sign * (cols[4].get('no_format', 0.0) - cols[5].get('no_format', 0.0))
            for pid in (line['name'].split('.')[0], line['name'].rsplit('.', 1)[0]):
                parents[pid]['initial'] += initial
                parents[pid]['debit'] += debit
                parents[pid]['credit'] += credit
                parents[pid]['end'] += end
        for pid in sorted(parents.keys()):
            account_lines.append({
                'number': pid,
                'initial': '%.2f' % parents[pid]['initial'],
                'debit': '%.2f' % parents[pid]['debit'],
                'credit': '%.2f' % parents[pid]['credit'],
                'end': '%.2f' % parents[pid]['end'],
            })

        report_date = fields.Date.to_date(sat_options['date']['date_from'])

        values = {
            'vat': self.env.company.vat or '',
            'month': str(report_date.month).zfill(2),
            'year': report_date.year,
            'type': 'N',
            'accounts': account_lines,
        }
        if options.get('l10n_mx_month_13'):
            values.update({'month': '13'})

        return values
