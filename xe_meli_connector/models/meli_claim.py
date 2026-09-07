import logging

from odoo import _, api, fields, models

import requests

_logger = logging.getLogger(__name__)

# Only these claim types are handled in this phase — a return or a
# cancellation initiated by either side. Everything else Mercado Libre
# reports on the post_purchase topic (quality disputes, payment
# disputes, product changes, delayed-shipment cases) is deliberately
# out of scope for now (see the "Reversas de Mercado Libre" design doc,
# section "Fuera de alcance").
# Mercado Libre's own docs are inconsistent about the spelling of the
# return type: the /claims/$CLAIM_ID and /claims/search reference pages
# describe it as singular "return", but a real example payload in the
# /claims/search docs shows "type": "returns" (plural). Accept both on
# the way in, but always store the canonical singular value (see
# _meli_import_claim) so the rest of the module only ever deals with one
# spelling.
IN_SCOPE_CLAIM_TYPES = ('return', 'returns', 'cancel_sale', 'cancel_purchase')


class MeliClaim(models.Model):
    _name = 'meli.claim'
    _description = 'Mercado Libre Claim (Return/Cancellation)'
    _order = 'create_date desc'

    claim_id = fields.Char(
        string='Mercado Libre Claim ID', required=True, copy=False,
    )
    meli_order_id = fields.Char(
        string='Mercado Libre Order ID', copy=False,
        help="resource_id from the claim, when resource == 'order' — "
             "always the individual order id (needed to reliably link "
             "to its sale.order), never the pack id. See meli_pack_id "
             "below for the id a person would actually recognize from "
             "Ventiapp or the Mercado Libre portal when this order is "
             "part of a cart.",
    )
    meli_pack_id = fields.Char(
        string='Mercado Libre Pack ID', copy=False,
        help="This order's pack id, when it's part of a cart — fetched "
             "with one extra, best-effort call to /orders/$ORDER_ID at "
             "import time (2026-09-07: meli_order_id alone doesn't match "
             "what Ventiapp/the Mercado Libre portal show as 'the' order "
             "number for a pack order, which is the pack id, not the "
             "individual order id).",
    )
    sale_order_id = fields.Many2one(
        'sale.order', string='Sale Order', compute='_compute_sale_order_id',
        store=True,
    )
    company_id = fields.Many2one(
        'res.company', string='Company', related='sale_order_id.company_id',
        store=True,
    )
    claim_type = fields.Selection([
        ('return', 'Return'),
        ('cancel_sale', 'Cancelled by Seller'),
        ('cancel_purchase', 'Cancelled by Buyer'),
    ], string='Claim Type', required=True)
    claim_status = fields.Selection([
        ('opened', 'Opened'),
        ('closed', 'Closed'),
    ], string='Claim Status', required=True)
    stage = fields.Char(
        string='Stage',
        help="Raw stage from Mercado Libre: claim, dispute, recontact, stale, or none.",
    )
    resolution_reason = fields.Char(
        string='Resolution Reason',
        help="resolution.reason from Mercado Libre, once the claim is closed.",
    )
    reason_code = fields.Char(
        string='Reason Code',
        help="Raw reason_id from Mercado Libre (e.g. PDD9549) — why the "
             "claim was opened by the complainant. Category prefixes: "
             "PNR (Producto No Recibido), PDD (Producto Diferente o "
             "Defectuoso), CS (Compra Cancelada). Not the same as "
             "resolution_reason above, which is how the claim was closed.",
    )
    reason_name = fields.Char(
        string='Reason Name',
        help="name from /post-purchase/v1/claims/reasons/$REASON_ID, "
             "resolved from reason_code — best-effort, blank if the "
             "lookup ever fails.",
    )
    reason_detail = fields.Char(
        string='Reason Detail',
        help="detail (human-readable, Spanish) from the same reasons lookup.",
    )
    affects_reputation = fields.Selection([
        ('affected', 'Affects Reputation'),
        ('not_affected', 'Does Not Affect Reputation'),
        ('not_applies', 'Not Applicable'),
    ], string='Affects Reputation')
    reputation_has_incentive = fields.Boolean(
        string='Still Within Grace Window',
        help="True while there is still time (typically 48h from "
             "reputation_due_date) to respond without the seller's "
             "reputation being affected, even if affects_reputation "
             "currently says 'affected'.",
    )
    reputation_due_date = fields.Datetime(
        string='Reputation Due Date',
        help="Deadline to respond before the reputation impact becomes final.",
    )
    is_full = fields.Boolean(
        string='Is Full',
        help="Whether the related order was fulfilled from the Full "
             "warehouse, as of when this claim was processed — a "
             "snapshot, not a live value.",
    )
    last_synced_at = fields.Datetime(string='Last Synced At')
    claim_date_created = fields.Datetime(
        string='Claim/Return Date',
        help="date_created from Mercado Libre — when this claim or "
             "return was actually opened on their side (not when this "
             "record was synced into Odoo, see Last Synced At above).",
    )
    meli_portal_url = fields.Char(
        string='Mercado Libre Portal Link', compute='_compute_meli_portal_url',
        help="Direct link to this claim's own chat/messaging thread on "
             "Mercado Libre's seller portal — confirmed working pattern "
             "(2026-09-07): "
             "https://vendedores.mercadolibre.com.mx/ventas/nueva/"
             "mensajeria/$ORDER_ID/reclamo/$CLAIM_ID. MLM-only, like the "
             "rest of this module.",
    )

    _sql_constraints = [(
        'claim_id_uniq', 'unique(claim_id)',
        'This Mercado Libre claim is already registered.',
    )]

    @api.depends('meli_order_id', 'meli_pack_id')
    def _compute_sale_order_id(self):
        """Matches by meli_order_id first (orders imported by
        xe_meli_connector), falling back to client_order_ref/reference —
        same two-step lookup as meli.invoice.document._compute_sale_order_id.
        Without this fallback, claims never match legacy Ventiapp-created
        orders (the vast majority of historical orders), since those
        never populate meli_order_id at all (found in practice
        2026-09-04: most rows in the Returns & Cancellations list had no
        sale_order_id for exactly this reason, not because of any real
        data problem).

        A pack order adds a further wrinkle (found in practice
        2026-09-07): a legacy Ventiapp order stores the PACK id in
        client_order_ref/reference, not the individual order id — so
        for a claim on an order that's part of a pack, the two lookups
        above both miss, even though the order genuinely exists. Falls
        back to meli_pack_id in that case, checked the same two ways.
        """
        SaleOrder = self.env['sale.order']
        for claim in self:
            order = SaleOrder.browse()
            if claim.meli_order_id:
                order = SaleOrder.search(
                    [('meli_order_id', '=', claim.meli_order_id)], limit=1,
                )
                if not order:
                    order = SaleOrder.search([
                        '|',
                        ('client_order_ref', '=', claim.meli_order_id),
                        ('reference', '=', claim.meli_order_id),
                    ], limit=1)
            if not order and claim.meli_pack_id:
                order = SaleOrder.search(
                    [('meli_pack_id', '=', claim.meli_pack_id)], limit=1,
                )
                if not order:
                    order = SaleOrder.search([
                        '|',
                        ('client_order_ref', '=', claim.meli_pack_id),
                        ('reference', '=', claim.meli_pack_id),
                    ], limit=1)
            claim.sale_order_id = order

    @api.depends('meli_order_id', 'meli_pack_id', 'claim_id')
    def _compute_meli_portal_url(self):
        for claim in self:
            # The portal always keys a pack order's messaging thread by the
            # PACK id, never the individual order id (found in practice
            # 2026-09-08: a claim on a pack order 404'd when linked with
            # meli_order_id — same wrinkle meli_pack_id itself exists for,
            # see that field's help text).
            portal_id = claim.meli_pack_id or claim.meli_order_id
            claim.meli_portal_url = (
                'https://vendedores.mercadolibre.com.mx/ventas/nueva/'
                f'mensajeria/{portal_id}/reclamo/{claim.claim_id}'
                if portal_id and claim.claim_id else False
            )

    @api.model
    def _meli_import_claim(self, company_id, claim_id):
        """Entry point for the /meli/notifications webhook (post_purchase
        topic, claims/claims_actions filters). Fetches the claim,
        silently ignores anything outside the scope of this phase (a
        claim type we don't handle yet, or one not tied to an order),
        and upserts a meli.claim record. Notifies the configured
        returns manager the first time a non-Full claim is seen opened
        — never for Full (handled by automation instead, in a later
        phase) and never twice for the same claim.
        """
        claim_id = str(claim_id)
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            _logger.info(
                "No connected Mercado Libre config found for company %s, "
                "skipping claim %s.", company_id, claim_id,
            )
            return self.browse()

        claim_data = config._api_get(f'/post-purchase/v1/claims/{claim_id}')
        if claim_data.get('resource') != 'order':
            _logger.info(
                "Mercado Libre claim %s has resource %r (not 'order'), "
                "skipping.", claim_id, claim_data.get('resource'),
            )
            return self.browse()

        raw_type = claim_data.get('type')
        if raw_type not in IN_SCOPE_CLAIM_TYPES:
            _logger.info(
                "Mercado Libre claim %s has out-of-scope type %r, "
                "skipping.", claim_id, raw_type,
            )
            return self.browse()
        # ML's docs use both "return" and "returns" for the same claim
        # type (see the module-level comment on IN_SCOPE_CLAIM_TYPES) —
        # normalize to the singular canonical value stored on the record.
        claim_type = 'return' if raw_type == 'returns' else raw_type

        existing = self.sudo().search([('claim_id', '=', claim_id)], limit=1)
        was_open_before = bool(existing) and existing.claim_status == 'opened'

        meli_order_id = str(claim_data.get('resource_id') or '')
        sale_order = self.env['sale.order']
        if meli_order_id:
            sale_order = sale_order.search(
                [('meli_order_id', '=', meli_order_id)], limit=1,
            )
            if not sale_order:
                # Legacy Ventiapp-created orders never set meli_order_id
                # — same fallback as _compute_sale_order_id, needed here
                # too so is_full (and the "no matching order" log below)
                # aren't silently wrong for the majority of historical orders.
                sale_order = sale_order.search([
                    '|',
                    ('client_order_ref', '=', meli_order_id),
                    ('reference', '=', meli_order_id),
                ], limit=1)

        meli_pack_id = False
        if meli_order_id:
            meli_pack_id = self._meli_fetch_pack_id(config, meli_order_id)
        if not sale_order and meli_pack_id:
            # Same pack wrinkle as _compute_sale_order_id: a legacy
            # Ventiapp order stores the PACK id in client_order_ref/
            # reference, not this order's own individual id.
            sale_order = sale_order.search(
                [('meli_pack_id', '=', meli_pack_id)], limit=1,
            )
            if not sale_order:
                sale_order = sale_order.search([
                    '|',
                    ('client_order_ref', '=', meli_pack_id),
                    ('reference', '=', meli_pack_id),
                ], limit=1)
        is_full = False
        if sale_order:
            # Claims and the order they reference always belong to the
            # same company in practice; only fall back to a fresh lookup
            # if that ever isn't the case, rather than assuming it blindly.
            order_config = (
                config if config.company_id == sale_order.company_id
                else self.env['meli.config'].sudo().search(
                    [('company_id', '=', sale_order.company_id.id)], limit=1,
                )
            )
            is_full = bool(
                order_config and order_config.warehouse_fulfillment_id
                and sale_order.warehouse_id == order_config.warehouse_fulfillment_id
            )

        reason_code = claim_data.get('reason_id')
        reason_info = self._meli_reason_lookup(config, reason_code)
        reputation_info = self._meli_fetch_affects_reputation(config, claim_id)

        vals = {
            'claim_id': claim_id,
            'meli_order_id': meli_order_id,
            'claim_type': claim_type,
            'claim_status': claim_data.get('status'),
            'stage': claim_data.get('stage'),
            'resolution_reason': (claim_data.get('resolution') or {}).get('reason'),
            'is_full': is_full,
            'last_synced_at': fields.Datetime.now(),
        }
        claim_date_created = self.env['sale.order']._meli_parse_datetime(
            claim_data.get('date_created')
        )
        if claim_date_created:
            vals['claim_date_created'] = claim_date_created
        # Enrichment fields (reason name/detail, reputation impact) are
        # best-effort — only overwrite when the corresponding lookup
        # actually returned something, so a transient failure on a
        # re-import (e.g. a status-change notification) never wipes out
        # a previously-fetched, still-valid value.
        if reason_code:
            vals['reason_code'] = reason_code
        if reason_info.get('reason_name'):
            vals['reason_name'] = reason_info['reason_name']
        if reason_info.get('reason_detail'):
            vals['reason_detail'] = reason_info['reason_detail']
        if reputation_info.get('affects_reputation'):
            vals['affects_reputation'] = reputation_info['affects_reputation']
            vals['reputation_has_incentive'] = reputation_info.get('reputation_has_incentive', False)
            vals['reputation_due_date'] = reputation_info.get('reputation_due_date')
        if meli_pack_id:
            vals['meli_pack_id'] = meli_pack_id
        if existing:
            existing.write(vals)
            claim = existing
        else:
            claim = self.sudo().create(vals)

        is_newly_opened = claim.claim_status == 'opened' and not was_open_before
        if is_newly_opened and not claim.is_full:
            if claim.sale_order_id:
                claim._meli_notify_returns_manager(config)
            else:
                _logger.info(
                    "Mercado Libre claim %s references order %s with no "
                    "matching sale.order, notification skipped.",
                    claim_id, meli_order_id,
                )
        return claim

    @api.model
    def _meli_fetch_pack_id(self, config, order_id):
        """GET /orders/$ORDER_ID just for its pack_id — resource_id on a
        claim is always the individual order id, but a person cross-
        referencing against Ventiapp or the Mercado Libre portal expects
        the pack id instead whenever the order is part of a cart (found
        in practice 2026-09-07). Best effort, same as the reason/
        reputation lookups above: never blocks the claim import.
        """
        try:
            data = config._api_get(f'/orders/{order_id}')
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not fetch pack_id for Mercado Libre order %s.",
                order_id,
            )
            return False
        pack_id = data.get('pack_id')
        return str(pack_id) if pack_id else False

    @api.model
    def _meli_reason_lookup(self, config, reason_code):
        """Enriches a raw reason_id code (e.g. "PDD9549") with its
        human-readable name/detail via
        /post-purchase/v1/claims/reasons/$REASON_ID. Self-caches by
        reusing whatever an earlier claim already resolved for the same
        code — there are only a few hundred codes in the whole catalog,
        so this avoids a repeat API call for the same reason. Best
        effort: a lookup failure never blocks the claim import (the raw
        reason_code is still stored regardless).
        """
        if not reason_code:
            return {}
        cached = self.sudo().search([
            ('reason_code', '=', reason_code), ('reason_name', '!=', False),
        ], limit=1)
        if cached:
            return {'reason_name': cached.reason_name, 'reason_detail': cached.reason_detail}
        try:
            data = config._api_get(f'/post-purchase/v1/claims/reasons/{reason_code}')
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not fetch details for Mercado Libre claim reason %s.",
                reason_code,
            )
            return {}
        return {'reason_name': data.get('name'), 'reason_detail': data.get('detail')}

    @api.model
    def _meli_fetch_affects_reputation(self, config, claim_id):
        """GET /post-purchase/v1/claims/$CLAIM_ID/affects-reputation —
        whether this claim impacts the seller's reputation, and (via
        has_incentive) whether there's still a grace window to respond
        without it counting against them. Best effort, same as the
        reason lookup above: never blocks the claim import.
        """
        try:
            data = config._api_get(
                f'/post-purchase/v1/claims/{claim_id}/affects-reputation'
            )
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not fetch reputation impact for Mercado Libre claim %s.",
                claim_id,
            )
            return {}
        return {
            'affects_reputation': data.get('affects_reputation'),
            'reputation_has_incentive': bool(data.get('has_incentive')),
            'reputation_due_date': self.env['sale.order']._meli_parse_datetime(
                data.get('due_date')
            ),
        }

    def _meli_notify_returns_manager(self, config):
        self.ensure_one()
        if not config.returns_manager_id:
            return
        claim_type_label = dict(
            self._fields['claim_type'].selection
        ).get(self.claim_type, self.claim_type)
        self.sale_order_id._meli_post_with_mention(
            _(
                "Mercado Libre reports a new %(claim_type)s claim "
                "(%(claim_id)s) on this order. Review it, and once the "
                "return is physically received, handle stock and "
                "invoicing manually."
            ) % {'claim_type': claim_type_label, 'claim_id': self.claim_id},
            mention_partner=config.returns_manager_id.partner_id,
        )
