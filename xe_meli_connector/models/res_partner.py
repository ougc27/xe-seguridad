from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    meli_buyer_id = fields.Char(
        string='Mercado Libre Buyer ID', copy=False, index=True,
        help="Mercado Libre's numeric buyer ID (from the order's "
             "buyer.id). Identifies the 'master' contact created for a "
             "Mercado Libre buyer on custom-shipping orders (XE manages "
             "delivery itself) — see "
             "res.partner._meli_find_or_create_delivery_contact(). Left "
             "empty on every other contact.",
    )

    def _meli_delivery_contact_signature(self):
        self.ensure_one()
        return (
            self.name, self.phone or False, self.mobile or False,
            self.street or False, self.city or False, self.zip or False,
            self.state_id.id, self.country_id.id,
        )

    @api.model
    def _meli_resolve_country(self, country_code):
        if not country_code:
            return self.env['res.country']
        return self.env['res.country'].search(
            [('code', '=', country_code)], limit=1,
        )

    @api.model
    def _meli_resolve_state(self, country, state_code, state_name):
        """Mercado Libre's state code/name for a Mexican address (e.g.
        code "DIF", name "Distrito Federal") isn't guaranteed to match
        Odoo's own res.country.state records verbatim. Tries an exact
        code match first, then a case-insensitive EXACT name match, and
        gives up (empty recordset) rather than risk assigning the wrong
        state — an empty field is safer than a silently wrong one. Uses
        '=ilike' (exact, case-insensitive), not 'ilike' (substring): a
        substring match against real Odoo data ("México" vs "Ciudad de
        México") would silently pick the wrong one of the two ordered
        first, exactly the "silently wrong" outcome this method exists
        to avoid.
        """
        if not country:
            return self.env['res.country.state']
        if state_code:
            state = self.env['res.country.state'].search([
                ('country_id', '=', country.id), ('code', '=', state_code),
            ], limit=1)
            if state:
                return state
        if state_name:
            return self.env['res.country.state'].search([
                ('country_id', '=', country.id),
                ('name', '=ilike', state_name),
            ], limit=1)
        return self.env['res.country.state']

    @api.model
    def _meli_find_or_create_delivery_contact(self, buyer_id, destination):
        """buyer_id: Mercado Libre's numeric buyer id, as a string
        (matches res.partner.meli_buyer_id). destination: the dict
        returned by sale.order._meli_fetch_custom_shipping_destination,
        or None.

        Returns the delivery-address contact (res.partner,
        type='delivery') to use as partner_shipping_id, or an empty
        recordset if buyer_id or destination (or its 'name') is
        missing — the caller falls back to the generic Mercado Libre
        contact in that case.

        See docs/superpowers/specs/2026-09-01-meli-delivery-contact-design.md
        for why the master contact's name is set only once (at
        creation) and why an existing delivery child is only ever
        reused on an EXACT match, never edited:
        stock.picking.partner_id is a live relation, not a snapshot, so
        editing a delivery contact already used on a dispatched picking
        would silently rewrite that delivery's recorded address.
        """
        buyer_id = (buyer_id or '').strip()
        if not buyer_id or not destination or not destination.get('name'):
            return self.env['res.partner']

        # active_test=False: an archived master (e.g. a one-off contact
        # someone cleaned up manually) must still be found here, or the
        # next order for the same buyer would silently create a second,
        # duplicate master under the same meli_buyer_id.
        master = self.with_context(active_test=False).search(
            [('meli_buyer_id', '=', buyer_id)], limit=1,
        )
        if not master:
            master = self.create({
                'name': destination['name'],
                'meli_buyer_id': buyer_id,
                'company_type': 'person',
            })

        country = self._meli_resolve_country(destination.get('country_code'))
        state = self._meli_resolve_state(
            country, destination.get('state_code'), destination.get('state_name'),
        )
        phone = destination.get('phone') or False
        candidate_values = {
            'name': destination['name'],
            'phone': phone,
            'mobile': phone,
            'street': destination.get('street') or False,
            'city': destination.get('city') or False,
            'zip': destination.get('zip') or False,
            'state_id': state.id if state else False,
            'country_id': country.id if country else False,
        }
        candidate_signature = (
            candidate_values['name'], candidate_values['phone'],
            candidate_values['mobile'], candidate_values['street'],
            candidate_values['city'], candidate_values['zip'],
            candidate_values['state_id'], candidate_values['country_id'],
        )
        existing_children = self.search([
            ('parent_id', '=', master.id), ('type', '=', 'delivery'),
        ])
        for child in existing_children:
            if child._meli_delivery_contact_signature() == candidate_signature:
                return child
        return self.create({
            **candidate_values,
            'parent_id': master.id,
            'type': 'delivery',
        })
