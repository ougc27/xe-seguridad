# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

WEEKDAYS = [
    ('0', 'Lunes'),
    ('1', 'Martes'),
    ('2', 'Miércoles'),
    ('3', 'Jueves'),
    ('4', 'Viernes'),
    ('5', 'Sábado'),
    ('6', 'Domingo'),
]


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    deferred_deposit = fields.Boolean(
        string="Depósito diferido",
        help="El proveedor (p. ej. Aplazo) deposita las ventas de cada corte semanal "
             "en un día fijo. Los pagos de banco (PBINMX) se fechan con ese día de depósito.",
    )
    deposit_cutoff_weekday = fields.Selection(
        WEEKDAYS,
        string="Último día del corte",
        default='1',
        help="Día en que cierra el corte semanal. Las ventas hasta este día (inclusive) "
             "entran en el siguiente depósito.",
    )
    deposit_weekday = fields.Selection(
        WEEKDAYS,
        string="Día de depósito",
        default='3',
        help="Día de la semana en que el proveedor deposita el corte cerrado.",
    )

    @api.constrains('deferred_deposit', 'type', 'journal_id')
    def _check_deferred_deposit(self):
        for method in self:
            if method.deferred_deposit and (method.type != 'bank' or not method.journal_id):
                raise ValidationError(_(
                    "El depósito diferido solo aplica a métodos de pago de banco con diario."
                ))

    def _get_deferred_deposit_date(self, sale_date):
        """Return the deposit date for a sale made on ``sale_date``: the first
        ``deposit_weekday`` strictly after the cutoff that contains the sale.
        """
        self.ensure_one()
        cutoff = int(self.deposit_cutoff_weekday)
        deposit = int(self.deposit_weekday)
        cutoff_date = sale_date + timedelta(days=(cutoff - sale_date.weekday()) % 7)
        return cutoff_date + timedelta(days=(deposit - cutoff) % 7 or 7)
