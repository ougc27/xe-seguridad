from odoo import models, api
from odoo.tools import frozendict
from datetime import datetime
from lxml import etree
from collections import defaultdict


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
        invoice_name = invoice_customer.fiscal_name or invoice_customer.name
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
                ('state', '=', 'posted'),
                ('company_id', '=', cfdi_values['company'].id)
            ])
            if invoice_origin.x_studio_almacen_id.partner_id.zip:
                zip = invoice_origin.x_studio_almacen_id.partner_id.zip
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

        root_company = cfdi_values.get('root_company')

        self.env['l10n_mx_edi.document']._add_certificate_cfdi_values(cfdi_values)
        if cfdi_values.get('errors'):
            on_failure("\n".join(cfdi_values['errors']))
            return

        # == CFDI values ==
        populate_return = on_populate(cfdi_values)
        if cfdi_values.get('receptor'):
            customer = cfdi_values['receptor']['customer']
            if customer.parent_id:
                customer = customer.parent_id
            if cfdi_values.get('document_name', None):
                invoice_origin = self.env['account.move'].search([
                    ('name', '=', cfdi_values['document_name']),
                    ('company_id', '=', cfdi_values['company'].id),
                    ('state', '=', 'posted')
                ])
                if invoice_origin.x_studio_almacen_id.partner_id:
                    customer = invoice_origin.x_studio_almacen_id.partner_id  
            tz = customer._l10n_mx_edi_get_cfdi_timezone()
            if datetime.fromisoformat(cfdi_values['fecha']).date() == datetime.now(tz).date():
                date_fmt = '%Y-%m-%dT%H:%M:%S'
                cfdi_values["fecha"] = datetime.now(tz).astimezone(tz).strftime(date_fmt)
        if cfdi_values.get('errors'):
            on_failure("\n".join(cfdi_values['errors']))
            return

        payment_reference = None
        if cfdi_values.get('conceptos_list'):
            payment_reference = cfdi_values['conceptos_list'][0]['line']['record'].move_id.payment_reference
        
            for concepto_list in cfdi_values['conceptos_list']:
                description = concepto_list['description']
                no_identificacion = concepto_list['no_identificacion']
                client_barcode = concepto_list['line']['record'].client_barcode
                if payment_reference:
                    if not 'INV/' in payment_reference:
                        concepto_list['description'] = description + ', OC: ' + payment_reference
                concepto_list['no_identificacion'] = client_barcode or no_identificacion

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

    @api.model
    def _add_base_lines_cfdi_values(self, cfdi_values, base_lines, percentage_paid=None):
        """ Add the values about the lines to 'cfdi_values'.

        :param cfdi_values:     The current CFDI values.
        :param base_lines:      A list of dictionaries representing the lines of the document.
                                (see '_convert_to_tax_base_line_dict' in account.tax).
        :param percentage_paid: The percentage of the document lines to consider (when computing the payment CFDI).
        """
        currency = cfdi_values['currency']
        tax_objected = cfdi_values['objeto_imp']
        customer = (
            cfdi_values
            .get('receptor', {})
            .get('customer')
        )        

        # Invoice lines.
        cfdi_values['conceptos_list'] = line_values_list = []
        for line in base_lines:
            product = line['product']
            quantity = line['quantity']
            uom = line['uom']
            discount = line['discount']

            if percentage_paid:
                for list_key in ('transferred_values_list', 'withholding_values_list'):
                    for tax_values in line[list_key]:
                        tax_values['base'] = currency.round(tax_values['base'] * percentage_paid)
                        tax_values['importe'] = currency.round(tax_values['importe'] * percentage_paid)

            # Post fix the base and tax amounts to be within allowed 0.01 rounding error
            total_delta_base = 0.0
            if cfdi_values['company'].tax_calculation_rounding_method == 'round_globally':
                for list_key in ('transferred_values_list', 'withholding_values_list'):
                    for tax_values in line[list_key]:
                        if tax_values['importe'] and tax_values['tasa_o_cuota']:
                            post_amounts_map = self._get_post_fix_tax_amounts_map(
                                base_amount=tax_values['base'],
                                tax_amount=tax_values['importe'],
                                tax_rate=tax_values['tasa_o_cuota'],
                                precision_digits=cfdi_values['line_base_importe_dp'],
                            )
                            tax_values['base'] = post_amounts_map['new_base_amount']
                            tax_values['importe'] = post_amounts_map['new_tax_amount']
                            total_delta_base += post_amounts_map['delta_base_amount']

            transferred_values_list = line['transferred_values_list']
            withholding_values_list = line['withholding_values_list']

            is_refund_gi = cfdi_values['receptor']['uso_cfdi'] == 'G02'
            if is_refund_gi:
                product_unspsc_code = '84111506'
                uom_unspsc_code = 'ACT'
                description = "Devoluciones, descuentos o bonificaciones"
            else:
                product_unspsc_code = customer._get_unspsc_code_for_partner(product)
                uom_unspsc_code = uom.unspsc_code_id.code
                description = line['name']

            cfdi_line_values = {
                'line': line,
                'clave_prod_serv': product_unspsc_code,
                'no_identificacion': product.default_code,
                'cantidad': quantity,
                'clave_unidad': uom_unspsc_code,
                'unidad': (uom.name or '').upper(),
                'description': description,
                'traslados_list': [],
                'retenciones_list': [],
            }

            # Discount.
            if currency.is_zero(discount):
                discount = None
            cfdi_line_values['descuento'] = discount

            # Misc.
            if transferred_values_list or withholding_values_list:
                cfdi_line_values['objeto_imp'] = tax_objected
            else:
                cfdi_line_values['objeto_imp'] = '01'
            cfdi_line_values['importe'] = line['gross_price_subtotal'] + total_delta_base
            if cfdi_line_values['objeto_imp'] == '02':
                cfdi_line_values['traslados_list'] = transferred_values_list
                cfdi_line_values['retenciones_list'] = withholding_values_list
            else:
                cfdi_line_values['importe'] += sum(x['importe'] for x in transferred_values_list)\
                                               - sum(x['importe'] for x in withholding_values_list)
            cfdi_line_values['valor_unitario'] = cfdi_line_values['importe'] / cfdi_line_values['cantidad']

            line_values_list.append(cfdi_line_values)

        # Taxes.
        withholding_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
        withholding_reduced_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
        transferred_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
        for cfdi_line_values in line_values_list:
            for tax_values in cfdi_line_values['retenciones_list']:
                key = frozendict({'impuesto': tax_values['impuesto']})
                withholding_reduced_values_map[key]['importe'] += tax_values['importe']
            for result_dict, list_key in (
                (withholding_values_map, 'retenciones_list'),
                (transferred_values_map, 'traslados_list'),
            ):
                for tax_values in cfdi_line_values[list_key]:
                    tax_key = frozendict({
                        'impuesto': tax_values['impuesto'],
                        'tipo_factor': tax_values['tipo_factor'],
                        'tasa_o_cuota': tax_values['tasa_o_cuota']
                    })
                    result_dict[tax_key]['base'] += tax_values['base']
                    result_dict[tax_key]['importe'] += tax_values['importe']

        for list_key, source_dict in (
            ('retenciones_list', withholding_values_map),
            ('retenciones_reduced_list', withholding_reduced_values_map),
            ('traslados_list', transferred_values_map),
        ):
            cfdi_values[list_key] = [
                {
                    **tax_key,
                    'base': currency.round(tax_values['base']),
                    'importe': currency.round(tax_values['importe']),
                }
                for tax_key, tax_values in source_dict.items()
            ]

        # Totals.
        transferred_tax_amounts = [x['importe'] for x in cfdi_values['traslados_list'] if x['tipo_factor'] != 'Exento']
        withholding_tax_amounts = [x['importe'] for x in cfdi_values['retenciones_list'] if x['tipo_factor'] != 'Exento']
        cfdi_values['total_impuestos_trasladados'] = sum(transferred_tax_amounts)
        cfdi_values['total_impuestos_retenidos'] = sum(withholding_tax_amounts)
        cfdi_values['subtotal'] = sum(x['importe'] for x in line_values_list)
        cfdi_values['descuento'] = sum(x['descuento'] for x in line_values_list if x['descuento'])
        cfdi_values['total'] = cfdi_values['subtotal'] \
                             - cfdi_values['descuento'] \
                             + cfdi_values['total_impuestos_trasladados'] \
                             - cfdi_values['total_impuestos_retenidos']

        if currency.is_zero(cfdi_values['descuento']):
            cfdi_values['descuento'] = None

        # Cleanup attributes for Exento taxes.
        for line in base_lines:
            for key in ('transferred_values_list', 'withholding_values_list'):
                for tax_values in line[key]:
                    if tax_values['tipo_factor'] == 'Exento':
                        tax_values['importe'] = None
        for key in ('retenciones_list', 'traslados_list'):
            for tax_values in cfdi_values[key]:
                if tax_values['tipo_factor'] == 'Exento':
                    tax_values['importe'] = None
        if not transferred_tax_amounts:
            cfdi_values['total_impuestos_trasladados'] = None
        if not withholding_tax_amounts:
            cfdi_values['total_impuestos_retenidos'] = None
