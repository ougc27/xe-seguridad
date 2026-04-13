import re
from datetime import datetime
from collections import defaultdict
from odoo import fields, models, api, _
from odoo.tools import frozendict
from odoo.tools.float_utils import float_round
from .l10n_mx_edi_document import USAGE_SELECTION
from odoo.addons.l10n_mx_edi.models.l10n_mx_edi_document import (
    CFDI_DATE_FORMAT
)

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

    skip_sat_update = fields.Boolean(
        string='Skip SAT Status Update',
        help='Prevents this invoice from being synchronized with the SAT status.'
    )

    def l10n_mx_edi_cfdi_invoice_try_update_payments(self):
        """ Try to update the state of payments for the current invoices. """
        warehouse_id = self.x_studio_almacen_id
        payments_diff = self._l10n_mx_edi_cfdi_invoice_get_payments_diff()

        for invoice, commands in payments_diff['to_remove'].items():
            invoice.l10n_mx_edi_invoice_document_ids = commands

        for payment, pay_results in payments_diff['to_process']:
            if not payment.x_studio_almacen_id:
                payment.write({
                    'warehouse_id': warehouse_id
                })
            payment.l10n_mx_edi_cfdi_invoice_try_update_payment(pay_results)

    @api.depends('l10n_mx_edi_cfdi_attachment_id')
    def _compute_l10n_mx_edi_cfdi_uuid(self):
        '''Fill the invoice fields from the cfdi values.
        '''
        for move in self:
            if move.l10n_mx_edi_cfdi_attachment_id:
                if not move.l10n_mx_edi_cfdi_attachment_id.datas:
                    move.l10n_mx_edi_cfdi_attachment_id.download_file_from_gcs()
                cfdi_infos = self.env['l10n_mx_edi.document']._decode_cfdi_attachment(move.l10n_mx_edi_cfdi_attachment_id.raw)
                move.l10n_mx_edi_cfdi_uuid = cfdi_infos.get('uuid')
            else:
                move.l10n_mx_edi_cfdi_uuid = None

    def l10n_mx_edi_cfdi_invoice_try_update_payment(self, pay_results, force_cfdi=False):
        """ Update the CFDI state of the current payment.

        :param pay_results: The amounts to consider for each invoice.
                            See '_l10n_mx_edi_cfdi_payment_get_reconciled_invoice_values'.
        :param force_cfdi:  Force the sending of the CFDI if the payment is PUE.
        """
        self.ensure_one()

        last_document = self.l10n_mx_edi_payment_document_ids.sorted()[:1]
        invoices = pay_results['invoices']

        # == Check PUE/PPD ==
        if (
            'PPD' not in set(invoices.mapped('l10n_mx_edi_payment_policy'))
        ):
            self._l10n_mx_edi_cfdi_payment_document_sent_pue(invoices)
            return
        # == Retry a cancellation flow ==
        if last_document.state == 'payment_cancel_failed':
            last_document._action_retry_payment_try_cancel()
            return

        qweb_template = self.env['l10n_mx_edi.document']._get_payment_cfdi_template()

        # == Lock ==
        self.env['res.company']._with_locked_records(self + invoices)

        # == Send ==
        def on_populate(cfdi_values):
            self._l10n_mx_edi_add_payment_cfdi_values(cfdi_values, pay_results)

        def on_failure(error, cfdi_filename=None, cfdi_str=None):
            self._l10n_mx_edi_cfdi_payment_document_sent_failed(error, invoices, cfdi_filename=cfdi_filename, cfdi_str=cfdi_str)

        def on_success(_cfdi_values, cfdi_filename, cfdi_str, populate_return=None):
            self._l10n_mx_edi_cfdi_payment_document_sent(invoices, cfdi_filename, cfdi_str)

        cfdi_filename = f'{self.journal_id.code}-{self.name}-MX-Payment-20.xml'.replace('/', '')
        self.env['l10n_mx_edi.document']._send_api(
            self.company_id,
            qweb_template,
            cfdi_filename,
            on_populate,
            on_failure,
            on_success,
        )

    def _l10n_mx_edi_add_payment_cfdi_values(self, cfdi_values, pay_results):
            """ Prepare the values to render the payment cfdi.

            :param cfdi_values: Prepared cfdi_values.
            :param pay_results: The amounts to consider for each invoice.
                                See '_l10n_mx_edi_cfdi_payment_get_reconciled_invoice_values'.
            :return: The dictionary to render the xml.
            """
            self.ensure_one()

            self._l10n_mx_edi_add_common_cfdi_values(cfdi_values)
            company = cfdi_values['company']
            company_curr = company.currency_id

            # Misc.
            cfdi_values['exportacion'] = '01'
            cfdi_values['forma_de_pago'] = (self.l10n_mx_edi_payment_method_id.code or '').replace('NA', '99')
            cfdi_values['moneda'] = self.currency_id.name
            cfdi_values['num_operacion'] = self.ref

            # Amounts.
            total_in_payment_curr = sum(x['payment_amount_currency'] for x in pay_results['invoice_results'])
            total_in_company_curr = sum(x['balance'] + x['payment_exchange_balance'] for x in pay_results['invoice_results'])
            if self.currency_id == company_curr:
                cfdi_values['monto'] = total_in_company_curr
            else:
                cfdi_values['monto'] = total_in_payment_curr

            # Exchange rate.
            # 'tipo_cambio' is a conditional attribute used to express the exchange rate of the currency on the date the
            # payment was made.
            # The value must reflect the number of Mexican pesos that are equivalent to a unit of the currency indicated
            # in the 'moneda' attribute.
            # It is required when the MonedaP attribute is different from MXN.
            cfdi_values['tipo_cambio_dp'] = 6
            if self.currency_id == company_curr:
                payment_rate = None
            else:
                raw_payment_rate = abs(total_in_company_curr / total_in_payment_curr) if total_in_payment_curr else 0.0
                payment_rate = float_round(raw_payment_rate, precision_digits=cfdi_values['tipo_cambio_dp'])
            cfdi_values['tipo_cambio'] = payment_rate

            # === Create the list of invoice data ===
            invoice_values_list = []
            for invoice_values in pay_results['invoice_results']:
                invoice = invoice_values['invoice']

                inv_cfdi_values = self.env['l10n_mx_edi.document']._get_company_cfdi_values(invoice.company_id)
                self.env['l10n_mx_edi.document']._add_certificate_cfdi_values(inv_cfdi_values)
                invoice._l10n_mx_edi_add_invoice_cfdi_values(inv_cfdi_values)

                # Apply the percentage paid to the tax amounts.
                if invoice.amount_total:
                    percentage_paid = abs(invoice_values['reconciled_amount'] / invoice.amount_total)
                else:
                    percentage_paid = 0.0
                for key in ('retenciones_list', 'traslados_list'):
                    for tax_values in inv_cfdi_values[key]:
                        for tax_key in ('base', 'importe'):
                            if tax_values[tax_key] is not None:
                                tax_values[tax_key] = invoice.currency_id.round(tax_values[tax_key] * percentage_paid)

                        # CRP20261:
                        # - 'base' * 'tasa_o_cuota' must give 'importe' with 0.01 rounding error allowed.
                        # Suppose an invoice of 5 * 0.47 with 16% tax. Each line gives a tax amount of 0.08 so 0.40 for the whole invoice.
                        # However, 5 * 0.47 = 2.35 and 2.35 * 0.16 = 0.38 so the constraint is failing.
                        # - 'base' + 'importe' must be exactly equal to the part that is actually paid.
                        # Using the same example, we need to report 2.35 + 0.40 = 2.75
                        # => To solve that, let's proceed backward. 2.75 * 0.16 / 1.16 = 0.38 (importe) and 2.75 - 0.38 = 2.27 (base).
                        if (
                            company.tax_calculation_rounding_method == 'round_per_line'
                            and all(tax_values[key] is not None for key in ('base', 'importe', 'tasa_o_cuota'))
                        ):
                            post_amounts_map = self.env['l10n_mx_edi.document']._get_post_fix_tax_amounts_map(
                                base_amount=tax_values['base'],
                                tax_amount=tax_values['importe'],
                                tax_rate=tax_values['tasa_o_cuota'],
                                precision_digits=invoice.currency_id.decimal_places,
                            )
                            tax_values['importe'] = post_amounts_map['new_tax_amount']
                            tax_values['base'] = post_amounts_map['new_base_amount']

                # 'equivalencia' (rate) is a conditional attribute used to express the exchange rate according to the currency
                # registered in the document related. It is required when the currency of the related document is different
                # from the payment currency.
                # The number of units of the currency must be recorded indicated in the related document that are
                # equivalent to a unit of the currency of the payment.
                def calculate_rate(invoice_amount, payment_amount):
                    if not payment_amount:
                        return 0.0
                    return abs(invoice_amount / payment_amount)

                if invoice.currency_id == self.currency_id:
                    # Same currency.
                    computed_rate = None
                elif invoice.currency_id == company_curr != self.currency_id:
                    # Adapt the payment rate to find the reconciled amount of the invoice but expressed in payment currency.
                    balance = invoice_values['balance'] + invoice_values['invoice_exchange_balance']
                    computed_rate = calculate_rate(balance, invoice_values['payment_amount_currency'])
                elif self.currency_id == company_curr != invoice.currency_id:
                    # Adapt the invoice rate to find the reconciled amount of the payment but expressed in invoice currency.
                    balance = invoice_values['balance'] + invoice_values['payment_exchange_balance']
                    computed_rate = calculate_rate(invoice_values['invoice_amount_currency'], balance)
                else:
                    # Both are expressed in different currencies.
                    computed_rate = calculate_rate(invoice_values['invoice_amount_currency'], invoice_values['payment_amount_currency'])

                invoice_values_list.append({
                    **inv_cfdi_values,
                    'id_documento': invoice.l10n_mx_edi_cfdi_uuid,
                    'equivalencia': computed_rate,
                    'inv_rate': computed_rate,
                    'num_parcialidad': invoice_values['number_of_payments'],
                    'imp_pagado': invoice_values['reconciled_amount'],
                    'imp_saldo_ant': invoice_values['amount_residual_before'],
                    'imp_saldo_insoluto': invoice_values['amount_residual_after'],
                })
            cfdi_values['docto_relationado_list'] = invoice_values_list

            # Customer.
            rfcs = set(x['receptor']['rfc'] for x in invoice_values_list)
            if len(rfcs) > 1:
                cfdi_values['errors'] = [_("You can't register a payment for invoices having different RFCs.")]
                return

            customer_values = invoice_values_list[0]['receptor']
            customer = customer_values['customer']
            cfdi_values['receptor'] = customer_values

            zip = cfdi_values['issued_address'].zip
            if self.x_studio_almacen_id.partner_id.zip:
                zip = self.x_studio_almacen_id.partner_id.zip or self.warehouse_id.zip
            cfdi_values['lugar_expedicion'] = zip

            # Date.
            cfdi_date = datetime.combine(fields.Datetime.from_string(self.date), datetime.strptime('12:00:00', '%H:%M:%S').time())
            cfdi_values['fecha'] = self._l10n_mx_edi_get_datetime_now_with_mx_timezone(cfdi_values).strftime(CFDI_DATE_FORMAT)
            cfdi_values['fecha_pago'] = cfdi_date.strftime(CFDI_DATE_FORMAT)

            # Bank information.
            payment_method_code = self.l10n_mx_edi_payment_method_id.code
            is_payment_code_emitter_ok = payment_method_code in ('02', '03', '04', '05', '06', '28', '29', '99')
            is_payment_code_receiver_ok = payment_method_code in ('02', '03', '04', '05', '28', '29', '99')
            is_payment_code_bank_ok = payment_method_code in ('02', '03', '04', '28', '29', '99')

            bank_account = customer.bank_ids.filtered(lambda x: x.company_id.id in (False, company.id))[:1]

            partner_bank = bank_account.bank_id
            if partner_bank.country and partner_bank.country.code != 'MX':
                partner_bank_vat = 'XEXX010101000'
            else:  # if no partner_bank (e.g. cash payment), partner_bank_vat is not set.
                partner_bank_vat = partner_bank.l10n_mx_edi_vat

            payment_account_ord = re.sub(r'\s+', '', bank_account.acc_number or '') or None
            payment_account_receiver = re.sub(r'\s+', '', self.journal_id.bank_account_id.acc_number or '') or None

            cfdi_values.update({
                'rfc_emisor_cta_ord': is_payment_code_emitter_ok and partner_bank_vat,
                'nom_banco_ord_ext': is_payment_code_bank_ok and partner_bank.name,
                'cta_ordenante': is_payment_code_emitter_ok and payment_account_ord,
                'rfc_emisor_cta_ben': is_payment_code_receiver_ok and self.journal_id.bank_account_id.bank_id.l10n_mx_edi_vat,
                'cta_beneficiario': is_payment_code_receiver_ok and payment_account_receiver,
            })

            # Taxes.
            cfdi_values.update({
                'monto_total_pagos': total_in_company_curr,
                'mxn_digits': company_curr.decimal_places,
            })

            def update_tax_amount(key, amount):
                if key not in cfdi_values:
                    cfdi_values[key] = 0.0
                cfdi_values[key] += amount

            def check_transferred_tax_values(tax_values, tag, tax_class, amount):
                return (
                    tax_values['impuesto'] == tag
                    and tax_values['tipo_factor'] == tax_class
                    and company_curr.compare_amounts(tax_values['tasa_o_cuota'] or 0.0, amount) == 0
                )

            withholding_values_map = defaultdict(lambda: {'importe': 0.0})
            transferred_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
            pay_rate = cfdi_values['tipo_cambio'] or 1.0
            for cfdi_inv_values in invoice_values_list:
                inv_rate = cfdi_inv_values.pop('inv_rate', False) or 1.0
                to_mxn_rate = pay_rate / inv_rate
                for tax_values in cfdi_inv_values['retenciones_list']:
                    key = frozendict({'impuesto': tax_values['impuesto']})
                    withholding_values_map[key]['importe'] += tax_values['importe'] / inv_rate

                    tax_amount_mxn = tax_values['importe'] * to_mxn_rate
                    if tax_values['impuesto'] == '001':
                        update_tax_amount('total_retenciones_isr', tax_amount_mxn)
                    elif tax_values['impuesto'] == '002':
                        update_tax_amount('total_retenciones_iva', tax_amount_mxn)
                    elif tax_values['impuesto'] == '003':
                        update_tax_amount('total_retenciones_ieps', tax_amount_mxn)

                for tax_values in cfdi_inv_values['traslados_list']:
                    key = frozendict({
                        'impuesto': tax_values['impuesto'],
                        'tipo_factor': tax_values['tipo_factor'],
                        'tasa_o_cuota': tax_values['tasa_o_cuota']
                    })
                    tax_amount = tax_values['importe'] or 0.0
                    transferred_values_map[key]['base'] += tax_values['base'] / inv_rate
                    transferred_values_map[key]['importe'] += tax_amount / inv_rate

                    base_amount_mxn = tax_values['base'] * to_mxn_rate
                    tax_amount_mxn = tax_amount * to_mxn_rate
                    if check_transferred_tax_values(tax_values, '002', 'Tasa', 0.0):
                        update_tax_amount('total_traslados_base_iva0', base_amount_mxn)
                        update_tax_amount('total_traslados_impuesto_iva0', tax_amount_mxn)
                    elif check_transferred_tax_values(tax_values, '002', 'Exento', 0.0):
                        update_tax_amount('total_traslados_base_iva_exento', base_amount_mxn)
                    elif check_transferred_tax_values(tax_values, '002', 'Tasa', 0.08):
                        update_tax_amount('total_traslados_base_iva8', base_amount_mxn)
                        update_tax_amount('total_traslados_impuesto_iva8', tax_amount_mxn)
                    elif check_transferred_tax_values(tax_values, '002', 'Tasa', 0.16):
                        update_tax_amount('total_traslados_base_iva16', base_amount_mxn)
                        update_tax_amount('total_traslados_impuesto_iva16', tax_amount_mxn)

            # Rounding global tax amounts.
            for dictionary in (withholding_values_map, transferred_values_map):
                for values in dictionary.values():
                    if 'base' in values:
                        values['base'] = self.currency_id.round(values['base'])
                    values['importe'] = self.currency_id.round(values['importe'])

            for key in (
                'total_traslados_base_iva0',
                'total_traslados_impuesto_iva0',
                'total_traslados_base_iva_exento',
                'total_traslados_base_iva8',
                'total_traslados_impuesto_iva8',
                'total_traslados_base_iva16',
                'total_traslados_impuesto_iva16',
                'total_retenciones_isr',
                'total_retenciones_iva',
                'total_retenciones_ieps',
            ):
                if key in cfdi_values:
                    cfdi_values[key] = company_curr.round(cfdi_values[key])
                else:
                    cfdi_values[key] = None

            cfdi_values['retenciones_list'] = [
                {**k, **v}
                for k, v in withholding_values_map.items()
            ]
            cfdi_values['traslados_list'] = [
                {**k, **v}
                for k, v in transferred_values_map.items()
            ]

            # Cleanup attributes for Exento taxes.
            for tax_values in cfdi_values['traslados_list']:
                if tax_values['tipo_factor'] == 'Exento':
                    tax_values['importe'] = None
