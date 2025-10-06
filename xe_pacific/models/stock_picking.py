from collections import defaultdict
from odoo import api, models, fields, _
from odoo.osv import expression
from odoo.exceptions import UserError
import math


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shipping_assignment = fields.Selection([
        ('logistics', 'Logística'),
        ('shipments', 'Embarques')], store=True, compute="_compute_shipping_assignment")

    kanban_task_status = fields.Selection([
        ('to_scheduled', 'POR PROGRAMAR'),
        ('scheduled', 'PROGRAMADO'),
        ('confirmed', 'CONFIRMAD0'),
        ('shipped', 'EMBARCADO / RUTA'),
        ('waiting', 'PDTE MODIFICACIÓN'),
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_states')

    shipment_task_status = fields.Selection([
        ('to_scheduled', 'POR PROGRAMAR'),
        ('scheduled', 'PROGRAMADO'),
        ('warehouse', 'ALMACÉN'),
        ('quality', 'CALIDAD'),
        ('production', 'PRODUCCIÓN'),
        ('shipments', 'EMBARQUES'),
        ('confirmed', 'EMBARCADO'),
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_status')

    x_studio_canal_de_distribucin = fields.Char(
        string="Nombre del Equipo de Ventas",
        compute="_compute_x_studio_canal_de_distribucin",
        store=True,
        readonly=True
    )

    is_remission_separated = fields.Boolean()

    x_studio_folio_rem = fields.Char(string='Remisión', copy=False, tracking=True)

    end_date = fields.Datetime(
        index=True, tracking=True)

    partner_id = fields.Many2one(
        'res.partner', 'Contact',
        check_company=True, index='btree_not_null', tracking=True)

    is_loose_construction = fields.Boolean()

    tag_ids = fields.Many2many(
        'inventory.tag', 'inventory_tag_rel', 'picking_id', 'tag_id', string='Tags', tracking=True)
    
    invoice_ids = fields.Many2many(
        'account.move',
        'account_move_picking_rel',
        'picking_id',
        'move_id',
        string='Invoices',
        help='Match the invoices with the shipment.',
        readonly=True,
        copy=False
    )

    sale_note = fields.Html(
        string="Terms and Conditions",
        related='sale_id.description',
        readonly=True
    )

    x_task = fields.Many2one(
        'project.task', 'Related task', copy=False)

    x_is_loose = fields.Boolean(string="Liberado CF", copy=False)

    contact_address_complete = fields.Char(
        string="Complete Address",
        compute="_compute_contact_address_complete",
        store=True
    )

    door_count = fields.Integer(
        compute="_compute_door_count",
        store=True
    )

    group_id = fields.Many2one(
        'procurement.group', 'Procurement Group',
        readonly=True, related='move_ids.group_id', store=True, tracking=True)

    initial_date = fields.Datetime(
        copy=False,
        readonly=True,
        help="This field is used to measure productivity.")

    helpdesk_ticket_ids = fields.One2many('helpdesk.ticket', 'picking_id', string="Helpdesk Tickets")

    ticket_count = fields.Integer(compute='_compute_helpdesk_ticket_ids')

    x_lot = fields.Char(copy=False)

    x_block = fields.Char(copy=False)

    x_subdivision = fields.Char(copy=False)

    service_ticket_id = fields.Many2one('helpdesk.ticket', copy=False)

    is_full_returned = fields.Boolean(compute='_compute_is_full_returned', copy=False, store=True)

    @api.depends('return_ids.state')
    def _compute_is_full_returned(self):
        for record in self:
            done_returns = record.return_ids.filtered(lambda r: r.state == 'done')

            if not done_returns:
                record.is_full_returned = False
                continue

            original_quantities = defaultdict(float)
            for move in record.move_line_ids:
                original_quantities[move.product_id] += move.quantity

            returned_quantities = defaultdict(float)
            for ret in done_returns:
                for move in ret.move_line_ids:
                    returned_quantities[move.product_id] += move.quantity
            
            all_returned = all(
                returned_quantities.get(product, 0.0) == qty
                for product, qty in original_quantities.items()
            )

            record.is_full_returned = all_returned

    @api.depends('group_id', 'helpdesk_ticket_ids')
    def _compute_x_studio_canal_de_distribucin(self):
        for record in self:
            group_id = record.group_id
            helpdesk_ticket_ids = record.helpdesk_ticket_ids
            if group_id and group_id.sale_id:
                record.x_studio_canal_de_distribucin = group_id.sale_id.partner_id.team_id.name
            elif helpdesk_ticket_ids:
                record.x_studio_canal_de_distribucin = helpdesk_ticket_ids[0].crm_team_id.name
            else:
                record.x_studio_canal_de_distribucin = False

    @api.depends('helpdesk_ticket_ids', 'service_ticket_id')
    def _compute_helpdesk_ticket_ids(self):
        for ticket in self:
            ticket.ticket_count = len(ticket.helpdesk_ticket_ids) + len(ticket.service_ticket_id)

    @api.depends('partner_id')
    def _compute_contact_address_complete(self):
        for record in self:
            record.contact_address_complete = record.partner_id.contact_address_complete if record.partner_id else ""

    @api.depends('move_ids_without_package', 'move_ids_without_package.product_uom_qty')
    def _compute_door_count(self):
        for record in self:
            door_count = 0
            for line in record.move_ids_without_package:
                if 'Puertas / Puertas' in line.product_id.categ_id.complete_name:
                    door_count += line.product_uom_qty
            record.door_count = door_count

    @api.depends('location_id', 'move_ids', 'group_id')
    def _compute_shipping_assignment(self):
        for rec in self:
            if rec.service_ticket_id:
                shipping_assignment = rec.shipping_assignment
                rec.shipping_assignment = shipping_assignment
            elif rec.company_id.id == 4:
                sale_id = rec.group_id.sale_id
                distribution_channel = sale_id.partner_id.team_id.name
                shipment_channels = ['DISTRIBUIDORES', 'MARKETPLACE', 'SODIMAC HC', 'INTERGRUPO']
                internal = rec.picking_type_code == 'internal'
                if distribution_channel in shipment_channels:
                    rec.x_is_loose = True
                if internal:
                    rec.x_is_loose = True
                    rec.shipping_assignment = 'shipments'
                    continue
                if rec.location_id and rec.location_id.warehouse_id.name:
                    if any(keyword in rec.location_id.warehouse_id.name for keyword in ['Amazon', 'MercadoLibre']):
                        rec.shipping_assignment = 'shipments'
                        continue
                    if 'monterrey pr1' in rec.location_id.warehouse_id.name.lower():
                        if distribution_channel in ['DISTRIBUIDORES', 'MARKETPLACE', 'GOTT']:
                            rec.shipping_assignment = 'shipments'
                            continue
                        if sale_id.order_line.filtered(lambda record: record.product_id.default_code == 'FLTENVIO'):
                            rec.shipping_assignment = 'shipments'
                            continue
                        if rec.picking_type_code == 'internal':
                            rec.shipping_assignment = 'shipments'
                            continue
                rec.shipping_assignment= 'logistics'
                continue
            else:
                rec.shipping_assignment = 'shipments'

    def _group_expand_states(self, states, domain, order):
        return [key for key, val in self._fields['kanban_task_status'].selection]

    def _group_expand_status(self, states, domain, order):
        return [key for key, val in self._fields['shipment_task_status'].selection]

    def _create_separated_picking_by_categ(self, rec, moves, sale_order, scheduled_date):
        if rec.state == 'done':
            return
        new_picking = rec.copy({
            'is_remission_separated': True,
            'scheduled_date': scheduled_date,
            'state': 'confirmed',
        })
        moves.write({'picking_id': new_picking.id})
        rec.create_notes_in_pickings(sale_order, new_picking)
        for move in new_picking.move_ids:
            if move not in moves:
                move.sudo().unlink()
        new_picking.batch_id.sudo().unlink()
        return new_picking

    def create_notes_in_pickings(self, sale_order, new_picking):
        html_link = self.with_context(
            model_name="sale.order",
            record_id=sale_order.id
        )._get_html_link(
            title=sale_order.name
        )

        body = _("Este trasladar se creó a partir de: %s", html_link)

        new_picking.message_post(body=body,
            author_id=self.env.ref('base.partner_root').id,
            subtype_xmlid='mail.mt_note')
    
    def single_product_separation(self, move_ids, sale_order, scheduled_date, construction=False):
        combined_moves = {}
        first_picking = None
        first_move =self.move_ids[0]
        for move in move_ids:
            product_key = (move.product_id.id, move.product_uom.id, move.bom_line_id.id if move.bom_line_id else None)
            if product_key in combined_moves:
                combined_moves[product_key].write({'product_uom_qty': combined_moves[product_key].product_uom_qty + move.product_uom_qty})
                move.sudo().unlink()
            else:
                combined_moves[product_key] = move
        insfor = move_ids.filtered(lambda m: m.product_id.default_code == 'INSFOR')
        for move in move_ids:
            if insfor and move.product_id.default_code == 'INSFOR':
                if len(move_ids) == 1 or move.id == move_ids[0].id:
                    continue
                if move.id != move_ids[0].id:
                    new_picking = self.copy(
                        {
                            'is_remission_separated': True,
                            'scheduled_date': scheduled_date,
                        }
                    )
                    for new_move in new_picking.move_ids:
                        if new_move.product_id.id == move.product_id.id:
                            new_move.write({'state': 'confirmed'})
                            continue
                        new_move.sudo().unlink()
                    new_picking.write({'state': 'confirmed'})
                    new_picking.batch_id.sudo().unlink()
                    self.create_notes_in_pickings(sale_order, new_picking)
                    continue
            quantity = move.product_uom_qty
            product_uom_qty = 1
            bom_line_id = move.bom_line_id
            if bom_line_id:
                product_uom_qty = bom_line_id.product_qty
                quantity = quantity / product_uom_qty
            if not bom_line_id and move.id == move_ids[0].id and not construction:
                quantity -= product_uom_qty
            if quantity >= 1:
                move.write({'product_uom_qty': product_uom_qty})
            quantity = int(quantity) if quantity.is_integer() else math.floor(quantity)
            for i in range(quantity):
                new_picking = self.copy(
                    {
                        'is_remission_separated': True,
                        'scheduled_date': scheduled_date,
                    }
                )
                if first_picking is None:
                    first_picking = new_picking
                for new_move in new_picking.move_ids:
                    if (new_move.product_id.id, new_move.bom_line_id.id) == (move.product_id.id, move.bom_line_id.id):
                        new_move.write({'product_uom_qty': product_uom_qty, 'state': 'confirmed'})
                    else:
                        new_move.sudo().unlink()

                new_picking.write({'state': 'confirmed'})
                new_picking.batch_id.sudo().unlink()
                self.create_notes_in_pickings(sale_order, new_picking)
        if first_picking and first_move.bom_line_id:
            first_picking.sudo().unlink()

    def combine_lines(self):
        product_map = {}
        for move in self.move_ids:
            product_id = move.product_id.id
            if product_id in product_map:
                product_map[product_id].append(move)
            else:
                product_map[product_id] = [move]
        
        for product_id, moves in product_map.items():
            if len(moves) > 1:
                total_qty = sum(m.product_uom_qty for m in moves)
        
                main_move = moves[0]
                main_move.write({'product_uom_qty': total_qty})
        
                for move in moves[1:]:
                    move.unlink()

    def separate_remissions(self):
        if self.filtered(lambda p: p.state in ['transit', 'done']):
            raise UserError(_("Remissions in transit or done states cannot be separated."))
        for rec in self:
            rec.combine_lines()
            first_move_id = rec.move_ids[0].id
            sale_order = rec.env['sale.order'].search(
                [('name', '=', rec.group_id.name), ('company_id', '=', rec.env.company.id)])
            scheduled_date = rec.scheduled_date
            rec.write({'is_remission_separated': True})
            rec.single_product_separation(rec.move_ids, sale_order, scheduled_date)
            for move in rec.move_ids:
                if move.id != first_move_id:
                    move.sudo().unlink()
            rec.batch_id.sudo().unlink()

    def separate_construction_remissions(self):
        for rec in self:
            if rec.state == 'done' or rec.state == 'cancel':
                continue
            sale_order = rec.env['sale.order'].search(
                [('name', '=', rec.group_id.name), ('company_id', '=', rec.env.company.id)]
            )
            scheduled_date = rec.scheduled_date
            rec.batch_id.sudo().unlink()

            if len(rec.move_ids) == 1:
                rec.single_product_separation(rec.move_ids, sale_order, scheduled_date)
                continue

            combined_moves = {}
            for move in rec.move_ids:
                product_key = (move.product_id.id, move.product_uom.id, move.bom_line_id.id if move.bom_line_id else move.id)
                if product_key in combined_moves:
                    combined_moves[product_key].write({'product_uom_qty': combined_moves[product_key].product_uom_qty + move.product_uom_qty})
                    move.sudo().unlink()
                else:
                    combined_moves[product_key] = move

            door_moves = rec.move_ids.filtered(
                lambda m: 'Puertas / Puertas' in m.product_id.categ_id.complete_name and 
                          m.product_id.type == 'product'
            )
            lock_moves = rec.move_ids.filtered(
                lambda m: 'cerraduras' in m.product_id.categ_id.complete_name.lower() and 
                          m.product_id.type == 'product'
            )
            door_accessory_moves = rec.move_ids.filtered(
                lambda m: 'accesorios' in m.product_id.categ_id.complete_name.lower() and
                          m.product_id.type == 'product'
            )
            door_installation_moves = rec.move_ids.filtered(
                lambda m: m.product_id.default_code in ('INSBAS', 'INS10')
            )
            lock_installation_moves = rec.move_ids.filtered(
                lambda m: m.product_id.default_code in ('INSBASCDI', 'INSCDI')
            )

            while sum(m.product_uom_qty for m in door_moves) > 0 or sum(m.product_uom_qty for m in lock_moves) > 0 or sum(m.product_uom_qty for m in door_accessory_moves) > 0:
                grouped_moves = []

                for move in door_moves.filtered(lambda m: m.product_uom_qty > 0):
                    grouped_moves.append((move, 1))
                    move.write({'product_uom_qty': move.product_uom_qty - 1})
  
                    if door_installation_moves:
                        installation_move = door_installation_moves.filtered(lambda m: m.product_uom_qty > 0)
                        if installation_move:
                            grouped_moves.append((installation_move[0], 1))
                            installation_move[0].write({'product_uom_qty': installation_move[0].product_uom_qty - 1})
                    break

                for move in door_accessory_moves.filtered(lambda m: m.product_uom_qty > 0):
                    bom_line_id = move.bom_line_id
                    product_uom_qty = bom_line_id.product_qty if bom_line_id else 1
                    grouped_moves.append((move, product_uom_qty))
                    move.write({'product_uom_qty': move.product_uom_qty - product_uom_qty})
                
                for move in lock_moves.filtered(lambda m: m.product_uom_qty > 0):
                    grouped_moves.append((move, 1))
                    move.write({'product_uom_qty': move.product_uom_qty - 1})
             
                    if lock_installation_moves:
                        installation_move = lock_installation_moves.filtered(lambda m: m.product_uom_qty > 0)
                        if installation_move:
                            grouped_moves.append((installation_move[0], 1))
                            installation_move[0].write({'product_uom_qty': installation_move[0].product_uom_qty - 1})
                    break

                new_picking = rec.copy({
                    'is_remission_separated': True,
                    'scheduled_date': scheduled_date,
                    'state': 'confirmed',
                })

                for new_move in new_picking.move_ids:
                    for original_move, qty in grouped_moves:
                        if (new_move.product_id.id == original_move.product_id.id and 
                            new_move.bom_line_id.id == original_move.bom_line_id.id):
                            new_move.write({'product_uom_qty': qty, 'state': 'confirmed'})
                            break
                    else:
                        new_move.sudo().unlink()

                rec.create_notes_in_pickings(sale_order, new_picking)
 
            remaining_moves = rec.move_ids - (door_moves + lock_moves + door_accessory_moves + door_installation_moves + lock_installation_moves)

            if remaining_moves:
                grouped_moves = [(move, move.product_uom_qty) for move in remaining_moves]

                new_picking = rec.copy({
                    'is_remission_separated': True,
                    'scheduled_date': scheduled_date,
                    'state': 'confirmed',
                })

                for new_move in new_picking.move_ids:
                    for original_move, qty in grouped_moves:
                        if (new_move.product_id.id == original_move.product_id.id and
                            new_move.bom_line_id.id == original_move.bom_line_id.id):
                            new_move.write({'product_uom_qty': qty, 'state': 'confirmed'})
                            break
                    else:
                        if qty > 0:
                            rec.single_product_separation(new_move, sale_order, scheduled_date, True)
                        new_move.sudo().unlink()
                
                rec.create_notes_in_pickings(sale_order, new_picking)
            
                if not door_moves and not lock_moves:
                    if door_installation_moves:
                        rec.single_product_separation(door_installation_moves, sale_order, scheduled_date, True)
                    if lock_installation_moves:
                        rec.single_product_separation(lock_installation_moves, sale_order, scheduled_date, True)
                rec.sudo().unlink()
                continue
            elif not rec.move_ids.filtered(lambda m: m.product_uom_qty > 0):
                rec.sudo().unlink()
                continue
            
            for line in rec.move_ids.filtered(lambda m: m.product_uom_qty <= 0):
                line.sudo().unlink()

    def separate_client_remissions(self):
        for rec in self:
            sale_order = rec.env['sale.order'].search(
                [('name', '=', rec.group_id.name), ('company_id', '=', rec.env.company.id)]
            )
            storable_moves = rec.move_ids.filtered(
                lambda m: m.product_id.type == 'product'
            )
            service_moves = rec.move_ids.filtered(
                lambda m: m.product_id.type == 'consu' and 
                          'visita' in m.product_id.name.lower()
            )
            installation_moves = rec.move_ids.filtered(
                lambda m: m.product_id.type == 'consu' and 
                'instalación' in m.product_id.name.lower() or
                'instalacion' in m.product_id.name.lower()    
            )
            rest_moves = rec.move_ids.filtered(
                lambda m: m.product_id.type == 'consu' and 
                    'visita' not in m.product_id.name.lower() and
                    'instalaci' not in m.product_id.name.lower()
            )
            scheduled_date = rec.scheduled_date
    
            if service_moves:
                rec._create_separated_picking_by_categ(
                    rec, service_moves, sale_order, scheduled_date)
    
            if installation_moves:
                rec._create_separated_picking_by_categ(
                    rec, installation_moves, sale_order, scheduled_date)

            if rest_moves:
                rec._create_separated_picking_by_categ(
                    rec, rest_moves, sale_order, scheduled_date)

            rec.write({'is_remission_separated': True})
            rec.batch_id.sudo().unlink()

            if not rec.move_ids.filtered(lambda m: m.product_uom_qty > 0):
                rec.sudo().unlink()
                
    def combine_remissions(self):
        if not self:
            return

        primary_picking = self[0]
        first_picking = self.filtered(lambda p: p.create_uid == self.env.ref('base.user_root'))
        if first_picking:
            primary_picking = first_picking[0]

        picking_state = any(
            move.state not in ('waiting', 'confirmed', 'assigned')
            for move in self
        )
        if picking_state:
            raise UserError("Una de las entregas no esta en estado de espera o listo")

        group_id = self.mapped('group_id')
        partner_id = self.mapped('partner_id')
        location_id = self.mapped('location_id')

        if len(group_id) > 1 or len(partner_id) > 1 or len(location_id) > 1:
            raise UserError("El contacto, ubicación origen o la venta no son iguales en las entregas")

        for picking in self - primary_picking:
            for move in picking.move_ids:
                existing_move = primary_picking.move_ids.filtered(
                    lambda m: m.product_id == move.product_id and 
                            m.product_uom == move.product_uom and 
                            m.bom_line_id == move.bom_line_id
                )
                if existing_move:
                    existing_move = existing_move[0]
                    existing_move.product_uom_qty += move.product_uom_qty
                else:
                    move.write({'picking_id': primary_picking.id})

            picking.action_cancel()
            picking.sudo().unlink()

        primary_picking.write({
            'is_remission_separated': False,
            'state': 'confirmed'
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': primary_picking.id,
        }

    @api.depends('name', 'x_studio_folio_rem')
    @api.depends_context('from_helpdesk_ticket')
    def _compute_display_name(self):
        if not self._context.get('from_helpdesk_ticket'):
            return super()._compute_display_name()
        for rec in self:
            name = rec.name
            if rec.x_studio_folio_rem:
                name = f'{name} - {rec.x_studio_folio_rem}'
            rec.display_name = name

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        if self.env.context.get('from_helpdesk_ticket'):
            query = """
                SELECT id
                    FROM stock_picking
                    WHERE
                    (
                        name ilike %s
                    OR
                        x_studio_folio_rem ilike %s
                    )
                    AND
                        company_id = %s
                    AND
	                    TRIM(name) != ''
                    AND 
                        TRIM(x_studio_folio_rem) != ''
                    LIMIT %s
            """
            self.env.cr.execute(
                query, (
                    '%' + name + '%',
                    '%' + name + '%',
                    self.env.company.id,
                    limit
                )
            )
            return [row[0] for row in self.env.cr.fetchall()]
        return super()._name_search(name, domain, operator, limit, order)

    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        if self.filtered(lambda picking: picking.state in ['transit', 'cancel']):
            return

        self.mapped('package_level_ids').filtered(lambda pl: pl.state == 'draft' and not pl.move_ids)._generate_moves()
        self.filtered(lambda picking: picking.state == 'draft').action_confirm()
        moves = self.move_ids.filtered(
            lambda move: (
                move.state not in ('draft', 'cancel', 'done')
                and not move.product_id.product_tmpl_id.not_automatic_lot_number
            )
        ).sorted(
            key=lambda move: (
                -int(move.priority),
                not bool(move.date_deadline),
                move.date_deadline,
                move.date,
                move.id
            )
        )
        if not moves:
            raise UserError(_('Nothing to check the availability for.'))
        moves._action_assign()
        return True

    def write(self, vals):
        res = super().write(vals)
        for picking in self:
            service_ticket_id = picking.service_ticket_id
            if service_ticket_id:
                if picking.state == 'done' and (
                    picking.shipment_task_status == 'finished' or picking.kanban_task_status == 'finished'
                ):
                    finished_stage = self.env['helpdesk.stage'].search([
                        ('team_ids', 'in', [service_ticket_id.team_id.id]),
                        ('name', 'ilike', 'Finalizado')
                    ], limit=1)
                    if finished_stage:
                        service_ticket_id.write({'stage_id': finished_stage.id, 'kanban_state': 'done'})
                if vals.get('scheduled_date') and picking.scheduled_date != picking.create_date:
                    service_ticket_id.write({'scheduled_date': picking.scheduled_date})
                    programed_stage = self.env['helpdesk.stage'].search([
                        ('team_ids', 'in', [service_ticket_id.team_id.id]),
                        ('name', 'ilike', 'Programado')
                    ], limit=1)
                    if programed_stage:
                        service_ticket_id.write({'stage_id': programed_stage.id})
            if picking.x_studio_folio_rem and picking.state not in ['transit', 'done']:
                picking.write({'state': 'transit'})
            if picking.shipping_assignment == 'shipments':
                picking.x_task.sudo().unlink()
        return res

    def _create_backorder(self):
        """ This method is called when the user chose to create a backorder. It will create a new
        picking, the backorder, and move the stock.moves that are not `done` or `cancel` into it.
        """
        res = super()._create_backorder()
        for backorder in res:
            backorder_id = backorder.backorder_id
            group_id = backorder_id.group_id
            if not backorder.group_id and group_id:
                for move in backorder.move_ids:
                    if not move.group_id:
                        move.write({'group_id': group_id})
                backorder.write({'group_id': group_id})
            if backorder.picking_type_code == 'outgoing':
                backorder.write({'initial_date': backorder.sale_id.date_order})
            if backorder.picking_type_code == 'internal':
                backorder.write({'initial_date': backorder_id.initial_date})
        return res

    def set_initial_date(self):
        if not self.initial_date:
            date_order = self.env['sale.order'].search([
                ('name', '=', self.origin),
                ('company_id', '=', self.env.company.id)
            ], limit=1).date_order
            if date_order:
                self.write({'initial_date': date_order})

    def action_confirm(self):
        res = super().action_confirm()
        for rec in self: 
            if rec.picking_type_code == 'internal' and not rec.initial_date:
                rec.write({'initial_date': fields.Datetime.now()})
        return res

    @api.model
    def create(self, vals):
        res = super().create(vals)
        for picking in res:
            if picking.picking_type_code == 'outgoing':
                picking.set_initial_date()
        return res

    def update_tag_ids_to_pickings(self, from_write=False):
        final_client = False
        pickings = self.env['sale.order'].search([
            ('name', '=', self.origin),
            ('company_id', '=', self.env.company.id)
        ]).picking_ids
        if pickings:
            picking = pickings[0]
            if picking.shipping_assignment == 'shipments' or picking.x_studio_canal_de_distribucin == 'CONSTRUCTORAS':
                return False
        if from_write:
            """origin = self.origin
            if self.return_id:
                origin = self.return_id.origin
            sale_lines = self.env['sale.order'].search(
                [('name', '=', origin), ('company_id', '=', self.env.company.id)]
            ).order_line"""
            sale_lines = False
            if self.sale_id:
                sale_lines = self.sale_id.order_line
            if sale_lines:
                if (
                    self.move_ids.filtered(
                        lambda m: m.product_id.default_code in ('VISTEC', 'VISTEC_COMPRADA')
                    )
                    and sale_lines.filtered(lambda m: m.product_id.type == 'product')
                ):
                    final_client = 'delivery'
                if (
                    self.move_ids.filtered(lambda m: m.product_id.type == 'product')
                    and sale_lines.filtered(
                        lambda m: m.product_id.type == 'consu' and
                        'instalación' in m.product_id.name.lower() or
                        'instalacion' in m.product_id.name.lower()    
                    )
                ):
                    final_client = 'instalation'
                if (
                    self.move_ids.filtered(
                        lambda m: m.product_id.default_code in ('VISREF', 'MTTOPUERTAS')
                    )
                ):
                    final_client = 'instalation'
        else:
            if (
                self.move_ids.filtered(
                    lambda m: m.product_id.default_code in ('VISREF', 'MTTOPUERTAS')
                )
            ):
                final_client = 'instalation'
            elif (
                self.move_ids.filtered(
                    lambda m: m.product_id.default_code in ('VISTEC', 'VISTEC_COMPRADA'))
            ):
                final_client = 'technical_visit'
            elif (
                self.move_ids.filtered(lambda m: m.product_id.type == 'product')
            ):
                final_client = 'delivery'
            elif (
                self.move_ids.filtered(
                    lambda m: m.product_id.type == 'consu' and
                    'instalación' in m.product_id.name.lower() or
                    'instalacion' in m.product_id.name.lower()    
                )
            ):
                final_client = 'instalation'
        for picking in pickings:
            picking.add_tag_ids_to_pickings(final_client)

    def add_tag_ids_to_pickings(self, final_client):
        tag_name_map = {
            'technical_visit': 'Visita técnica',
            'delivery': 'Entrega',
            'instalation': 'Instalación',               
        }
        tag_name = tag_name_map.get(final_client)
        
        if tag_name:
            tag_to_add = self.env['inventory.tag'].search([('name', '=', tag_name)], limit=1)
    
            other_tags = self.env['inventory.tag'].search([
                ('name', 'in', [v for k, v in tag_name_map.items() if k != final_client])
            ])
            
            self.write({
                'tag_ids': [
                    (3, tag.id) for tag in other_tags
                ] + [(4, tag_to_add.id)] if tag_to_add else []
            })

    def action_open_helpdesk_tickets(self):
        self.ensure_one()
        tickets = self.helpdesk_ticket_ids

        if not tickets:
            tickets = self.service_ticket_id

        if len(tickets) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'helpdesk.ticket',
                'view_mode': 'form',
                'res_id': tickets[0].id,
                'target': 'current',
            }
        else:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Related Tickets'),
                'res_model': 'helpdesk.ticket',
                'view_mode': 'tree,form',
                'domain': [('id', 'in', tickets.ids)],
                'target': 'current',
            }

    def action_open_ticket_wizard(self):
        self.ensure_one()
        warehouse_id = self.location_id.warehouse_id.id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Generate Ticket'),
            'res_model': 'generate.ticket.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
                'default_user_id': self.user_id.id,
                'default_warehouse_id': warehouse_id,
                'default_partner_id': self.partner_id.id,
            },
        }
