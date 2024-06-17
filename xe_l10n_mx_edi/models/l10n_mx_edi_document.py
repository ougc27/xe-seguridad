from odoo import models, api, _


class L10nMxEdiDocument(models.Model):

    _inherit = 'l10n_mx_edi.document'

    @api.model
    def _add_certificate_cfdi_values(self, cfdi_values):
        """ Replace the company zip value to the partner
        zip value if partner has is_border_zone_iva field true'.

        :param cfdi_values: The current CFDI values.
        """
        super()._add_certificate_cfdi_values(cfdi_values)
        invoice_origin = self.move_id.invoice_origin
        order_name = invoice_origin.split(", ")[0]
        partner_id = self.env['sale.order'].search([('name', '=', order_name)]).partner_id
        if partner_id.is_border_zone_iva:
            if 'emisor' in cfdi_values:
                cfdi_values['emisor']['domicilio_fiscal_receptor'] = partner_id.zip
