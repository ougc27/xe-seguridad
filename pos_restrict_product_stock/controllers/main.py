# -*- coding: utf-8 -*-
import pytz

from odoo.addons.point_of_sale.controllers.main import PosController

from odoo import http, _
from odoo.http import request
from datetime import timedelta, datetime
from odoo.tools import format_amount
from odoo.exceptions import UserError


class PosControllerInherit(PosController):

    @http.route(['/pos/ticket'], type='http', auth="public", website=True, sitemap=False)
    def invoice_request_screen(self, **kwargs):
        errors = {}
        form_values = {}
        if request.httprequest.method == 'POST':
            for field in ['pos_reference', 'date_order', 'ticket_code']:
                if not kwargs.get(field):
                    errors[field] = " "
                else:
                    form_values[field] = kwargs.get(field)

            if errors:
                errors['generic'] = _("Please fill all the required fields.")
            elif len(form_values['pos_reference']) < 14:
                errors['pos_reference'] = _("The Ticket Number should be at least 14 characters long.")
            else:
                date_order = datetime(*[int(i) for i in form_values['date_order'].split('-')])
                order = request.env['pos.order'].sudo().search([
                    ('pos_reference', '=like', '%' + form_values['pos_reference'].replace('%', r'\%').replace('_', r'\_')),
                    ('date_order', '>=', date_order),
                    ('date_order', '<', date_order + timedelta(days=1)),
                    ('ticket_code', '=', form_values['ticket_code']),
                ], limit=1)
                if order:
                    return request.redirect('/pos/ticket/validate?access_token=%s' % (order.access_token))
                else:
                    errors['generic'] = _("No sale order found.")

        return request.render("point_of_sale.ticket_request_with_code", {
            'errors': errors,
            'banner_error': " ".join(errors.values()),
            'form_values': form_values,
        })

    @http.route(['/pos/ticket/validate'], type='http', auth="public", website=True, sitemap=False)
    def show_ticket_validation_screen(self, access_token='', **kwargs):
        def _parse_additional_values(fields, prefix, kwargs):
            """ Parse the values in the kwargs by extracting the ones matching the given fields name.
            :return a dict with the parsed value and the field name as key, and another on with the prefix to
            re-render the form with previous values if needed.
            """
            res, res_prefixed = {}, {}
            for field in fields:
                key = prefix + field.name
                if key in kwargs:
                    val = kwargs.pop(key)
                    res[field.name] = val
                    res_prefixed[key] = val
            return res, res_prefixed

        # If the route is called directly, return a 404
        if not access_token:
            return request.not_found()
        # Get the order using the access token. We can't use the id in the route because we may not have it yet when the QR code is generated.
        pos_order = request.env['pos.order'].sudo().search([('access_token', '=', access_token)])
        if not pos_order:
            return request.not_found()

        is_date_expired = False
        limit_date = pos_order.date_order + timedelta(days=2)
        now = datetime.now().date()
        if now > limit_date.date():
            is_date_expired = True

        # Set the proper context in case of unauthenticated user accessing
        # from the main company website
        pos_order = pos_order.with_company(pos_order.company_id)

        # If the order was already invoiced, return the invoice directly by forcing the access token so that the non-connected user can see it.
        if pos_order.account_move and pos_order.account_move.is_sale_document():
            return request.redirect('/my/invoices/%s?access_token=%s' % (pos_order.account_move.id, pos_order.account_move._portal_ensure_token()))

        pos_order_country = pos_order.company_id.account_fiscal_country_id
        additional_partner_fields = request.env['res.partner'].get_partner_localisation_fields_required_to_invoice(pos_order_country)
        additional_invoice_fields = request.env['account.move'].get_invoice_localisation_fields_required_to_invoice(pos_order_country)

        user_is_connected = not request.env.user._is_public()

        # Validate the form by ensuring required fields are filled and the VAT is correct.
        form_values = {'error': {}, 'error_message': {}, 'extra_field_values': {}}
        if kwargs and request.httprequest.method == 'POST':
            form_values.update(kwargs)
            # Extract the additional fields values from the kwargs now as they can't be there when validating the 'regular' partner form.
            partner_values, prefixed_partner_values = _parse_additional_values(additional_partner_fields, 'partner_', kwargs)
            form_values['extra_field_values'].update(prefixed_partner_values)
            # Do the same for invoice values, separately as they are only needed for the invoice creation.
            invoice_values, prefixed_invoice_values = _parse_additional_values(additional_invoice_fields, 'invoice_', kwargs)
            form_values['extra_field_values'].update(prefixed_invoice_values)
            # Check the basic form fields if the user is not connected as we will need these information to create the new user.
            if not user_is_connected:
                error, error_message = self.details_form_validate(kwargs, partner_creation=True)
            else:
                # Check that the billing information of the user are filled.
                error, error_message = {}, []
                partner = request.env.user.partner_id
                for field in self.MANDATORY_BILLING_FIELDS:
                    if not partner[field]:
                        error[field] = 'error'
                        error_message.append(_('The %s must be filled in your details.', request.env['ir.model.fields']._get('res.partner', field).field_description))
            # Check that the "optional" additional fields are filled.
            error, error_message = self.extra_details_form_validate(partner_values, additional_partner_fields, error, error_message)
            error, error_message = self.extra_details_form_validate(invoice_values, additional_invoice_fields, error, error_message)

            if request.env['res.partner']._run_vat_test(
                form_values.get('vat'), pos_order_country, True) is False:
                error['vat'] = 'error'
            if not error:
                return self._get_invoice(partner_values, invoice_values, pos_order, additional_invoice_fields, kwargs)
            else:
                form_values.update({'error': error, 'error_message': error_message})

        # Most of the time, the country of the customer will be the same as the order. We can prefill it by default with the country of the company.
        if 'country_id' not in form_values:
            form_values['country_id'] = pos_order_country.id

        partner = request.env['res.partner']
        # Prefill the customer extra values if there is any and an user is connected
        partner = (user_is_connected and request.env.user.partner_id) or pos_order.partner_id
        if partner:
            if additional_partner_fields:
                form_values['extra_field_values'] = {'partner_' + field.name: partner[field.name] for field in additional_partner_fields if field.name not in form_values['extra_field_values']}

            # This is just to ensure that the user went and filled its information at least once.
            # Another more thorough check is done upon posting the form.
            if not partner.country_id or not partner.street:
                form_values['partner_address'] = False
            else:
                form_values['partner_address'] = partner._display_address()

        return request.render("point_of_sale.ticket_validation_screen", {
            'partner': partner,
            'address_url': f'/my/account?redirect=/pos/ticket/validate?access_token={access_token}',
            'user_is_connected': user_is_connected,
            'format_amount': format_amount,
            'env': request.env,
            'countries': request.env['res.country'].sudo().search([]),
            'states': request.env['res.country.state'].sudo().search([]),
            'partner_can_edit_vat': True,
            'pos_order': pos_order,
            'invoice_required_fields': additional_invoice_fields,
            'partner_required_fields': additional_partner_fields,
            'access_token': access_token,
            **form_values,
            'is_date_expired': is_date_expired
        })

    def _get_invoice(self, partner_values, invoice_values, pos_order, additional_invoice_fields, kwargs):
        # If the user is not connected, then we will simply create a new partner with the form values.
        # Matching with existing partner was tried, but we then can't update the values, and it would force the user to use the ones from the first invoicing.
        move_partner = False
        pos_partner_id = pos_order.partner_id
        if request.env.user._is_public() and not pos_partner_id.id:
            partner_values.update({key: kwargs[key] for key in self._get_mandatory_fields()})
            partner_values.update({key: kwargs[key] for key in self._get_optional_fields() if key in kwargs})
            for field in {'country_id', 'state_id'} & set(partner_values.keys()):
                try:
                    partner_values[field] = int(partner_values[field])
                except Exception:
                    partner_values[field] = False
            partner_values.update({'zip': partner_values.pop('zipcode', '')})
            partner = request.env['res.partner'].sudo().create(partner_values)  # In this case, partner_values contains the whole partner info form.
        # If the user is connected, then we can update if needed its fields with the additional localized fields if any, then proceed.
        else:
            partner = pos_partner_id or (not request.env.user._is_public() and request.env.user.partner_id)
            partner.write(partner_values)  # In this case, partner_values only contains the additional fields that can be updated.
            partner_values.update({key: kwargs[key] for key in self._get_mandatory_fields()})
            partner_values.update({key: kwargs[key] for key in self._get_optional_fields() if key in kwargs})
            for field in {'country_id', 'state_id'} & set(partner_values.keys()):
                try:
                    partner_values[field] = int(partner_values[field])
                except Exception:
                    partner_values[field] = False
            partner_values.update({'zip': partner_values.pop('zipcode', '')})

            partner_values.update({
                'parent_id': pos_partner_id.id,
                'type': 'invoice'
            })
            vat = partner_values.pop('vat', '')

            existing_invoice_contact = request.env['res.partner'].sudo().search([
                ('parent_id', '=', pos_order.partner_id.id),
                ('type', '=', 'invoice'),
                ('vat', '=', vat)
            ], limit=1)

            if existing_invoice_contact:
                existing_invoice_contact.write(partner_values)
                move_partner = existing_invoice_contact
            else:
                move_partner = request.env['res.partner'].sudo().create(partner_values)
                move_partner.sudo().write({'vat': vat})
        pos_order.partner_id = partner
        # Get the required fields for the invoice and add them to the context as default values.
        with_context = {}
        for field in additional_invoice_fields:
            with_context.update({f'default_{field.name}': invoice_values.get(field.name)})
        # Allowing default values for moves is important for some localizations that would need specific fields to be set on the invoice, such as Mexico.
        with_context.update({'from_portal': True})
        try:
            pos_order.with_context(with_context).action_pos_order_invoice(move_partner)
        except UserError as e:
            if pos_order.account_move:
                pos_order.account_move.sudo().button_draft()
                pos_order.account_move.sudo().button_cancel()
            pos_order.account_move = False
            return request.render("point_of_sale.ticket_validation_screen", {
                'partner': pos_order.partner_id,
                'address_url': f'/my/account?redirect=/pos/ticket/validate?access_token={pos_order.access_token}',
                'user_is_connected': not request.env.user._is_public(),
                'format_amount': format_amount,
                'env': request.env,
                'countries': request.env['res.country'].sudo().search([]),
                'states': request.env['res.country.state'].sudo().search([]),
                'partner_can_edit_vat': True,
                'pos_order': pos_order,
                'invoice_required_fields': additional_invoice_fields,
                'partner_required_fields': request.env['res.partner'].get_partner_localisation_fields_required_to_invoice(
                    pos_order.company_id.account_fiscal_country_id
                ),
                'access_token': pos_order.access_token,
                'is_date_expired': False,
                'error': {},
                'error_message': [e],
                'extra_field_values': {},
            })
        return request.redirect('/my/invoices/%s?access_token=%s' % (pos_order.account_move.id, pos_order.account_move._portal_ensure_token()))
