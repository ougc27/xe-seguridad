import json
import logging

from odoo import _, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MeliOAuthController(http.Controller):

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
                request.env['sale.order'].sudo().with_delay(
                    priority=5, channel='root.meli_sales', max_retries=8,
                    description=f"Import Mercado Libre order {order_id}",
                    identity_key=f"meli_import_order_{order_id}",
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
                request.env['meli.invoice.document'].sudo().with_delay(
                    # Priority 3 (vs. the 5 used by order/claim webhooks)
                    # — invoices got de-prioritized by default and lagged
                    # behind, per the user 2026-09-04.
                    priority=3, channel='root.meli_sales', max_retries=8,
                    description=f"Import Mercado Libre invoice {invoice_id}",
                    identity_key=f"meli_import_invoice_{invoice_id}",
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
