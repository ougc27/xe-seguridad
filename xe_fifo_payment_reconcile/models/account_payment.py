from odoo import fields, models

FIFO_RECONCILE_CRON_XMLID = 'xe_fifo_payment_reconcile.ir_cron_fifo_reconcile'


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    fifo_reconcile_release = fields.Boolean(
        string='Release for FIFO Reconcile', tracking=True, copy=False,
        help="Turn on to let the background process apply this payment to "
             "the partner's oldest open PUE invoices. It turns itself off "
             "once the process ends (done, done with residual or error). "
             "Turning it off while in progress pauses the process; turning "
             "it back on resumes or retries it.",
    )
    fifo_reconcile_state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('in_progress', 'In Progress'),
            ('done', 'Done'),
            ('done_residual', 'Done with Residual'),
            ('error', 'Error'),
        ],
        string='FIFO Reconcile State', readonly=True, tracking=True,
        copy=False,
    )
    # Solo para el 'invisible' del formulario: el contacto comercial del
    # pago tiene activada la conciliación FIFO.
    fifo_partner_enabled = fields.Boolean(
        string='FIFO Enabled for Partner',
        related='partner_id.commercial_partner_id.fifo_auto_reconcile',
    )
    # Reintentos consecutivos por errores de concurrencia; se reinicia al
    # terminar un lote con éxito o al reactivar el booleano.
    fifo_retry_count = fields.Integer(
        string='FIFO Concurrency Retries', copy=False, default=0,
    )

    def write(self, vals):
        released = vals.get('fifo_reconcile_release')
        if released:
            # Activar (o reactivar) el booleano equivale a reintentar o continuar
            vals = dict(vals, fifo_reconcile_state='pending', fifo_retry_count=0)
        res = super().write(vals)
        if released:
            self._fifo_reconcile_trigger_cron()
        return res

    def _fifo_reconcile_trigger_cron(self, at=None):
        # _trigger() hace el NOTIFY que despierta al proceso de crons; crear
        # ir.cron.trigger a mano no lo despierta (visto en staging).
        cron = self.env.ref(FIFO_RECONCILE_CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(at=at)
