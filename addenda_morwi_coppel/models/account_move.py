# -*- coding: utf-8 -*-
from odoo import models, api, fields

from lxml import etree
import logging
_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    addenda_coppel_po_date = fields.Date(string='PO Date')
    addenda_coppel_po_date_delivery = fields.Date(string='PO Date Delivery')
    addenda_coppel_transaction_type = fields.Char(string='Transaction Type')
    addenda_coppel_supplier_type = fields.Char(default='1', string='Supplier Type', readonly=True)
    addenda_coppel_store_dc_id = fields.Many2one(
        'coppel.store',
        'Final C.D. for Delivery',
    )
    addenda_coppel_store_reception_dc_id = fields.Many2one(
        'coppel.store',
        'Final C.D. for Reception',
    )
    #Remove fields
    addenda_coppel_dc = fields.Selection([
        ('30001', 'CULIACAN'),
        ('30002', 'LEON'),
        ('30003', 'HERMOSILLO'),
        ('30004', 'LAGUNA'),
        ('30006', 'MONTERREY'),
        ('30007', 'GUADALAJARA'),
        ('30008', 'AZCAPOTZALCO'),
        ('30009', 'PUEBLA'),
        ('30010', 'VILLA HERMOSA'),
        ('30011', 'IZTAPALAPA'),
        ('30012', 'IMP_MXL2'),
        ('30013', 'MEXICALI'),
        ('30014', 'IZCALLI'),
        ('30015', 'IXTAPALUCA'),
        ('30016', 'TECAMAC'),
        ('30017', 'MERIDA'),
        ('30018', 'LOS MOCHIS'),
        ('30019', 'VERACRUZ'),
        ('30020', 'GUADALAJARA II'),
        ('30021', 'IXTAPALUCA IMP'),
        ('30022', 'TOLUCA'),
        ('30023', 'GUADALUPE'),
        ('30024', 'TEXCOCO'),
        ('30025', 'TIJUANA'),
        ('30026', 'CHIHUAHUA'),
        ('30030', 'TECAMAC II'),
        ('30031', 'PUEBLA II'),
    ], string='Final C.D. for Delivery')
    addenda_coppel_reception_dc = fields.Selection([
        ('30001', 'CULIACAN'),
        ('30002', 'LEON'),
        ('30003', 'HERMOSILLO'),
        ('30004', 'LAGUNA'),
        ('30006', 'MONTERREY'),
        ('30007', 'GUADALAJARA'),
        ('30008', 'AZCAPOTZALCO'),
        ('30009', 'PUEBLA'),
        ('30010', 'VILLA HERMOSA'),
        ('30011', 'IZTAPALAPA'),
        ('30012', 'IMP_MXL2'),
        ('30013', 'MEXICALI'),
        ('30014', 'IZCALLI'),
        ('30015', 'IXTAPALUCA'),
        ('30016', 'TECAMAC'),
        ('30017', 'MERIDA'),
        ('30018', 'LOS MOCHIS'),
        ('30019', 'VERACRUZ'),
        ('30020', 'GUADALAJARA II'),
        ('30021', 'IXTAPALUCA IMP'),
        ('30022', 'TOLUCA'),
        ('30023', 'GUADALUPE'),
        ('30024', 'TEXCOCO'),
        ('30025', 'TIJUANA'),
        ('30026', 'CHIHUAHUA'),
        ('30030', 'TECAMAC II'),
        ('30031', 'PUEBLA II'),
    ], string='C.D. for Reception')
    addenda_coppel_region = fields.Selection([
        ('1', 'La Paz, Tijuana, Mexicali'),
        ('2', 'Culiacán, Hermosillo, Los Mochis'),
        ('3', 'Laguna'),
        ('4', 'Monterrey'),
        ('5', 'Guadalajara'),
        ('6', 'León, Izcalli'),
        ('7', 'Puebla'),
        ('8', 'Villahermosa, Merida'),
        ('9', 'México, Iztapalapa, Izcalli, Ixtapaluca, Tecamac'),
    ], string='Region ID')
    addenda_coppel_currency_function = fields.Selection([
        ('BILLING_CURRENCY', 'Divisa de facturación'),
        ('PRICE_CURRENCY', 'Divisa del precio'),
        ('PAYMENT_CURRENCY', 'Divisa de pago'),
    ], string='Currency Function')
    addenda_coppel_packing = fields.Selection([
        ('SELLER_PROVIDED', 'Caja Propia'),
        ('BUYER_PROVIDED', 'Caja de Coppel'),
    ], string='Packing')
    addenda_coppel_transporter = fields.Char(string='Transporter')
    addenda_coppel_discount_charge = fields.Selection([
        ('ALLOWANCE_GLOBAL', 'Descuento Global'),
        ('CHARGE_GLOBAL', 'Cargo Global'),
    ], string='Discount or Charge')
    addenda_coppel_discount_charge_imputation = fields.Selection([
        ('BILL_BACK', 'Reclamación'),
        ('OFF_INVOICE', 'Fuera de factura'),
    ], string='Discount or Charge Imputation')
    addenda_coppel_discount_type = fields.Selection([
        ('AA', 'Abono por Publicidad'),
        ('EAB', 'Descuento por pronto pago'),
        ('DXDIST', 'Descuento por distribuir la mercancia de bodega a tienda'),
        ('DXALM', 'Descuento por resguardar la mercancia'),
        ('DXECEN', 'Descuento por entregar en una sola bodega'),
        ('DXDIST', 'Descuento por distribuir la mercancía de bodega a tienda'),
        ('DXALM', 'Descuento por resguardar la mercancía'),
        ('XECEN', 'Descuento por entregar en una sola bodega'),
    ], string='Discount Type')


class AccountMoveSend(models.TransientModel):
    _inherit = 'account.move.send'

    @api.model
    def _process_send_and_print(self, moves, wizard=None, allow_fallback_pdf=False, **kwargs):
        # extends account to create the pdf attachment
        # in the matching inter-company move

        # l10n_mx_edi_cfdi_attachment_id
        for move in moves:
            ir_attachments = self.env['ir.attachment'].sudo().search([('name', '=', move.invoice_pdf_report_id.name)])
            for ir_attachment in ir_attachments:
                if ir_attachment and not ir_attachment.datas:
                    ir_attachment.sudo().download_file_from_gcs()
            l10n_mx_attachment = move.invoice_pdf_report_id
            if l10n_mx_attachment and not l10n_mx_attachment.datas:
                l10n_mx_attachment.sudo().download_file_from_gcs()
            l10n_mx_edi_cfdi_attachment_id = move.l10n_mx_edi_cfdi_attachment_id
            if l10n_mx_edi_cfdi_attachment_id and not l10n_mx_edi_cfdi_attachment_id.datas:
                l10n_mx_edi_cfdi_attachment_id.sudo().download_file_from_gcs()
            for doc in move.l10n_mx_edi_invoice_document_ids:
                attachment_id = doc.attachment_id
                if attachment_id and not attachment_id.datas:
                    attachment_id.download_file_from_gcs()
                cfdi_node = etree.fromstring(attachment_id.raw)
                for node in cfdi_node.xpath("//*[local-name()='Cadena']"):
                    if node.text == '{}':
                        node.text = move._l10n_mx_edi_get_extra_invoice_report_values()['cadena']
            
                attachment_id.raw = etree.tostring(cfdi_node, pretty_print=True, xml_declaration=True, encoding='UTF-8')

        res = super()._process_send_and_print(moves, wizard=wizard, allow_fallback_pdf=allow_fallback_pdf, **kwargs)
        return res


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    addenda_coppel_vendor_id = fields.Many2one('product.supplierinfo', compute='_compute_addenda_coppel_vendor_id', string='Vendor')

    def _compute_addenda_coppel_vendor_id(self):
        for line in self:
            if line.product_id and line.product_id.seller_ids:
                vendors = line.product_id.seller_ids.filtered(lambda x: x.partner_id.id == line.move_id.partner_id.id)
                if len(vendors) == 1:
                    line.addenda_coppel_vendor_id = vendors
                else:
                    line.addenda_coppel_vendor_id = False
            else:
                line.addenda_coppel_vendor_id = False