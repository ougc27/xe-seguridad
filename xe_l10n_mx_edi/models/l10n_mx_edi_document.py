from odoo import models, api
from datetime import datetime
from lxml import etree

USAGE_SELECTION = [
    ('G01', 'Acquisition of merchandise'),
    ('G02', 'Returns, discounts or bonuses'),
    ('G03', 'General expenses'),
    ('I01', 'Constructions'),
    ('I02', 'Office furniture and equipment investment'),
    ('I03', 'Transportation equipment'),
    ('I04', 'Computer equipment and accessories'),
    ('I05', 'Dices, dies, molds, matrices and tooling'),
    ('I06', 'Telephone communications'),
    ('I07', 'Satellite communications'),
    ('I08', 'Other machinery and equipment'),
    ('D01', 'Medical, dental and hospital expenses.'),
    ('D02', 'Medical expenses for disability'),
    ('D03', 'Funeral expenses'),
    ('D04', 'Donations'),
    ('D05', 'Real interest effectively paid for mortgage loans (room house)'),
    ('D06', 'Voluntary contributions to SAR'),
    ('D07', 'Medical insurance premiums'),
    ('D08', 'Mandatory School Transportation Expenses'),
    ('D09', 'Deposits in savings accounts, premiums based on pension plans.'),
    ('D10', 'Payments for educational services (Colegiatura)'),
    ('S01', "Without fiscal effects"),
    ('CP01', 'Payments'),
    ('CN01', 'Payroll')
]


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
        invoice_name = invoice_customer.name
        #invoice_name = invoice_customer.fiscal_name
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
                    'nombre': self._cfdi_sanitize_to_legal_name(invoice_name),
                    'uso_cfdi': 'S01',
                })
        else:
            customer_values = {
                'to_public': False,
                'rfc': invoice_customer.vat.strip(),
                'nombre': self._cfdi_sanitize_to_legal_name(invoice_name),
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
            invoice_origin = self.env['account.move'].search([
                ('name', '=', cfdi_values['document_name']),
                ('company_id', '=', cfdi_values['company'].id)
            ]).invoice_origin

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

    @api.model
    def _send_api(self, company, qweb_template, cfdi_filename, on_populate, on_failure, on_success):
        """ Common way to send a document.

        :param company:         The company.
        :param qweb_template:   The template name to render the cfdi.
        :param cfdi_filename:   The filename of the document.
        :param on_failure:      The method to call in case of failure.
        :param on_success:      The method to call in case of success.
        """
        # == Check the config ==
        cfdi_values = self.env['l10n_mx_edi.document']._get_company_cfdi_values(company)
        if cfdi_values.get('errors'):
            on_failure("\n".join(cfdi_values['errors']))
            return

        root_company = cfdi_values['root_company']

        self.env['l10n_mx_edi.document']._add_certificate_cfdi_values(cfdi_values)
        if cfdi_values.get('errors'):
            on_failure("\n".join(cfdi_values['errors']))
            return

        # == CFDI values ==
        populate_return = on_populate(cfdi_values)
        customer = cfdi_values['receptor']['customer']
        if customer.parent_id:
            customer = customer.parent_id
        if customer.is_border_zone_iva:
            tz = customer._l10n_mx_edi_get_cfdi_timezone()
            if datetime.fromisoformat(cfdi_values['fecha']).date() == datetime.now(tz).date():
                date_fmt = '%Y-%m-%dT%H:%M:%S'
                cfdi_values["fecha"] = datetime.now(tz).astimezone(tz).strftime(date_fmt)
        if cfdi_values.get('errors'):
            on_failure("\n".join(cfdi_values['errors']))
            return

        # == Generate the CFDI ==
        certificate = cfdi_values['certificate']
        self._clean_cfdi_values(cfdi_values)
        cfdi = self.env['ir.qweb']._render(qweb_template, cfdi_values)

        if 'cartaporte_30' in qweb_template:
            # Since we are inheriting version 2.0 of the Carta Porte template,
            # we need to update both the namespace prefix and its URI to version 3.0.
            cfdi = str(cfdi) \
                .replace('cartaporte20', 'cartaporte30') \
                .replace('CartaPorte20', 'CartaPorte30')

        cfdi_infos = self.env['l10n_mx_edi.document']._decode_cfdi_attachment(cfdi)
        cfdi_cadena_crypted = certificate._get_encrypted_cadena(cfdi_infos['cadena'])
        cfdi_infos['cfdi_node'].attrib['Sello'] = cfdi_cadena_crypted
        cfdi_str = etree.tostring(cfdi_infos['cfdi_node'], pretty_print=True, xml_declaration=True, encoding='UTF-8')

        # == Check credentials ==
        pac_name = root_company.l10n_mx_edi_pac
        credentials = getattr(self.env['l10n_mx_edi.document'], f'_get_{pac_name}_credentials')(root_company)
        if credentials.get('errors'):
            on_failure(
                "\n".join(credentials['errors']),
                cfdi_filename=cfdi_filename,
                cfdi_str=cfdi_str,
            )
            return

        # == Check PAC ==
        sign_results = getattr(self.env['l10n_mx_edi.document'], f'_{pac_name}_sign')(credentials, cfdi_str)
        if sign_results.get('errors'):
            on_failure(
                "\n".join(sign_results['errors']),
                cfdi_filename=cfdi_filename,
                cfdi_str=cfdi_str,
            )
            return

        # == Success ==
        on_success(cfdi_values, cfdi_filename, sign_results['cfdi_str'], populate_return=populate_return)