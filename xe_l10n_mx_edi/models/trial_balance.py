from odoo import models, fields
from odoo.exceptions import UserError

from collections import defaultdict
from lxml import etree


class TrialBalanceCustomHandler(models.AbstractModel):
    _inherit = 'account.trial.balance.report.handler'

    def action_l10n_mx_generate_sat_xml(self, options):
        if self.env.company.account_fiscal_country_id.code != 'MX':
            raise UserError(_("Only Mexican company can generate SAT report."))

        sat_values = self._l10n_mx_get_sat_values(options)
        file_name = f"{sat_values['vat']}{sat_values['year']}{sat_values['month']}BN"
        sat_report = etree.fromstring(self.env['ir.qweb']._render('l10n_mx_reports.cfdibalance', sat_values))

        self.env['ir.attachment'].l10n_mx_reports_validate_xml_from_attachment(sat_report, 'xsd_mx_cfdibalance_1_3.xsd')

        return {
            'file_name': f"{file_name}.xml",
            'file_content': etree.tostring(sat_report, pretty_print=True, xml_declaration=True, encoding='utf-8'),
            'file_type': 'xml',
        }