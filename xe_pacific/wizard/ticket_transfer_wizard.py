from odoo import models, fields, _
from odoo.exceptions import UserError


class TicketTransferWizard(models.TransientModel):
    _name = 'ticket.transfer.wizard'
    _description = 'Transfer Wizard desde Helpdesk'

    ticket_id = fields.Many2one('helpdesk.ticket', readonly=True)
    user_id = fields.Many2one('res.users', readonly=True)
    service_warehouse_id = fields.Many2one('stock.warehouse', readonly=True)
    partner_id = fields.Many2one('res.partner', readonly=True)
    crm_team_id = fields.Many2one('crm.team', readonly=True)
    lot = fields.Char(readonly=True)
    block = fields.Char(readonly=True)
    subdivision = fields.Char(readonly=True)
    shipping_assignment = fields.Selection([
        ('logistics', 'Logistics'),
        ('shipments', 'Shipments')], required=True)

    def add_notes_to_transfer(self, ticket_id, picking_id):
        fields_to_include = [
            ('partner_id.name', _('Client')),
            ('architect', _('Architect')),
            ('lot', _('Lot')),
            ('block', _('Block')),
            ('subdivision', _('Subdivision')),
            ('complete_address', _('Client Address')),
            ('phone', _('Contact Phone')),
            ('product_id.display_name', _('Service for')),
            ('description', _('Description')),
        ]

        note_lines = []

        for field_path, label in fields_to_include:
            value = ticket_id
            for attr in field_path.split('.'):
                value = getattr(value, attr, None)
                if not value:
                    break
            if value:
                note_lines.append(f"{label}: {value}")

        if ticket_id.remission_incident and ticket_id.picking_id.x_studio_folio_rem:
            note_lines.append(f"{_('Incident in remission')}: {ticket_id.picking_id.x_studio_folio_rem}")

        detalle_fields = {
            'mechanism_review': _('Mechanism Review'),
            'accessory_install_pending': _('Accessories Pending Installation'),
            'accessory_delivery_pending': _('Accessories Pending Delivery'),
            'spare_part_replacement': _('Spare Part Replacement'),
            'loose_broken_gaskets': _('Loose or broken gaskets'),
            'damaged_interior_profile': _('Damaged Interior Profile'),
            'missing_caps': _('Missing Caps'),
            'customer_locked': _('Client Did Not Provide Access'),
            'smart_lock_inspection': _('Smart Lock Inspection'),
            'smart_lock_replacement': _('Smart Lock Replacement'),
            'deficient_masonry': _('Deficient Masonry'),
            'loose_door_frame': _('Loose Door Frame'),
            'door_installation': _('Door Installation'),
            'door_reinstallation': _('Door Reinstallation'),
            'door_replacement': _('Door Replacement'),
            'door_dent': _('Door Dent'),
            'paint_factory_defect': _('Factory Paint Defect'),
            'door_faded_paint': _('Faded Door Paint'),
            'rusty_paint': _('Rusty Paint'),
        }

        checked_details = [label for field, label in detalle_fields.items() if getattr(ticket_id, field, False)]

        if checked_details:
            note_lines.append(f"{_('Details')}:")
            note_lines.extend(checked_details)

        note_text = "<br />".join(note_lines)
        picking_id.write({'note': note_text})

    def action_validate_transfer(self):
        ticket_id = self.ticket_id
        service_warehouse_id = self.service_warehouse_id.id
        if not service_warehouse_id:
            raise UserError(_("The service warehouse is needed to generate the transfer."))

        picking_type = self.env['stock.picking.type'].search([
            ('warehouse_id', '=', service_warehouse_id),
            ('code', '=', 'outgoing')
        ], limit=1)

        product_code = 'SERVPINTURA' if ticket_id.ticket_type_id.name.lower() == 'pintura' else 'SERVFUNC'
        product_id = self.env['product.product'].search([('default_code', '=', product_code)], limit=1)

        picking_vals = {
            'picking_type_id': picking_type.id,
            'user_id': self.user_id.id,
            'partner_id': self.partner_id.id,
            'x_lot': self.lot,
            'x_block': self.block,
            'x_subdivision': self.subdivision,
            'origin': ticket_id.name,
            'service_ticket_id': ticket_id.id,
            'shipping_assignment': self.shipping_assignment
            #'is_from_helpdesk': True,
        }

        picking = self.env['stock.picking'].create(picking_vals)
        self.add_notes_to_transfer(ticket_id, picking)

        if product_id:
            move_vals = {
                'product_id': product_id.id,
                'name': product_id.name,
                'product_uom_qty': 1,
                'product_uom': product_id.uom_id.id,
                'location_id': picking.location_id.id,
                'location_dest_id': picking.location_dest_id.id,
                'picking_id': picking.id,
            }
            self.env['stock.move'].create(move_vals)

        picking.action_assign()

        ticket_id.write({'is_locked': True})

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }
