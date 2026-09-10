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
        elif topic == 'post_purchase' and resource:
            claim_id = self._meli_extract_claim_id(resource)
            if not claim_id:
                _logger.warning(
                    "Mercado Libre post_purchase notification with "
                    "unexpected resource %r — could not extract a claim "
                    "id, skipping.", resource,
                )
                return request.make_response('', status=200)
            config = self._find_config_by_ml_user_id(payload)
            if config:
                request.env['meli.claim'].sudo().with_delay(
                    priority=5, channel='root.meli_sales', max_retries=8,
                    description=f"Import Mercado Libre claim {claim_id}",
                    identity_key=f"meli_import_claim_{claim_id}",
                )._meli_import_claim(config.company_id.id, claim_id)
            else:
                _logger.warning(
                    "Mercado Libre notification for unknown ml_user_id %s "
                    "(claim %s) — no matching meli.config.",
                    payload.get('user_id'), claim_id,
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

    @staticmethod
    def _meli_extract_claim_id(resource):
        """resource is always .../claims/{claim_id}[/<sub-resource>] for
        both post_purchase subtopics — "claims" (resource ends in the
        claim id) and "claims_actions" (resource has a trailing
        sub-resource segment, e.g. "actions-history", confirmed in
        production 2026-09-02). The claim id is the path segment right
        after "claims", never simply the LAST segment — taking the last
        segment silently grabbed "actions-history" as the claim id on a
        claims_actions notification, which then 404'd against
        /post-purchase/v1/claims/actions-history.
        """
        parts = [p for p in resource.rstrip('/').split('/') if p]
        try:
            idx = parts.index('claims')
        except ValueError:
            return None
        if idx + 1 >= len(parts):
            return None
        return parts[idx + 1]
