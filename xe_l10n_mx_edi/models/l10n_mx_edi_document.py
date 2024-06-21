from odoo import models, api


class L10nMxEdiDocument(models.Model):

    _inherit = 'l10n_mx_edi.document'

    @api.model
    def _add_certificate_cfdi_values(self, cfdi_values):
        """ Replace the company zip value to the partner
        zip value if partner has is_border_zone_iva field true'.
        :param cfdi_values: The current CFDI values.
        """
        root_company = cfdi_values['root_company']
        certificate = root_company.l10n_mx_edi_certificate_ids._get_valid_certificate()
        if not certificate:
            cfdi_values['errors'] = [_("No valid certificate found")]
            return

        supplier = root_company.partner_id.commercial_partner_id.with_user(self.env.user)
        zip = supplier.zip
        invoice_origin = self.move_id.invoice_origin
        if invoice_origin:
            order_name = invoice_origin.split(", ")[0]
            print("Order name .......")
            print(order_name)
            partner_id = self.env['sale.order'].search([('name', '=', order_name)]).partner_id
            print("partner id ......")
            print(partner_id)
            if partner_id.is_border_zone_iva:
                zip = partner_id.zip
        print("zip......")
        print(zip)

        cfdi_values.update({
            'certificate': certificate,
            'no_certificado': certificate.serial_number,
            'certificado': certificate._get_data()[0].decode('utf-8'),
            'emisor': {
                'supplier': supplier,
                'rfc': supplier.vat,
                'nombre': self._cfdi_sanitize_to_legal_name(root_company.name),
                'regimen_fiscal': root_company.l10n_mx_edi_fiscal_regime,
                'domicilio_fiscal_receptor': zip,
            },
        })
