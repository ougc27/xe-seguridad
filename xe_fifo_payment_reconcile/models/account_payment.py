import logging
import time
from datetime import timedelta

from markupsafe import Markup
from psycopg2 import Error as PsycopgError, errorcodes

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_amount

_logger = logging.getLogger(__name__)

FIFO_RECONCILE_CRON_XMLID = 'xe_fifo_payment_reconcile.ir_cron_fifo_reconcile'

# Tamaño de lote: número de FACTURAS por ejecución (una factura puede
# aportar varias líneas si sus términos de pago dividen el saldo).
PARAM_BATCH_SIZE = 'xe_fifo_payment_reconcile.fifo_reconcile_batch_size'
DEFAULT_BATCH_SIZE = 250

# Regla fija del proceso: solo facturas PUE. Se excluyen las facturas sin
# forma de pago y las de forma 99 ("Por definir", la que usan las PPD).
# Conciliar PPD requeriría emitir complementos de pago y queda fuera de
# alcance, así que esto NO es un filtro configurable.
EXCLUDED_PAYMENT_METHOD_CODE = '99'

# Errores transitorios de concurrencia: el lote se revierte y se reintenta
# en una ejecución posterior, sin marcar el pago como error.
CONCURRENCY_ERROR_CODES = (
    errorcodes.SERIALIZATION_FAILURE,
    errorcodes.LOCK_NOT_AVAILABLE,
    errorcodes.DEADLOCK_DETECTED,
)
MAX_CONCURRENCY_RETRIES = 5
CONCURRENCY_RETRY_DELAY = timedelta(seconds=60)

# Estados en los que el proceso termina y apaga el booleano
FINAL_STATES = ('done', 'done_residual', 'error')


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

    @api.model_create_multi
    def create(self, vals_list):
        # Un pago creado ya liberado (importación o API) también arranca en pendiente
        vals_list = [
            dict(vals, fifo_reconcile_state='pending', fifo_retry_count=0)
            if vals.get('fifo_reconcile_release') else vals
            for vals in vals_list
        ]
        payments = super().create(vals_list)
        if any(payment.fifo_reconcile_release for payment in payments):
            payments._fifo_reconcile_trigger_cron()
        return payments

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

    # ------------------------------------------------------------------
    # Cron: cada ejecución procesa UN lote de UN pago liberado
    # ------------------------------------------------------------------

    @api.model
    def _cron_fifo_reconcile(self):
        payment = self.sudo().search([
            ('fifo_reconcile_release', '=', True),
            ('fifo_reconcile_state', 'in', ('pending', 'in_progress')),
            ('state', '=', 'posted'),
            ('partner_id.commercial_partner_id.fifo_auto_reconcile', '=', True),
        ], order='date asc, id asc', limit=1)
        if not payment:
            return
        company = payment.company_id
        # Todo corre con la compañía del pago, no con la de la UI ni la del
        # usuario del cron. El cron no trae 'lang' en el contexto, así que se
        # toma el idioma de la compañía para que el chatter salga traducido.
        lang = company.partner_id.lang or self.env.user.lang
        payment.with_company(company).with_context(lang=lang)._fifo_reconcile_run_batch()

    def _fifo_reconcile_run_batch(self):
        self.ensure_one()
        start_time = time.monotonic()
        try:
            # Savepoint externo: si falla el candado de persistencia o el
            # registro del resultado, también se revierte el lote. El interno
            # es el del lote; el candado verifica que de verdad se liberó.
            with self.env.cr.savepoint():
                with self.env.cr.savepoint():
                    result = self._fifo_reconcile_apply_batch()
                self._fifo_reconcile_check_persisted(result)
                self._fifo_reconcile_log_result(result, time.monotonic() - start_time)
        except PsycopgError as error:
            self.env.invalidate_all(flush=False)
            if error.pgcode in CONCURRENCY_ERROR_CODES:
                self._fifo_reconcile_handle_concurrency_error(error)
            else:
                self._fifo_reconcile_handle_error(error)
            return
        except Exception as error:
            self.env.invalidate_all(flush=False)
            self._fifo_reconcile_handle_error(error)
            return
        if result['state'] == 'in_progress':
            # Encadena el siguiente lote de inmediato
            self._fifo_reconcile_trigger_cron()

    def _fifo_reconcile_log_result(self, result, elapsed):
        """Registra el estado y el resumen del lote. Va fuera del savepoint
        del lote, pero dentro del externo: si esta escritura choca por
        concurrencia, se revierte también el lote y se reintenta.
        """
        self.ensure_one()
        state = result['state']
        vals = {'fifo_reconcile_state': state, 'fifo_retry_count': 0}
        if state in FINAL_STATES:
            vals['fifo_reconcile_release'] = False
        self.write(vals)
        self.message_post(body=self._fifo_reconcile_summary_html(result, elapsed))
        # Forzar la escritura aquí para que un conflicto salga dentro del try
        self.env.flush_all()

    @api.model
    def _fifo_reconcile_batch_size(self):
        value = self.env['ir.config_parameter'].sudo().get_param(PARAM_BATCH_SIZE)
        try:
            batch_size = int(value)
        except (TypeError, ValueError):
            return DEFAULT_BATCH_SIZE
        return batch_size if batch_size > 0 else DEFAULT_BATCH_SIZE

    def _fifo_reconcile_invoice_domain(self, receivable_account):
        self.ensure_one()
        # Se busca sobre account.move para que el orden FIFO salga directo de
        # la búsqueda. No usar l10n_mx_edi_payment_policy: no está almacenado
        # y Odoo ignora la condición sin marcar error.
        return [
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
            ('commercial_partner_id', '=', self.partner_id.commercial_partner_id.id),
            ('currency_id', '=', self.currency_id.id),
            ('payment_state', 'in', ('not_paid', 'partial')),
            ('l10n_mx_edi_payment_method_id', '!=', False),
            ('l10n_mx_edi_payment_method_id.code', '!=', EXCLUDED_PAYMENT_METHOD_CODE),
            ('line_ids', 'any', [
                ('account_id', '=', receivable_account.id),
                ('reconciled', '=', False),
            ]),
        ]

    def _fifo_reconcile_apply_batch(self):
        """Aplica un lote del pago contra sus facturas FIFO. Corre dentro de
        un savepoint: cualquier excepción revierte el lote completo.

        Devuelve un dict con el estado resultante y los datos del resumen
        para el chatter.
        """
        self.ensure_one()
        currency = self.company_id.currency_id
        fmt = self._fifo_reconcile_format_amount
        result = {
            'state': False,
            'note': False,
            'pay_line': self.env['account.move.line'],
            'expected_residual': None,
            'residual_after': 0.0,
            'applied': 0.0,
            'moves': self.env['account.move'],
            'line_count': 0,
            'partial_line': self.env['account.move.line'],
            'partial_residual': 0.0,
        }

        # --- Validaciones previas ---
        if self.currency_id != currency:
            raise UserError(_(
                "The payment currency (%(payment)s) must match the company "
                "currency (%(company)s).",
            ) % {'payment': self.currency_id.name, 'company': currency.name})

        pay_lines = self.move_id.line_ids.filtered(
            lambda l: l.account_type == 'asset_receivable' and not l.reconciled
        )
        if not pay_lines:
            result.update(state='done', note=_("The payment has no residual left to reconcile."))
            return result
        if len(pay_lines) > 1:
            raise UserError(_(
                "The payment has %s open receivable lines; exactly one was expected.",
            ) % len(pay_lines))
        pay_line = pay_lines
        receivable_account = pay_line.account_id
        residual_before = -pay_line.amount_residual
        if currency.compare_amounts(residual_before, 0.0) <= 0:
            raise UserError(_(
                "The payment's receivable line residual is not a credit (%s).",
            ) % fmt(pay_line.amount_residual))
        result['pay_line'] = pay_line

        # --- Facturas candidatas en orden FIFO ---
        invoice_domain = self._fifo_reconcile_invoice_domain(receivable_account)
        Move = self.env['account.move']
        invoices = Move.search(
            invoice_domain,
            order='invoice_date asc, sequence_number asc, id asc',
            limit=self._fifo_reconcile_batch_size(),
        )
        if not invoices:
            result.update(state='done_residual', residual_after=residual_before)
            return result

        # --- Recorrido perezoso a nivel línea: factura en orden FIFO y, dentro
        # de ella, sus líneas abiertas por vencimiento. Se detiene en cuanto se
        # agota el residual del pago. Criterio de línea abierta: reconciled =
        # False (así se excluyen las líneas de $0.00 de términos como "1 Día").
        remaining = residual_before
        full_lines = self.env['account.move.line']
        partial_line = self.env['account.move.line']
        moves = self.env['account.move']
        snapshot = []  # [(línea, residual antes)]
        for invoice in invoices:
            inv_lines = invoice.line_ids.filtered(
                lambda l: l.account_id == receivable_account and not l.reconciled
            ).sorted(lambda l: (l.date_maturity or invoice.invoice_date, l.id))
            if not inv_lines:
                raise UserError(_(
                    "Invoice %(invoice)s has no open receivable lines in account %(account)s.",
                ) % {'invoice': invoice.name, 'account': receivable_account.code})
            moves |= invoice
            for inv_line in inv_lines:
                line_residual = inv_line.amount_residual
                if currency.compare_amounts(line_residual, 0.0) <= 0:
                    raise UserError(_(
                        "Invoice %(invoice)s has a receivable line without a "
                        "debit residual (%(residual)s).",
                    ) % {'invoice': invoice.name, 'residual': fmt(line_residual)})
                snapshot.append((inv_line, line_residual))
                if currency.compare_amounts(line_residual, remaining) <= 0:
                    full_lines |= inv_line
                    remaining = currency.round(remaining - line_residual)
                else:
                    partial_line = inv_line
                    remaining = 0.0
                if currency.is_zero(remaining):
                    break
            if currency.is_zero(remaining):
                break

        # --- Conciliación en modo split (fijo): completas y parcial en llamadas
        # separadas. En una sola llamada Odoo 17 no respeta el orden y el
        # residual de la parcial cae en la línea de mayor monto del lote.
        if full_lines:
            (pay_line | full_lines).reconcile()
        if partial_line:
            (pay_line | partial_line).reconcile()
        self.env.flush_all()

        # --- Invariantes: si alguno falla, la excepción revierte el lote ---
        residual_after = -pay_line.amount_residual
        applied = currency.round(residual_before - residual_after)
        invoices_applied = currency.round(sum(
            residual_before_line - line.amount_residual
            for line, residual_before_line in snapshot
        ))
        if partial_line:
            expected_applied = residual_before
        else:
            expected_applied = currency.round(sum(r for _line, r in snapshot))
        if currency.compare_amounts(applied, expected_applied) != 0:
            raise UserError(_(
                "Invariant failed: amount applied to the payment (%(applied)s) "
                "differs from the expected amount (%(expected)s).",
            ) % {'applied': fmt(applied), 'expected': fmt(expected_applied)})
        if currency.compare_amounts(applied, invoices_applied) != 0:
            raise UserError(_(
                "Invariant failed: amount applied to the payment (%(applied)s) "
                "differs from the amount applied to invoices (%(invoices)s).",
            ) % {'applied': fmt(applied), 'invoices': fmt(invoices_applied)})

        # --- Estado resultante ---
        if currency.is_zero(residual_after):
            state = 'done'
        elif Move.search(invoice_domain, limit=1):
            state = 'in_progress'
        else:
            state = 'done_residual'

        result.update(
            state=state,
            expected_residual=residual_after,
            residual_after=residual_after,
            applied=applied,
            moves=moves,
            line_count=len(snapshot),
            partial_line=partial_line,
            partial_residual=partial_line.amount_residual if partial_line else 0.0,
        )
        return result

    def _fifo_reconcile_check_persisted(self, result):
        """Candado de persistencia: al salir del savepoint, relee desde la BD
        el residual del pago y lo compara con el esperado. Se agregó tras un
        incidente en staging donde el lote se revirtió en silencio.
        """
        self.ensure_one()
        expected = result.get('expected_residual')
        if expected is None:
            return
        self.env.invalidate_all()
        persisted = -result['pay_line'].amount_residual
        currency = self.company_id.currency_id
        if currency.compare_amounts(persisted, expected) != 0:
            fmt = self._fifo_reconcile_format_amount
            raise UserError(_(
                "The reconciliation did not persist: residual in database "
                "%(persisted)s, expected %(expected)s.",
            ) % {'persisted': fmt(persisted), 'expected': fmt(expected)})

    def _fifo_reconcile_handle_concurrency_error(self, error):
        self.ensure_one()
        retry_count = self.fifo_retry_count + 1
        if retry_count > MAX_CONCURRENCY_RETRIES:
            self._fifo_reconcile_handle_error(
                error,
                prefix=_("Concurrency conflict persisted after %s retries.") % MAX_CONCURRENCY_RETRIES,
            )
            return
        _logger.warning(
            "FIFO reconcile: concurrency error on payment %s (retry %s/%s), "
            "batch rolled back and rescheduled: %s",
            self.id, retry_count, MAX_CONCURRENCY_RETRIES, error,
        )
        # Sin nota en el chatter: sería ruido y otro punto de conflicto. Si
        # esta escritura choca a su vez, se revierte toda la ejecución y el
        # pago se reintenta en la siguiente corrida sin pérdida.
        self.write({'fifo_retry_count': retry_count})
        self._fifo_reconcile_trigger_cron(at=fields.Datetime.now() + CONCURRENCY_RETRY_DELAY)

    def _fifo_reconcile_handle_error(self, error, prefix=None):
        self.ensure_one()
        if isinstance(error, UserError):
            # Error de validación esperado: no hace falta el traceback completo
            error_text = error.args[0] if error.args else str(error)
            _logger.warning("FIFO reconcile: error on payment %s, batch rolled back: %s", self.id, error_text)
        else:
            error_text = '%s: %s' % (type(error).__name__, error)
            _logger.exception("FIFO reconcile: error on payment %s, batch rolled back", self.id)
        self.write({'fifo_reconcile_state': 'error', 'fifo_reconcile_release': False})
        body = Markup('<p><b>%s</b></p>') % _("FIFO reconcile error. The batch was rolled back.")
        if prefix:
            body += Markup('<p>%s</p>') % prefix
        body += Markup('<p>%s</p>') % error_text
        self.message_post(body=body)

    def _fifo_reconcile_format_amount(self, amount):
        currency = self.company_id.currency_id
        # Redondear y sumar 0.0 evita el "-0.00" (cero negativo) al formatear
        return format_amount(self.env, currency.round(amount) + 0.0, currency)

    def _fifo_reconcile_summary_html(self, result, elapsed):
        self.ensure_one()
        fmt = self._fifo_reconcile_format_amount
        if result['note']:
            return Markup('<p>%s</p>') % result['note']
        no_invoices_left = Markup('<p>%s</p>') % (
            _("No eligible invoices left. Unapplied payment residual: %s")
            % fmt(result['residual_after'])
        )
        moves = result['moves']
        if not moves:
            return no_invoices_left

        partial_line = result['partial_line']
        if partial_line:
            partial_text = _("%(invoice)s (due %(due)s), residual %(residual)s") % {
                'invoice': partial_line.move_id.name,
                'due': fields.Date.to_string(partial_line.date_maturity) or '-',
                'residual': fmt(result['partial_residual']),
            }
        else:
            partial_text = _("None")
        items = [
            _("Invoices applied: %(invoices)s (lines: %(lines)s)") % {
                'invoices': len(moves), 'lines': result['line_count'],
            },
            _("Amount applied: %s") % fmt(result['applied']),
            _("First invoice: %s") % moves[0].name,
            _("Last invoice: %s") % moves[-1].name,
            _("Partial invoice: %s") % partial_text,
            _("Remaining payment residual: %s") % fmt(result['residual_after']),
            _("Batch time: %.2f s") % elapsed,
        ]
        body = Markup('<p><b>%s</b> (%s)</p><ul>%s</ul>') % (
            _("FIFO reconcile batch"),
            _("max batch size: %s") % self._fifo_reconcile_batch_size(),
            Markup('').join(Markup('<li>%s</li>') % item for item in items),
        )
        if result['state'] == 'done_residual':
            body += no_invoices_left
        return body
