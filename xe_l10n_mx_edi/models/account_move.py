from odoo import fields, models
from odoo.tools import float_round

from .l10n_mx_edi_document import USAGE_SELECTION

import re
from collections import defaultdict


class AccountMove(models.Model):
    
    _inherit = 'account.move'

    l10n_mx_edi_usage = fields.Selection(
        selection=USAGE_SELECTION,
        string="Usage",
        readonly=False,
        store=True,
        compute='_compute_l10n_mx_edi_usage',
        default="G03",
        tracking=True,
        help="Used in CFDI to express the key to the usage that will gives the receiver to this invoice. This "
             "value is defined by the customer.\nNote: It is not cause for cancellation if the key set is not the usage "
             "that will give the receiver of the document.",
    )

    def _l10n_mx_edi_add_invoice_cfdi_values(self, cfdi_values, percentage_paid=None, global_invoice=False):
        # EXTENDS 'l10n_mx_edi'
        import pdb;
        pdb.set_trace()
        self.ensure_one()

        if self.journal_id.l10n_mx_address_issued_id:
            cfdi_values['issued_address'] = self.journal_id.l10n_mx_address_issued_id

        super()._l10n_mx_edi_add_invoice_cfdi_values(cfdi_values, percentage_paid=percentage_paid, global_invoice=global_invoice)
        if cfdi_values.get('errors'):
            return

        cfdi_values['exportacion'] = self.l10n_mx_edi_external_trade_type or '01'

        # External Trade
        ext_trade_values = cfdi_values['comercio_exterior'] = {}
        if self.l10n_mx_edi_external_trade_type == '02':

            # Customer.
            customer_values = cfdi_values['receptor']
            customer = customer_values['customer']
            if customer_values['rfc'] == 'XEXX010101000':
                cfdi_values['receptor']['num_reg_id_trib'] = customer.vat
                # A value must be registered in the ResidenciaFiscal field when information is registered in the
                # NumRegIdTrib field.
                cfdi_values['receptor']['residencia_fiscal'] = customer.country_id.l10n_mx_edi_code

            ext_trade_values['receptor'] = {
                **cfdi_values['receptor'],
                'curp': customer.l10n_mx_edi_curp,
                'calle': customer.street_name,
                'numero_exterior': customer.street_number,
                'numero_interior': customer.street_number2,
                'colonia': customer.l10n_mx_edi_colony_code,
                'localidad': customer.l10n_mx_edi_locality_id.code,
                'municipio': customer.city_id.l10n_mx_edi_code,
                'estado': customer.state_id.code,
                'pais': customer.country_id.l10n_mx_edi_code,
                'codigo_postal': customer.zip,
            }

            # Supplier.
            supplier_values = cfdi_values['emisor']
            supplier = supplier_values['supplier']
            #zip = supplier.zip
            #invoice_origin = self.invoice_origin
            #if invoice_origin:
                #order_name = invoice_origin.split(", ")[0]
                #partner_id = self.env['sale.order'].search([('name', '=', order_name)]).partner_id
                #if partner_id.is_border_zone_iva:
                    #zip = partner_id.zip

            ext_trade_values['emisor'] = {
                'curp': supplier.l10n_mx_edi_curp,
                'calle': supplier.street_name,
                'numero_exterior': supplier.street_number,
                'numero_interior': supplier.street_number2,
                'colonia': supplier.l10n_mx_edi_colony_code,
                'localidad': supplier.l10n_mx_edi_locality_id.code,
                'municipio': supplier.city_id.l10n_mx_edi_code,
                'estado': supplier.state_id.code,
                'pais': supplier.country_id.l10n_mx_edi_code,
                'codigo_postal': supplier.zip,
            }

            # Shipping.
            shipping = self.partner_shipping_id
            if shipping != customer:

                # In case of COMEX we need to fill "NumRegIdTrib" with the real tax id of the customer
                # but let the generic RFC.
                shipping_values = self.env['l10n_mx_edi.document']._get_customer_cfdi_values(
                    shipping,
                    cfdi_values['receptor']['issued_address'],
                    usage=cfdi_values['receptor']['uso_cfdi'],
                    to_public=self.l10n_mx_edi_cfdi_to_public,
                )['receptor']
                if (
                    shipping.country_id == shipping.commercial_partner_id.country_id
                    and shipping_values['rfc'] == 'XEXX010101000'
                ):
                    shipping_vat = shipping.vat.strip() if shipping.vat else None
                else:
                    shipping_vat = None

                if shipping.country_id.l10n_mx_edi_code == 'MEX':
                    colony = shipping.l10n_mx_edi_colony_code
                    locality = shipping.l10n_mx_edi_locality_id
                    city = shipping.city_id.l10n_mx_edi_code
                else:
                    colony = shipping.l10n_mx_edi_colony
                    locality = shipping.l10n_mx_edi_locality
                    city = shipping.city

                if shipping.country_id.l10n_mx_edi_code in ('MEX', 'USA', 'CAN') or shipping.state_id.code:
                    state = shipping.state_id.code
                else:
                    state = 'NA'

                ext_trade_values['destinario'] = {
                    'num_reg_id_trib': shipping_vat,
                    'nombre': shipping.name,
                    'calle': supplier.street_name,
                    'numero_exterior': supplier.street_number,
                    'numero_interior': supplier.street_number2,
                    'colonia': colony,
                    'localidad': locality,
                    'municipio': city,
                    'estado': state,
                    'pais': supplier.country_id.l10n_mx_edi_code,
                    'codigo_postal': supplier.zip,
                }

            # Certificate.
            ext_trade_values['certificado_origen'] = '1' if self.l10n_mx_edi_cer_source else '0'
            ext_trade_values['num_certificado_origen'] = self.l10n_mx_edi_cer_source

            # Rate.
            mxn = self.env["res.currency"].search([('name', '=', 'MXN')], limit=1)
            usd = self.env["res.currency"].search([('name', '=', 'USD')], limit=1)
            ext_trade_values['tipo_cambio_usd'] = usd._get_conversion_rate(usd, mxn, self.company_id, self.date)
            if ext_trade_values['tipo_cambio_usd']:
                to_usd_rate = (cfdi_values['tipo_cambio'] or 1.0) / ext_trade_values['tipo_cambio_usd']
            else:
                to_usd_rate = 0.0

            # Misc.
            if customer.country_id in self.env.ref('base.europe').country_ids:
                ext_trade_values['numero_exportador_confiable'] = self.company_id.l10n_mx_edi_num_exporter
            else:
                ext_trade_values['numero_exportador_confiable'] = None
            ext_trade_values['incoterm'] = self.invoice_incoterm_id.code
            ext_trade_values['observaciones'] = self.narration

            # Details per product.
            product_values_map = defaultdict(lambda: {
                'quantity': 0.0,
                'price_unit': 0.0,
                'total': 0.0,
            })
            for line_vals in cfdi_values['conceptos_list']:
                line = line_vals['line']['record']
                product_values_map[line.product_id]['quantity'] += line.l10n_mx_edi_qty_umt
                product_values_map[line.product_id]['price_unit'] += line.l10n_mx_edi_price_unit_umt
                product_values_map[line.product_id]['total'] += line_vals['importe']
            ext_trade_values['total_usd'] = 0.0
            ext_trade_values['mercancia_list'] = []
            for product, product_values in product_values_map.items():
                total_usd = float_round(product_values['total'] * to_usd_rate, precision_digits=4)
                ext_trade_values['mercancia_list'].append({
                    'no_identificacion': product.default_code,
                    'fraccion_arancelaria': product.l10n_mx_edi_tariff_fraction_id.code,
                    'cantidad_aduana': product_values['quantity'],
                    'unidad_aduana': product.l10n_mx_edi_umt_aduana_id.l10n_mx_edi_code_aduana,
                    'valor_unitario_udana': float_round(product_values['price_unit'] * to_usd_rate, precision_digits=6),
                    'valor_dolares': total_usd,
                })
                ext_trade_values['total_usd'] += total_usd
        else:
            # Invoice lines.
            for line_vals in cfdi_values['conceptos_list']:
                line_vals['informacion_aduanera_list'] = line_vals['line']['record']._l10n_mx_edi_get_custom_numbers()


    # -------------------------------------------------------------------------
    # CFDI Generation: Payments
    # -------------------------------------------------------------------------

    def _l10n_mx_edi_add_payment_cfdi_values(self, cfdi_values, pay_results):
        """ Prepare the values to render the payment cfdi.

        :param cfdi_values: Prepared cfdi_values.
        :param pay_results: The amounts to consider for each invoice.
                            See '_l10n_mx_edi_cfdi_payment_get_reconciled_invoice_values'.
        :return: The dictionary to render the xml.
        """
        import pdb;
        pdb.set_trace()
        super().get_delivery_date(cfdi_values, pay_results)
        zip = cfdi_values['issued_address'].zip
        invoice_origin = self.invoice_origin
        if invoice_origin:
            order_name = invoice_origin.split(", ")[0]
            partner_id = self.env['sale.order'].search([('name', '=', order_name)]).partner_id
            if partner_id.is_border_zone_iva:
                zip = partner_id.zip
        cfdi_values['lugar_expedicion'] = zip
