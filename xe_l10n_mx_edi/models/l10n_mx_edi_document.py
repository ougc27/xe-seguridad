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
