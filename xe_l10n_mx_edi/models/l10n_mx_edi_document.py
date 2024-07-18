from odoo import models, api

class L10nMxEdiDocument(models.Model):

    _inherit = 'l10n_mx_edi.document'

    @api.model
    def _add_customer_cfdi_values(self, cfdi_values, customer=None, usage=None, to_public=False):
        """ Add the values about the customer to 'cfdi_values'.

        :param cfdi_values:     The current CFDI values.
        :param customer:        The partner if not PUBLICO EN GENERAL.
        :param usage:           The partner's reason to ask for this CFDI.
        :param to_public:       'CFDI to public' mode.
        """
        customer = customer or self.env['res.partner']
        invoice_customer = customer if customer.type == 'invoice' else customer.commercial_partner_id
        is_foreign_customer = invoice_customer.country_id.code not in ('MX', False)
        has_missing_vat = not invoice_customer.vat
        has_missing_country = not invoice_customer.country_id
        issued_address = cfdi_values['issued_address']

        # If the CFDI is refunding a global invoice, it should be sent as a refund of a global invoice with
        # ad 'publico en general'.
        is_refund_gi = False
        if cfdi_values.get('tipo_de_comprobante') == 'E' and cfdi_values.get('tipo_relacion') in ('01', '03'):
            # Force uso_cfdi to G02 since it's a refund of a global invoice.
            origin_uuids = cfdi_values['cfdi_relationado_list']
            is_refund_gi = bool(self.search([('attachment_uuid', 'in', origin_uuids), ('state', '=', 'ginvoice_sent')], limit=1))

        customer_as_publico_en_general = (not customer and to_public) or is_refund_gi
        customer_as_xexx_xaxx = to_public or is_foreign_customer or has_missing_vat or has_missing_country

        if customer_as_publico_en_general or customer_as_xexx_xaxx:
            customer_values = {
                'to_public': True,
                'residencia_fiscal': None,
                'domicilio_fiscal_receptor': issued_address.zip,
                'regimen_fiscal_receptor': '616',
            }

            if customer_as_publico_en_general:
                customer_values.update({
                    'rfc': 'XAXX010101000',
                    'nombre': "PUBLICO EN GENERAL",
                    'uso_cfdi': 'G02' if is_refund_gi else 'S01',
                })
            else:
                customer_values.update({
                    'rfc': 'XEXX010101000' if is_foreign_customer else 'XAXX010101000',
                    'nombre': self._cfdi_sanitize_to_legal_name(invoice_customer.name),
                    'uso_cfdi': 'S01',
                })
        else:
            customer_values = {
                'to_public': False,
                'rfc': invoice_customer.vat.strip(),
                'nombre': self._cfdi_sanitize_to_legal_name(invoice_customer.name),
                'domicilio_fiscal_receptor': invoice_customer.zip,
                'regimen_fiscal_receptor': invoice_customer.l10n_mx_edi_fiscal_regime or '616',
                'uso_cfdi': usage if usage != 'P01' else 'S01',
            }
            if invoice_customer.country_id.l10n_mx_edi_code == 'MEX':
                customer_values['residencia_fiscal'] = None
            else:
                customer_values['residencia_fiscal'] = invoice_customer.country_id.l10n_mx_edi_code

        customer_values['customer'] = invoice_customer
        customer_values['issued_address'] = issued_address
        zip = issued_address.zip
        if cfdi_values.get('document_name', None):
            invoice_origin = self.env['account.move'].search([('name', '=', cfdi_values['document_name'])]).invoice_origin
            if invoice_origin:
                order_name = invoice_origin.split(", ")[0]
                partner_id = self.env['sale.order'].search([('name', '=', order_name)]).partner_id
                if partner_id.is_border_zone_iva:
                    zip = partner_id.zip

        cfdi_values.update({
            'receptor': customer_values,
            'lugar_expedicion': zip,
        })

        if customer_as_publico_en_general or customer_as_xexx_xaxx:
            cfdi_values['receptor']['domicilio_fiscal_receptor'] = zip
        if cfdi_values.get('emisor', None):
            cfdi_values['emisor']['domicilio_fiscal_receptor'] = zip


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

    def _l10n_mx_get_sat_values(self, options):

        report = self.env['account.report'].browse(options['report_id'])
        sat_options = self._l10n_mx_get_sat_options(options)
        report_lines = report._get_lines(sat_options)

        raise Exception(options)

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
        return {
            'vat': self.env.company.vat or '',
            'month': str(report_date.month).zfill(2),
            'year': report_date.year,
            'type': 'N',
            'accounts': account_lines,
        }