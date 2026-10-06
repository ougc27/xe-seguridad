import json
import logging
from datetime import timedelta

from odoo import _, fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Fix 2026-10-02 (user-caught, real production case: job volume nearly
# doubling, and — more seriously — two genuinely concurrent 'paid'
# pack siblings each creating their own separate sale.order, packs
# 2000015308820231/2000015308879705): queue_job's own identity_key
# dedup only ever catches a duplicate notification while the ORIGINAL
# job is still 'pending' in the queue. Mercado Libre re-sends the same
# notification within seconds as a matter of course (confirmed live:
# the same order_id generating 2-3 import jobs 5-90 seconds apart) —
# with a single worker, the original job was still sitting in the
# queue when the repeat arrived, so identity_key caught it every time.
# Running two workers made the original job finish almost instantly,
# so by the time the repeat notification lands, identity_key has
# nothing left to dedupe against and a brand new job gets created.
# This grace window closes that gap at the one place common to every
# notification topic (the webhook itself, before any job ever gets
# created) rather than chasing it inside each individual job — a
# repeat for the exact same identity_key within this many seconds of
# the first one is always the same Mercado Libre event, never a
# genuine second state change worth its own job.
MELI_NOTIFICATION_DEDUP_WINDOW_SECONDS = 30


class MeliOAuthController(http.Controller):

    def _recent_duplicate_job_exists(self, identity_key):
        # Fix 2026-10-06 (user-caught, real production case: packs
        # 2000015317436757/orders 2000018759123602, two genuinely
        # duplicate sale.order records — S1002713/S1002718 — each with
        # its own real stock movement/fiscal document split between
        # them): a plain SELECT here is NOT atomic with the with_delay()
        # INSERT that follows it in the caller — two webhook requests
        # arriving in the same instant (confirmed live: both resulting
        # queue.job rows share the exact same identity_key AND the exact
        # same date_created, down to the second) each run this SELECT
        # before either one's own job INSERT has committed, so both see
        # "no duplicate yet" and both proceed — the classic check-then-
        # act race, impossible to close with a tighter time window alone
        # since the window was never the problem; the missing lock was.
        # pg_advisory_xact_lock(hashtext(identity_key)) serializes any
        # two concurrent requests for the SAME identity_key — the second
        # one blocks here until the first's entire transaction (request)
        # commits, at which point its own job row is genuinely visible,
        # making this check correct instead of racy. Same proven
        # technique sale.order._meli_create_from_order_data already uses
        # for its own pack-consolidation check — scoped by identity_key
        # here instead of pack_id/order_id, since this guard runs for
        # every notification topic, not just order imports.
        request.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (identity_key,),
        )
        cutoff = fields.Datetime.now() - timedelta(
            seconds=MELI_NOTIFICATION_DEDUP_WINDOW_SECONDS,
        )
        return bool(request.env['queue.job'].sudo().search_count([
            ('identity_key', '=', identity_key),
            ('date_created', '>=', cutoff),
        ]))

    @http.route(
        '/meli/callback', type='http', auth='user', csrf=False,
        methods=['GET'],
    )
    def meli_callback(self, **kw):
        code = kw.get('code')
        state = kw.get('state')
        session_state = request.session.get('meli_oauth_state')
        config_id = request.session.get('meli_oauth_config_id')
        verifier = request.session.get('meli_oauth_verifier')

        valid = bool(
            code and state and session_state and config_id and verifier
            and state == session_state
        )
        if not valid:
            return request.make_response(
                _(
                    "Invalid or expired connection request. Please try "
                    "again from the 'Connect to Mercado Libre' button in "
                    "Odoo."
                ),
                status=400,
            )

        request.session.pop('meli_oauth_state', None)
        request.session.pop('meli_oauth_verifier', None)
        request.session.pop('meli_oauth_config_id', None)

        config = request.env['meli.config'].browse(int(config_id)).exists()
        if not config:
            return request.make_response(
                _(
                    "The configured connection no longer exists. Please "
                    "try again from the 'Connect to Mercado Libre' button "
                    "in Odoo."
                ),
                status=400,
            )
        config._exchange_code_for_token(code, verifier)

        return request.redirect(
            f'/web#model=meli.config&id={config.id}&view_type=form'
        )

    @http.route(
        '/meli/notifications', type='http', auth='public', csrf=False,
        methods=['POST'],
    )
    def meli_notifications(self, **kw):
        # Mercado Libre requires HTTP 200 within ~500ms or it may mark the
        # notification (and eventually the topic subscription) as failed.
        # All heavy work — the GET to /orders/$ID and the sale.order
        # creation — happens in a queue_job, never synchronously here.
        try:
            payload = json.loads(request.httprequest.data or b'{}')
        except ValueError:
            _logger.warning("Mercado Libre notification with invalid JSON body.")
            return request.make_response('', status=200)

        topic = payload.get('topic')
        resource = payload.get('resource') or ''
        # Re-enabled 2026-09-22 (user-directed): this app was an
        # invoicing-only build since 2026-09-14, deliberately never
        # creating a sale order on its own — VentiApp did that instead,
        # and this connector only ever adopted what already existed.
        # Now that _meli_import_order/_meli_create_from_order_data are
        # mature enough (shipping-va, coupon pricing, Deremate/1P
        # billing, pack-sibling handling, refacturación — all built and
        # tested this same cycle), Mercado Libre's own 'orders_v2' topic
        # is wired straight to that same, already-proven entry point.
        # 'post_purchase' (meli.claim/reclamos) is deliberately still
        # left out — that model doesn't exist in this build yet; a
        # separate, later increment.
        if topic == 'orders_v2' and resource:
            order_id = resource.rstrip('/').split('/')[-1]
            config = self._find_config_by_ml_user_id(payload)
            if config:
                identity_key = f"meli_import_order_{order_id}"
                if not self._recent_duplicate_job_exists(identity_key):
                    request.env['sale.order'].sudo().with_delay(
                        # priority=0 (2026-10-02, user-directed): every job
                        # this connector enqueues now runs at the same, top
                        # priority — this queue has no other consumer worth
                        # deprioritizing against.
                        priority=0, channel='root.meli_sales', max_retries=8,
                        description=f"Import Mercado Libre order {order_id}",
                        identity_key=identity_key,
                    )._meli_import_order(config.company_id.id, order_id)
            else:
                _logger.warning(
                    "Mercado Libre notification for unknown ml_user_id %s "
                    "(order %s) — no matching meli.config.",
                    payload.get('user_id'), order_id,
                )
        elif topic == 'invoices' and resource:
            # resource is always /users/$USER_ID/invoices/$INVOICE_ID for
            # this topic (no sub-resource suffix documented, unlike
            # post_purchase's claims_actions) — the invoice id is simply
            # the last path segment.
            invoice_id = resource.rstrip('/').split('/')[-1]
            config = self._find_config_by_ml_user_id(payload)
            if config:
                identity_key = f"meli_import_invoice_{invoice_id}"
                if not self._recent_duplicate_job_exists(identity_key):
                    request.env['meli.invoice.document'].sudo().with_delay(
                        # priority=0 (2026-10-02, user-directed): every job
                        # this connector enqueues now runs at the same, top
                        # priority — this queue has no other consumer worth
                        # deprioritizing against.
                        priority=0, channel='root.meli_sales', max_retries=8,
                        description=f"Import Mercado Libre invoice {invoice_id}",
                        identity_key=identity_key,
                    )._meli_import_invoice_document(config.company_id.id, invoice_id)
            else:
                _logger.warning(
                    "Mercado Libre notification for unknown ml_user_id %s "
                    "(invoice %s) — no matching meli.config.",
                    payload.get('user_id'), invoice_id,
                )
        return request.make_response('', status=200)

    def _find_config_by_ml_user_id(self, payload):
        ml_user_id = str(payload.get('user_id') or '')
        if not ml_user_id:
            return request.env['meli.config'].sudo()
        return request.env['meli.config'].sudo().search(
            [('ml_user_id', '=', ml_user_id)], limit=1,
        )
