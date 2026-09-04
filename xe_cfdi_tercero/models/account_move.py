from odoo import _, models
from odoo.exceptions import UserError

# Solo documentos de venta: el bloqueo no aplica a compras ni a asientos.
BLOCKED_MOVE_TYPES = ("out_invoice", "out_refund")


class AccountMove(models.Model):
    _inherit = "account.move"

    def _xe_is_cfdi_blocked(self):
        """Indica si este documento no debe timbrarse desde Odoo.

        Se lee la bandera con with_company() porque es un campo dependiente de
        compania y el usuario puede tener activa una compania distinta a la del
        documento.

        La condicion incluye la ausencia de UUID para que el bloqueo sea inerte
        sobre documentos ya timbrados: las facturas historicas de Mercado Libre
        conservan su CFDI y no se ven afectadas por este modulo.
        """
        self.ensure_one()
        if self.move_type not in BLOCKED_MOVE_TYPES:
            return False
        if self.l10n_mx_edi_cfdi_uuid:
            return False
        partner = self.commercial_partner_id.with_company(self.company_id)
        return bool(partner.xe_cfdi_issued_by_third_party)

    def _l10n_mx_edi_need_cfdi(self):
        """Nivel 1: el documento deja de requerir CFDI.

        Con esto la casilla de CFDI desaparece del asistente de Enviar e
        imprimir y los procesos automaticos dejan de considerar el documento.
        El envio del PDF y del correo siguen funcionando con normalidad.
        """
        self.ensure_one()
        if self._xe_is_cfdi_blocked():
            return False
        return super()._l10n_mx_edi_need_cfdi()

    def _xe_raise_if_cfdi_blocked(self):
        for move in self:
            if move._xe_is_cfdi_blocked():
                raise UserError(
                    _(
                        "No se puede timbrar %(doc)s.\n\n"
                        "El CFDI del cliente %(partner)s lo emite un tercero "
                        "(plataforma del marketplace). Timbrar desde Odoo "
                        "generaria un comprobante duplicado ante el SAT.\n\n"
                        "El XML emitido por la plataforma se relaciona con este "
                        "documento posteriormente.",
                        doc=move.display_name,
                        partner=move.commercial_partner_id.display_name,
                    )
                )

    def _l10n_mx_edi_cfdi_invoice_try_send(self):
        """Nivel 2: guard duro.

        Cubre llamadas directas por RPC, acciones de servidor, botones
        personalizados o cualquier ruta que no pase por el asistente.
        """
        self._xe_raise_if_cfdi_blocked()
        return super()._l10n_mx_edi_cfdi_invoice_try_send()

    def _l10n_mx_edi_cfdi_global_invoice_try_send(self, *args, **kwargs):
        """Guard adicional sobre la factura global (CFDI al publico en general).

        Evita que un documento bloqueado se cuele dentro de un timbrado global.
        """
        self._xe_raise_if_cfdi_blocked()
        return super()._l10n_mx_edi_cfdi_global_invoice_try_send(*args, **kwargs)
