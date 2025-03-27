from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_pos_store = fields.Boolean()

    shipping_instructions = fields.Char(
        'Shipping Instructions',
        copy=False)

    @api.depends('street', 'zip', 'city', 'country_id', 'l10n_mx_edi_locality')
    def _compute_complete_address(self):
        for record in self:
            record.contact_address_complete = ''
            if record.street:
                record.contact_address_complete += record.street + ', '
            if record.zip:
                record.contact_address_complete += record.zip + ' '
            if record.city:
                record.contact_address_complete += record.city + ', '
            if record.state_id:
                record.contact_address_complete += record.state_id.name + ', '
            if record.l10n_mx_edi_locality_id:
                record.contact_address_complete += 'Localidad: ' + record.l10n_mx_edi_locality_id.name + ', '
            if record.country_id:
                record.contact_address_complete += record.country_id.name
            record.contact_address_complete = record.contact_address_complete.strip().strip(',')
