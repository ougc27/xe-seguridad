from odoo import models, fields, api, _
from odoo.exceptions import UserError


class HelpdeskTicket(models.Model):
    _inherit = 'helpdesk.ticket'

    is_type_paint_or_function = fields.Boolean(compute='_compute_is_type_paint_or_function')

    accessory_delivery_pending = fields.Boolean(
        string='Accessory or spare part pending delivery',
        copy=False
    )

    accessory_install_pending = fields.Boolean(
        string='Accessory or spare part pending installation',
        copy=False
    )

    block = fields.Char(copy=False)

    architect = fields.Char()

    missing_caps = fields.Boolean(copy=False)

    complete_address = fields.Char()

    damaged_interior_profile = fields.Boolean(copy=False)
    
    deficient_masonry = fields.Boolean(string='Poor masonry work', copy=False)

    door_dent = fields.Boolean(string='Dent or bump on the door', copy=False)

    door_installation = fields.Boolean(copy=False)

    door_reinstallation = fields.Boolean(copy=False)

    door_replacement = fields.Boolean(copy=False)

    paint_factory_defect = fields.Boolean(string='Paint with a factory defect', copy=False)

    door_faded_paint = fields.Boolean(string='Paint changed tone or door is faded', copy=False)

    housing_type = fields.Selection([
        ('in_construction', 'In construction'),
        ('for_delivery', 'For delivery'),
        ('final_client', 'Final client'),
        ('post_sale', 'Post Sale')
    ], copy=False)

    remission_incident = fields.Boolean(string='Incident in remission', copy=False)

    customer_locked = fields.Boolean(string='Locked customer', copy=False)

    loose_door_frame = fields.Boolean(string='Loose frame', copy=False)

    loose_broken_gaskets = fields.Boolean(string='Loose or broken gaskets', copy=False)

    lot = fields.Char(copy=False)

    mechanism_review = fields.Boolean(copy=False)

    crm_team_id = fields.Many2one(
        'crm.team', 
        related='partner_id.team_id',
        store=True,
        readonly=True,
        copy=False
    )

    phone_number = fields.Char(copy=False)

    picking_id = fields.Many2one(
        'stock.picking',
        copy=False,
    )

    product_id = fields.Many2one(
        'product.product',
    )

    remission_date = fields.Datetime(
        related='picking_id.remission_date',
        store=True,
        readonly=True,
        help="Date at which the transfer has been processed or cancelled."
    )

    rusty_paint = fields.Boolean(copy=False)

    service_warehouse_id = fields.Many2one(
        'stock.warehouse',
        help="Service warehouse assigned to the registered ticket"
    )

    smart_lock_inspection = fields.Boolean(copy=False)

    smart_lock_replacement = fields.Boolean(copy=False)

    spare_part_replacement = fields.Boolean(copy=False)

    subdivision = fields.Char(copy=False)

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        related='sale_id.warehouse_id',
        readonly=True,
        copy=False,
        store=True,
    )

    sale_id = fields.Many2one(
        'sale.order',
        string="Sale Order",
        copy=False,
        tracking=True
    )

    pos_order_id = fields.Many2one(
        'pos.order',
        string="Pos Order",
        copy=False,
        tracking=True
    )

    call_ids = fields.One2many('helpdesk.call', 'ticket_id', string='Calls')

    service_picking_ids = fields.One2many(
        'stock.picking',
        'service_ticket_id',
        copy=False
    )

    show_transfer_button = fields.Boolean(compute="_compute_show_transfer_button")

    is_locked = fields.Boolean(help='When the ticket have an active service transfer the value is setted to true')

    pos_store_id = fields.Many2one(
        'res.partner',
        related='sale_id.pos_store',
        readonly=True
    )

    attribution_ids = fields.Many2many(
        'helpdesk.attribution',
        string='Attributable To'
    )
    
    name = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        tracking=True,
        default='New'
    )

    supervisor_id = fields.Many2one(
        'supervisor.installer',
        'Supervisor',
        related='picking_id.supervisor_id',
        readonly=True,
        help="Supervisor of the picking."
    )

    installer_id = fields.Many2one(
        'supervisor.installer',
        'Installer',
        related='picking_id.installer_id',
        readonly=True,
        help="Installer of the picking."
    )

    scheduled_date = fields.Datetime(
        readonly=True,
        help="Scheduled date of the picking."
    )
    
    ticket_type_id_domain = fields.Binary(compute="_compute_ticket_type_id_domain")

    @api.depends('team_id')
    def _compute_ticket_type_id_domain(self):
        for rec in self:
            if rec.team_id and rec.team_id.ticket_type_ids:
                rec.ticket_type_id_domain = [('id', 'in', self.team_id.ticket_type_ids.ids)]
            else:
                rec.ticket_type_id_domain = []

    @api.depends('ticket_type_id')
    def _compute_is_type_paint_or_function(self):
        for rec in self:
            rec.is_type_paint_or_function = rec.ticket_type_id.name and rec.ticket_type_id.name.lower() in ['pintura', 'funcionamiento']

    @api.depends('service_picking_ids.state')
    def _compute_show_transfer_button(self):
        for ticket in self:
            pickings = ticket.service_picking_ids
            if not pickings:
                ticket.show_transfer_button = True
            else:
                is_all_pickings_cancelled = all(p.state == 'cancel' for p in pickings)
                ticket.show_transfer_button = is_all_pickings_cancelled
                ticket.is_locked = not is_all_pickings_cancelled

    def action_open_transfer_wizard(self):
        self.ensure_one()
        if not self.complete_address or not self.phone_number or not self.product_id:
            raise UserError(_("The following fields are required: Client Address, Contact Phone, and Product."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Generate Transfer'),
            'res_model': 'ticket.transfer.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_ticket_id': self.id,
                'default_user_id': self.user_id.id,
                'default_service_warehouse_id': self.service_warehouse_id.id,
                'default_partner_id': self.partner_id.id,
                'default_crm_team_id': self.crm_team_id.id,
                'default_lot': self.lot,
                'default_block': self.block,
                'default_subdivision': self.subdivision,
                'product_id': self.product_id
            },
        }

    def action_open_service_picking_ids(self):
        self.ensure_one()
        service_picking_ids = self.service_picking_ids

        if len(service_picking_ids) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'stock.picking',
                'view_mode': 'form',
                'res_id': service_picking_ids[0].id,
                'target': 'current',
            }
        else:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Related Pickings'),
                'res_model': 'stock.picking',
                'view_mode': 'tree,form',
                'domain': [('id', 'in', service_picking_ids.ids)],
                'target': 'current',
            }

    def is_door_product(self, move):
        return (
            'Puertas / Puertas' in move.product_id.categ_id.complete_name and
            move.product_id.type == 'product'
        )

    def autofill_from_picking(self, picking_id):
        sale_id = picking_id.sale_id
        pos_order_id = picking_id.pos_order_id
        partner_id = picking_id.partner_id
        product_id = next(
            (m.product_id for m in picking_id.move_ids 
            if 'Puertas / Puertas' in m.product_id.categ_id.complete_name 
            and m.product_id.type == 'product'),
            picking_id.move_ids[:1] and picking_id.move_ids[0].product_id
        )  

        self.write({
            'picking_id': picking_id,
            'partner_id': partner_id.id,
            'complete_address': partner_id.contact_address_complete,
            'sale_id': sale_id.id,
            'pos_order_id': pos_order_id.id,
            'phone_number': partner_id.mobile or partner_id.phone,
            'product_id': product_id,
            'service_warehouse_id': sale_id.warehouse_id.id or pos_order_id.picking_type_id.warehouse_id.id,
            'lot': picking_id.x_lot,
            'block': picking_id.x_block,
            'subdivision': picking_id.x_subdivision,
        })        

    def autofill_from_sale(self):
        sale_id = self.sale_id
        picking_ids = sale_id.picking_ids
        picking_id = False
        pickings = picking_ids.filtered(lambda p: p.state != 'cancel')
        if pickings:
            pickings = sorted(pickings, key=lambda p: p.state != 'done')
            for picking in pickings:
                if any(self.is_door_product(m) for m in picking.move_ids):
                    picking_id = picking
                    break
            picking_id = picking_id or pickings[0]
            self.autofill_from_picking(picking_id)
        else:
            partner_id = sale_id.partner_id
            self.write({
                'partner_id': partner_id.id,
                'complete_address': partner_id.contact_address_complete,
                'phone_number': partner_id.mobile or partner_id.phone,
                'service_warehouse_id': sale_id.warehouse_id.id,
            })

    def autofill_from_pos_order(self):
        pos_order = self.pos_order_id
        picking_ids = pos_order.picking_ids
        picking_id = False
        pickings = picking_ids.filtered(lambda p: p.state != 'cancel')
        if pickings:
            pickings = sorted(pickings, key=lambda p: p.state != 'done')
            for picking in pickings:
                if any(self.is_door_product(m) for m in picking.move_ids):
                    picking_id = picking
                    break
            picking_id = picking_id or pickings[0]
            self.autofill_from_picking(picking_id)
        else:
            partner_id = pos_order.partner_id
            self.write({
                'partner_id': partner_id.id,
                'complete_address': partner_id.contact_address_complete,
                'phone_number': partner_id.mobile or partner_id.phone,
                'service_warehouse_id': pos_order.picking_type_id.warehouse_id.id,
            })

    def autofill_from_picking_or_sale(self):
        if self.service_warehouse_id or self.phone_number or self.partner_id or self.product_id or self.complete_address:
            raise UserError(_("You need to erase the following fields: Service Warehouse, Phone Number, Product, Partner, Complete Address"))
        picking_id = self.picking_id
        if picking_id:
            self.autofill_from_picking(picking_id)
        elif self.sale_id:
            self.autofill_from_sale()
        elif self.pos_order_id:
            self.autofill_from_pos_order()
        else:
            raise UserError(_("Sales order or transfer fields are needed to get the information."))

    @api.model
    def create(self, vals):
        if vals.get('name', 'New') == 'New':
            company = self.env.company
            vals['name'] = self.env['ir.sequence'].with_company(company).next_by_code('helpdesk.ticket.name')
        return super().create(vals)

    @api.depends('ticket_ref', 'partner_name')
    @api.depends_context('with_partner')
    def _compute_display_name(self):
        display_partner_name = self._context.get('with_partner', False)
        ticket_with_name = self.filtered('name')
        for ticket in ticket_with_name:
            ticket.display_name = ticket.name
        return super(HelpdeskTicket, self - ticket_with_name)._compute_display_name()
