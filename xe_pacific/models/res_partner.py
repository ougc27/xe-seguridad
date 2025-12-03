from odoo import models, fields, api
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_pos_store = fields.Boolean()

    shipping_instructions = fields.Char(
        'Shipping Instructions',
        copy=False)
    
    commercial_name = fields.Char(copy=False)

    x_cop = fields.Selection([
        ('prospecto', 'Prospecto'),
        ('cliente', 'Cliente'),
        ('proveedor', 'Proveedor'),
        ('cliente_proveedor', 'Cliente y proveedor'),
        ('otro', 'Otro'),
    ], string="Tipo")

    active = fields.Boolean(default=True, tracking=True)

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

    @api.model
    def _restricted_company_partner_fields(self, vals):
        restricted_fields = {
            'name',
            'vat',
            'fiscal_name',
            'phone',
            'mobile',
            'email',
            'website'
        }
        fields_attempted = restricted_fields.intersection(vals.keys())

        if not fields_attempted:
            return
        
        user = self.env.user

        if not user.has_group("base.group_system"):
            field_list = ", ".join(fields_attempted)
            raise UserError(("No tienes permisos para modificar los siguientes datos del contacto: %s") % field_list)

    def write(self, vals):
        for partner in self:
            if partner.sudo().ref_company_ids:
                partner._restricted_company_partner_fields(vals)
        return super().write(vals)
