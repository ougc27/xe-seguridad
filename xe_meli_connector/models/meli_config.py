import base64
import hashlib
import logging
import secrets

from urllib.parse import urlencode

from odoo import _, api, fields, models
from odoo.http import request as http_request

from odoo.addons.queue_job.exception import RetryableJobError

import requests

_logger = logging.getLogger(__name__)

TOKEN_URL = 'https://api.mercadolibre.com/oauth/token'
AUTHORIZE_URL = 'https://auth.mercadolibre.com.mx/authorization'
API_BASE_URL = 'https://api.mercadolibre.com'
DEFAULT_TIMEOUT = 30

# Confirmed with the user (2026-08-31, after a real 39h outage caused
# by a single transient DNS failure during a proactive refresh): the
# access token lasts 6 hours, and the refresh cron runs every 2 hours
# (see data/ir_cron.xml) — so 3 consecutive failures span one full
# token lifetime. Only after that many consecutive failures has the
# token actually expired anyway, so nothing is lost by finally giving
# up and requiring a human to reconnect.
MELI_REFRESH_FAILURE_THRESHOLD = 3

# HTTP statuses Mercado Libre can return that are known to be transient —
# retrying (with backoff) is the correct response, not failing permanently.
# Confirmed 2026-09-09 from real production tracebacks: 429 (rate limit),
# 504 (gateway timeout). 500/502/503 added defensively (generic transient
# server errors) — any other status (401 handled separately above; 403/404
# mean something is actually wrong and must keep failing immediately).
MELI_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

# Confirmed with the user (2026-09-10, after an 8600-document invoice
# rescue self-inflicted a burst of API traffic against Mercado Libre —
# see docs/superpowers/specs/2026-09-10-meli-cancellation-transaction-fix-design.md
# for the retry_pattern bug this compounded with): both Excel batch
# wizards (meli.import.batch.wizard, meli.invoice.import.batch.wizard)
# stagger their own job enqueues by this many seconds per row instead of
# firing all of them at once — 3 seconds = 20 requests/minute, chosen as
# a conservative pace well under what Mercado Libre's own rate limiting
# (per Client ID, across all endpoints — see their public FAQ on
# error 429) can sustain, while still finishing a few thousand rows in a
# few hours rather than a full day. Adjust here if experience shows this
# can safely go faster.
MELI_BATCH_IMPORT_SECONDS_BETWEEN_JOBS = 3


class MeliConfig(models.Model):
    _name = 'meli.config'
    _inherit = ['mail.thread']
    _description = 'Mercado Libre Connection'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    client_id = fields.Char(
        string='Client ID', groups='xe_meli_connector.group_meli_admin', copy=False,
    )
    client_secret = fields.Char(
        string='Client Secret', groups='xe_meli_connector.group_meli_admin', copy=False,
    )
    access_token = fields.Char(
        string='Access Token', groups='xe_meli_connector.group_meli_admin', copy=False,
    )
    refresh_token = fields.Char(
        string='Refresh Token', groups='xe_meli_connector.group_meli_admin', copy=False,
    )
    token_expires_at = fields.Datetime(string='Token Expires At')
    consecutive_refresh_failures = fields.Integer(
        string='Consecutive Refresh Failures', default=0, copy=False,
        help="Number of network-level refresh failures in a row (DNS, "
             "timeout, connection refused). Resets to 0 on any "
             "successful refresh. Only reaching "
             f"{MELI_REFRESH_FAILURE_THRESHOLD} in a row marks the "
             "connection as 'Error' — a single transient network blip "
             "no longer permanently disables the integration.",
    )
    site_id = fields.Char(string='Site ID', default='MLM')
    ml_user_id = fields.Char(string='ML User ID', readonly=True)
    state = fields.Selection([
        ('not_connected', 'Not Connected'),
        ('connected', 'Connected'),
        ('error', 'Error'),
    ], string='Status', default='not_connected', required=True)
    last_error = fields.Text(string='Last Error')
    active = fields.Boolean(default=True)
    last_connected_at = fields.Datetime(string='Last Connected At')
    last_refreshed_at = fields.Datetime(string='Last Refreshed At')
    last_poll_at = fields.Datetime(
        string='Last Poll At', copy=False,
        help="End of the window the backup polling cron last checked "
             "successfully. The next run always resumes from here — not "
             "a fixed lookback — so an outage of any length (webhook "
             "disabled, server down, etc.) never leaves a permanent gap.",
    )
    last_claim_poll_at = fields.Datetime(
        string='Last Claim Poll At', copy=False,
        help="Same checkpoint idea as last_poll_at, for the claims "
             "backup polling cron — see _poll_recent_claims.",
    )
    redirect_uri = fields.Char(
        string='Redirect URI', compute='_compute_redirect_uri',
        help="Copy this exact value into the app's redirect_uri in the "
             "Mercado Libre DevCenter.",
    )

    partner_id = fields.Many2one(
        'res.partner', string='Mercado Libre Customer',
        help="Generic contact used as the customer on sales orders "
             "imported from Mercado Libre.",
    )
    sale_team_id = fields.Many2one(
        'crm.team', string='Sales Team',
        help="Sales team assigned to imported orders.",
    )
    salesperson_id = fields.Many2one(
        'res.users', string='Salesperson',
        help="Fixed salesperson assigned to imported orders.",
    )
    returns_manager_id = fields.Many2one(
        'res.users', string='Returns Manager',
        help="Person notified when Mercado Libre reports a return, "
             "refund, or cancellation that needs a look — a new claim "
             "on a non-Full order, or an order status change to "
             "'partially_refunded', 'pending_cancel', or 'cancelled' "
             "that couldn't be finished automatically. Deliberately "
             "separate from the order's salesperson.",
    )
    delivery_contact_manager_id = fields.Many2one(
        'res.users', string='Delivery Contact Manager',
        help="Mentioned in the chatter when a custom-shipping order's "
             "delivery contact (recipient's real name/phone/address) "
             "could not be resolved. Falls back to the order's own "
             "salesperson if left empty.",
    )
    warehouse_fulfillment_id = fields.Many2one(
        'stock.warehouse', string='Fulfillment Warehouse (Full)',
        help="Warehouse used when the order's shipment is Full "
             "(logistic_type = fulfillment).",
    )
    warehouse_default_id = fields.Many2one(
        'stock.warehouse', string='Default Warehouse (non-Full)',
        help="Warehouse used for any other shipment type (Mercado "
             "Envíos, Traditional, etc.).",
    )
    failure_notify_user_ids = fields.Many2many(
        'res.users', string='Failure Notification Recipients',
        help="Notified (chatter + their own email/inbox preference) "
             "every time one of this connector's automations couldn't "
             "complete by itself and needs manual review — e.g. an "
             "order that failed to auto-confirm, a stuck invoicing "
             "reconciliation, etc. In addition to whoever the message "
             "already targets (the order's salesperson, the Queue Job "
             "Manager group) — not a replacement for them.",
    )
    shipping_item_id = fields.Many2one(
        'product.product', string='Shipping Item',
        domain=[('type', '=', 'service')],
        help="Service product used for the shipping-surcharge line "
             "added to orders whose Mercado Libre shipment is 'custom' "
             "(the seller manages logistics directly — today only XE's "
             "oversized security doors). Mirrors Ventiapp's 'Ítem de "
             "envío' setting, but configurable instead of hardcoded. "
             "Note: xe_pacific's qty_delivered sync and cancel handling "
             "for this line still match on the literal default_code "
             "'SHIPPING-VA' — changing this field to a product with a "
             "different default_code requires updating that logic too.",
    )

    _sql_constraints = [(
        'company_id_uniq', 'unique(company_id)',
        'Only one Mercado Libre connection is allowed per company.',
    )]

    def _compute_redirect_uri(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', ''
        )
        for record in self:
            record.redirect_uri = f"{base_url}/meli/callback"

    def _exchange_code_for_token(self, code, code_verifier):
        self.ensure_one()
        self._request_token({
            'grant_type': 'authorization_code',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': code,
            'redirect_uri': self.redirect_uri,
            'code_verifier': code_verifier,
        }, is_refresh=False)

    def _refresh_token(self):
        self.ensure_one()
        self._request_token({
            'grant_type': 'refresh_token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
        }, is_refresh=True)

    def _request_token(self, payload, is_refresh):
        self.ensure_one()
        try:
            response = requests.post(TOKEN_URL, data=payload, timeout=DEFAULT_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            if is_refresh:
                # Tolerating a few consecutive failures only makes sense
                # for an already-connected record: _cron_refresh_tokens
                # keeps retrying it on its own next tick as long as
                # state stays 'connected'. The initial code exchange
                # (is_refresh=False) has no such retry — it's a one-time,
                # human-initiated action — so it must still report
                # 'error' immediately on any failure.
                self._handle_network_failure(f"Connection error: {exc}")
            else:
                # No notify call here: this is the initial OAuth code
                # exchange, driven live by a human sitting at the
                # "Conectar con Mercado Libre" button — the controller
                # already reports the failure straight to them, so an
                # admin-mention would just be noise about something
                # nobody left unattended.
                self.write({
                    'state': 'error',
                    'consecutive_refresh_failures': 0,
                    'last_error': f"Connection error: {exc}",
                })
            return
        if response.status_code != 200:
            error_message = f"HTTP {response.status_code}: {self._safe_error_body(response)}"
            was_error = self.state == 'error'
            self.write({
                'state': 'error',
                'consecutive_refresh_failures': 0,
                'last_error': error_message,
            })
            if not was_error:
                self._meli_notify_admins_of_disconnection(error_message)
            return
        try:
            data = response.json()
        except ValueError:
            error_message = "Malformed response from Mercado Libre (not valid JSON)."
            was_error = self.state == 'error'
            self.write({
                'state': 'error',
                'consecutive_refresh_failures': 0,
                'last_error': error_message,
            })
            if not was_error:
                self._meli_notify_admins_of_disconnection(error_message)
            return
        if not data.get('access_token') or not data.get('refresh_token'):
            missing = [k for k in ('access_token', 'refresh_token') if not data.get(k)]
            error_message = f"Malformed token response from Mercado Libre (missing: {missing})."
            was_error = self.state == 'error'
            self.write({
                'state': 'error',
                'consecutive_refresh_failures': 0,
                'last_error': error_message,
            })
            if not was_error:
                self._meli_notify_admins_of_disconnection(error_message)
            return
        now = fields.Datetime.now()
        values = {
            'access_token': data.get('access_token'),
            'refresh_token': data.get('refresh_token'),
            'token_expires_at': fields.Datetime.add(
                now, seconds=data.get('expires_in', 21600),
            ),
            'ml_user_id': str(data.get('user_id') or self.ml_user_id or ''),
            'state': 'connected',
            'consecutive_refresh_failures': 0,
            'last_error': False,
        }
        values['last_refreshed_at' if is_refresh else 'last_connected_at'] = now
        self.write(values)

    def _handle_network_failure(self, error_message):
        """A transient network-layer failure (DNS, timeout, connection
        refused) reaching Mercado Libre's token endpoint — as opposed
        to Mercado Libre itself rejecting the request. Never flips
        `state` on its own below the threshold: the record is left
        exactly as it was (normally 'connected'), so the existing
        `_cron_refresh_tokens` filter on state == 'connected' already
        retries it on its own next tick, with no extra scheduling
        logic needed. Only MELI_REFRESH_FAILURE_THRESHOLD consecutive
        network failures in a row escalate to 'error' — seeing this
        many in a row means the token has certainly expired by now
        anyway (see MELI_REFRESH_FAILURE_THRESHOLD's own comment).
        """
        self.ensure_one()
        was_error = self.state == 'error'
        failures = self.consecutive_refresh_failures + 1
        values = {
            'consecutive_refresh_failures': failures,
            'last_error': error_message,
        }
        reached_error = failures >= MELI_REFRESH_FAILURE_THRESHOLD
        if reached_error:
            values['state'] = 'error'
            # Reset here too (mirrors every other error branch in
            # _request_token): otherwise a still-flaky reconnection
            # attempt made while already 'error' keeps climbing past
            # the threshold forever, showing a misleading "N consecutive
            # failures" on the form and re-tripping the escalation (and,
            # without the was_error guard below, re-notifying) on every
            # single subsequent attempt instead of just the first one.
            values['consecutive_refresh_failures'] = 0
        self.write(values)
        if reached_error and not was_error:
            self._meli_notify_admins_of_disconnection(error_message)

    def _meli_notify_admins_of_disconnection(self, reason):
        """Posts a chatter message mentioning every user in
        group_meli_admin — the exact group that already controls who
        can see Configuración, so it's the right audience by
        construction. Called the moment `state` actually transitions
        to 'error', never on a below-threshold transient failure (that
        would just be noise): closes the detection gap from the real
        2026-08-30 incident, where the connection sat broken for 39
        hours because nobody was told, not because retrying more would
        have helped.
        """
        self.ensure_one()
        admins = self.env.ref('xe_meli_connector.group_meli_admin').users
        if not admins:
            _logger.warning(
                "Mercado Libre connection for company %s reached 'error' "
                "state, but no user has the group_meli_admin group "
                "assigned yet -- nobody was mentioned in the chatter.",
                self.company_id.name,
            )
        self.message_post(
            body=_(
                "The Mercado Libre connection for %(company)s is now "
                "in <b>Error</b> state and needs manual attention."
                "<br/>Reason: %(reason)s"
            ) % {'company': self.company_id.name, 'reason': reason},
            partner_ids=admins.mapped('partner_id').ids,
        )

    def _safe_error_body(self, response):
        try:
            body = dict(response.json())
        except (ValueError, TypeError):
            text = (response.text or '')[:300]
            if self.client_secret:
                text = text.replace(self.client_secret, '***')
            return text
        body.pop('client_secret', None)
        return body

    def action_connect(self):
        self.ensure_one()
        verifier = secrets.token_urlsafe(43)
        challenge = self._pkce_challenge(verifier)
        state = secrets.token_urlsafe(24)

        http_request.session['meli_oauth_state'] = state
        http_request.session['meli_oauth_verifier'] = verifier
        http_request.session['meli_oauth_config_id'] = self.id

        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'state': state,
            'scope': 'offline_access read write',
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }
        return {
            'type': 'ir.actions.act_url',
            'url': f"{AUTHORIZE_URL}?{urlencode(params)}",
            'target': 'self',
        }

    @staticmethod
    def _pkce_challenge(verifier):
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()

    def action_refresh_now(self):
        self.ensure_one()
        self._refresh_token()
        success = self.state == 'connected'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _("Connected successfully.") if success else self.last_error,
                'type': 'success' if success else 'danger',
            },
        }

    def action_toggle_active(self):
        """Kill switch: turning this off makes the webhook controller find
        no config (silently ignores notifications), the token-refresh and
        polling crons skip this record entirely, and any queued job for
        this company fails with a clear error instead of doing anything —
        all three already filter on `active` today. The confirmation
        dialog lives on the button itself (`confirm=` in the view).
        """
        self.ensure_one()
        self.active = not self.active

    @api.model
    def action_open_meli_orders(self):
        """Window action for the 'Pedidos' menu: scoped to whichever
        partner(s) are actually configured as the Mercado Libre customer
        across all connections, computed live instead of a hardcoded
        partner id in the domain (safe if partner_id ever changes, and
        multi-company/multi-account-safe by construction).
        """
        partner_ids = self.with_context(active_test=False).search([]).mapped('partner_id').ids
        return {
            'type': 'ir.actions.act_window',
            'name': _('Orders'),
            'res_model': 'sale.order',
            'view_mode': 'tree,form',
            'domain': [('partner_id', 'in', partner_ids)],
        }

    def _api_get(self, path, params=None, headers=None):
        self.ensure_one()
        return self._api_request('GET', path, params=params, headers=headers)

    def _api_get_raw(self, path, params=None, headers=None):
        """Like _api_get, but returns the raw response bytes instead of
        parsing JSON — for endpoints that stream a file (e.g. the
        Mercado Libre native invoicer's XML/PDF documents) rather than a
        JSON payload.
        """
        self.ensure_one()
        response = self._api_response('GET', path, params=params, headers=headers)
        return response.content

    def _api_request(self, method, path, params=None, headers=None, retry_on_401=True):
        self.ensure_one()
        response = self._api_response(
            method, path, params=params, headers=headers, retry_on_401=retry_on_401,
        )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _api_response(self, method, path, params=None, headers=None, retry_on_401=True):
        """Shared by _api_request (JSON) and _api_get_raw (binary/XML
        streams) — does the actual HTTP call, the 401-refresh-and-retry
        dance, and raise_for_status, returning the raw requests.Response.

        Transient failures (rate limiting, gateway timeouts, connection
        blips) are re-raised as RetryableJobError so queue_job's own
        retry machinery actually engages — confirmed 2026-09-09 that
        queue_job ONLY retries on that specific exception type; every
        other exception fails the job permanently on the very first
        attempt, which is what was happening to every 429/504/SSL error
        today. A genuine 4xx (other than 429) or any other exception
        keeps failing immediately, unretried — retrying those would
        never help.
        """
        self.ensure_one()
        request_headers = {'Authorization': f'Bearer {self.access_token}'}
        request_headers.update(headers or {})
        try:
            response = requests.request(
                method, f'{API_BASE_URL}{path}', headers=request_headers,
                params=params, timeout=DEFAULT_TIMEOUT,
            )
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as err:
            raise RetryableJobError(
                f"Transient network error calling Mercado Libre "
                f"({method} {path}): {err}",
                seconds=30,
            ) from err
        if response.status_code == 401 and retry_on_401:
            self._refresh_token()
            if self.state == 'connected':
                return self._api_response(
                    method, path, params=params, headers=headers,
                    retry_on_401=False,
                )
        if response.status_code in MELI_TRANSIENT_HTTP_STATUSES:
            retry_after = response.headers.get('Retry-After')
            seconds = (
                int(retry_after) if retry_after and retry_after.isdigit()
                else None
            )
            raise RetryableJobError(
                f"Mercado Libre returned {response.status_code} for "
                f"{method} {path} — transient, will retry.",
                seconds=seconds,
            )
        response.raise_for_status()
        return response

    @api.model
    def _cron_refresh_tokens(self):
        configs = self.search([
            ('active', '=', True), ('state', '=', 'connected'),
        ])
        for config in configs:
            try:
                config._refresh_token()
            except Exception:
                _logger.exception(
                    "Failed to refresh Mercado Libre token for config %s",
                    config.id,
                )

    @api.model
    def _cron_poll_orders(self):
        """Re-enabled 2026-09-22 (user-directed): backup polling for the
        'orders_v2' webhook (see controllers/main.py's own comment) —
        catches a paid order whose notification was never delivered or
        failed. Was disabled along with the webhook branch itself for
        the 2026-09-14 invoicing-only build; the underlying
        _poll_recent_orders/_retry_failed_delivery_contacts methods and
        their own checkpoint fields (last_poll_at, _POLL_DEFAULT_
        LOOKBACK_HOURS below) were never removed.
        """
        configs = self.search([
            ('active', '=', True), ('state', '=', 'connected'),
        ])
        for config in configs:
            try:
                config._poll_recent_orders()
            except Exception:
                _logger.exception(
                    "Mercado Libre order polling failed for config %s",
                    config.id,
                )
            try:
                config._retry_failed_delivery_contacts()
            except Exception:
                _logger.exception(
                    "Mercado Libre delivery-contact retry failed for "
                    "config %s", config.id,
                )

    @api.model
    def _cron_poll_invoices(self):
        configs = self.search([
            ('active', '=', True), ('state', '=', 'connected'),
        ])
        for config in configs:
            try:
                config._poll_recent_invoices()
            except Exception:
                _logger.exception(
                    "Mercado Libre invoice reconciliation failed for config %s",
                    config.id,
                )

    _POLL_DEFAULT_LOOKBACK_HOURS = 2
    _POLL_OVERLAP_MINUTES = 15

    def _poll_recent_orders(self):
        """Safety-net polling: catches paid orders whose webhook
        notification was never delivered or failed. The webhook is the
        primary path — this only fills gaps.

        The window is checkpoint-based, not a fixed lookback: it always
        resumes from `last_poll_at` (the end of the last successful run),
        with a small overlap to absorb ML's hour-granularity search filter
        and any clock skew. This guarantees no permanent gap regardless of
        how long an outage lasts (webhook disabled, server down, etc.) —
        the only requirement is that the cron eventually runs again. Only
        the very first run ever (no checkpoint yet) falls back to a fixed
        lookback. The checkpoint only advances after the search call
        succeeds, so a failed run is retried from the same point next time.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        since = self.last_poll_at or fields.Datetime.subtract(
            now, hours=self._POLL_DEFAULT_LOOKBACK_HOURS,
        )
        since = fields.Datetime.subtract(since, minutes=self._POLL_OVERLAP_MINUTES)
        data = self._api_get('/orders/search', params={
            'seller': self.ml_user_id,
            'order.status': 'paid',
            'order.date_last_updated.from': since.strftime('%Y-%m-%dT%H:00:00.000-00:00'),
        }) or {}
        SaleOrder = self.env['sale.order'].sudo()
        for result in data.get('results', []):
            order_id = str(result.get('id'))
            existing = SaleOrder.search([('meli_order_id', '=', order_id)], limit=1)
            # Fix 1 (2026-09-09, final review — Critical): `and not
            # existing.meli_adopted` added — an order this connector
            # merely ADOPTED (linked to, never created) must never be
            # re-enqueued here even if it happens to sit in draft with
            # meli_sync_source set. _meli_retry_unmapped_lines's own
            # guard already refuses to act on such an order (see that
            # method), so this would be a no-op anyway — excluding it
            # here too avoids a wasted API call and log noise on every
            # polling cycle for as long as it stays in draft.
            if existing and not (
                existing.state == 'draft' and existing.meli_sync_source
                and not existing.meli_adopted
            ):
                # Already imported and resolved — nothing to do. A draft
                # stuck on an unmapped SKU is worth another look, in case
                # the mapping was completed since it was first created.
                continue
            # priority=0 (was 8, 2026-09-24 user-directed): this
            # connector is the only real consumer of this queue —
            # creating a sale is never lower priority than anything
            # else in it.
            SaleOrder.with_delay(
                priority=0, channel='root.meli_sales', max_retries=8,
                description=f"Import Mercado Libre order {order_id} (polling)",
                identity_key=f"meli_import_order_{order_id}",
            )._meli_import_order(self.company_id.id, order_id)
        self.last_poll_at = now

    def _retry_failed_delivery_contacts(self):
        """Companion to _poll_recent_orders, called from the same
        _cron_poll_orders tick: retries the delivery-contact resolution
        for every custom-shipping order still stuck in
        meli_delivery_contact_status == 'failed', with no attempt limit.

        Each order is retried in its own try/except: one order raising
        an unexpected error must not skip every other failed order in
        this company for this tick — they'd otherwise have to wait for
        whatever broke this one order to also clear up first, which is
        unrelated. _cron_poll_orders' own try/except only stops a single
        bad config from blocking every OTHER company; this is the same
        idea one level down, per order.
        """
        self.ensure_one()
        orders = self.env['sale.order'].sudo().search([
            ('company_id', '=', self.company_id.id),
            ('meli_delivery_contact_status', '=', 'failed'),
        ])
        for order in orders:
            try:
                order._meli_retry_delivery_contact(self)
            except Exception:
                _logger.exception(
                    "Mercado Libre delivery-contact retry failed for "
                    "order %s", order.id,
                )

    _MISSED_FEEDS_PAGE_LIMIT = 100

    def _meli_missed_invoice_ids(self):
        """Paginates GET /missed_feeds for the 'invoices' topic —
        notifications Mercado Libre tried to deliver (up to 8 retries
        over 1 hour) but never got an HTTP 200 for. This is the ONLY
        way to recover a genuinely lost 'invoices' webhook: unlike
        orders/claims, there is no resource-level search endpoint for
        invoices (confirmed against the real API and the docs,
        2026-09-04) — /missed_feeds is queried instead, since each
        entry carries the original notification's `resource`
        (/users/$USER_ID/invoices/$INVOICE_ID), which is the only place
        the invoice_id ever appears on its own.

        Mercado Libre only retains missed notifications for 2 days, and
        a GET here does not consume/clear them — the same invoice_id
        keeps reappearing on every call for the rest of that window.
        _poll_recent_invoices is responsible for not re-enqueueing one
        we've already successfully imported.
        """
        self.ensure_one()
        invoice_ids = []
        offset = 0
        while True:
            data = self._api_get('/missed_feeds', params={
                'app_id': self.client_id, 'topic': 'invoices',
                'offset': offset, 'limit': self._MISSED_FEEDS_PAGE_LIMIT,
            }) or {}
            messages = data.get('messages') or []
            for message in messages:
                resource = (message.get('resource') or '').rstrip('/')
                invoice_id = resource.split('/')[-1] if resource else ''
                if invoice_id:
                    invoice_ids.append(invoice_id)
            if len(messages) < self._MISSED_FEEDS_PAGE_LIMIT:
                break
            offset += self._MISSED_FEEDS_PAGE_LIMIT
        return invoice_ids

    def _poll_recent_invoices(self):
        """Safety-net reconciliation for the native invoicer's
        'invoices' webhook — see _meli_missed_invoice_ids for why this
        goes through /missed_feeds rather than a search-by-date
        endpoint (there isn't one). Skips any invoice_id we've already
        stored a document for, since Mercado Libre keeps listing the
        same missed notification for its whole 2-day retention window
        regardless of whether we already handled it.
        """
        self.ensure_one()
        invoice_ids = self._meli_missed_invoice_ids()
        if not invoice_ids:
            return
        Document = self.env['meli.invoice.document'].sudo()
        already_known = set(Document.search([
            ('meli_invoice_id', 'in', invoice_ids),
        ]).mapped('meli_invoice_id'))
        for invoice_id in invoice_ids:
            if invoice_id in already_known:
                continue
            Document.with_delay(
                # Priority 6 (vs. the 8 used by order/claim polling) —
                # invoices got de-prioritized by default and lagged
                # behind, per the user 2026-09-04.
                priority=6, channel='root.meli_sales', max_retries=8,
                description=(
                    f"Import Mercado Libre invoice {invoice_id} "
                    f"(missed_feeds reconciliation)"
                ),
                identity_key=f"meli_import_invoice_{invoice_id}",
            )._meli_import_invoice_document(self.company_id.id, invoice_id)

