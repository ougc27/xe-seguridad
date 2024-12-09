from odoo import api, models, fields, _
from odoo.osv import expression
from odoo.exceptions import UserError


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
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_states')

    shipment_task_status = fields.Selection([
        ('to_scheduled', 'POR PROGRAMAR'),
        ('scheduled', 'PROGRAMADO'),
        ('warehouse', 'ALMACÉN'),
        ('production', 'PRODUCCIÓN'),
        ('shipments', 'EMBARQUES'),
        ('confirmed', 'EMBARCADO'),
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_status')

    x_studio_canal_de_distribucin = fields.Char(
        string="Nombre del Equipo de Ventas",
        related='group_id.sale_id.partner_id.team_id.name',
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
        'inventory.tag', 'inventory_tag_rel', 'picking_id', 'tag_id', string='Tags')
    
    #invoice_ids = fields.Many2many(
        #'account.move',
        #'account_move_picking_rel',
        #'picking_id',
        #'move_id',
        #string='Invoices',
        #help='Match the invoices with the shipment.',
        #readonly=True
    #)

    @api.depends('location_id', 'move_ids', 'x_studio_canal_de_distribucin')
    def _compute_shipping_assignment(self):
        for rec in self:
            if rec.company_id.id == 4:
                if rec.x_studio_canal_de_distribucin == 'INTERGRUPO':
                    rec.shipping_assignment = 'shipments'
                    continue
                if rec.location_id and rec.location_id.warehouse_id.name:
                    if any(keyword in rec.location_id.warehouse_id.name for keyword in ['Amazon', 'MercadoLibre']):
                        rec.shipping_assignment = 'shipments'
                        continue
                    if rec.location_id.warehouse_id.name == 'Monterrey PR': 
                        if rec.x_studio_canal_de_distribucin == 'DISTRIBUIDORES':
                            rec.shipping_assignment = 'shipments'
                            continue
                        if rec.move_ids.filtered(lambda record: record.product_id.default_code == 'FLTENVIO'):
                            rec.shipping_assignment = 'shipments'
                            continue
                rec.shipping_assignment= 'logistics'
                continue
            rec.shipping_assignment = False

    def _group_expand_states(self, states, domain, order):
        return [key for key, val in self._fields['kanban_task_status'].selection]

    def _group_expand_status(self, states, domain, order):
        return [key for key, val in self._fields['shipment_task_status'].selection]

    def _create_separated_picking_by_categ(self, rec, moves, sale_order, scheduled_date):
        new_picking = rec.copy({
            'is_remission_separated': True,
            'scheduled_date': scheduled_date,
            'state': 'confirmed',
        })
        moves.write({'picking_id': new_picking.id})
        rec.create_notes_in_pickings(sale_order, new_picking)
        for move in new_picking.move_ids:
            if move not in moves:
                move.unlink()
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
        for move in move_ids:
            quantity = int(move.product_uom_qty)
            if move.id == move_ids[0].id and not construction:
                quantity -= 1
            if quantity >= 1:
                move.write({'product_uom_qty': 1})
            for i in range(quantity):
                new_picking = self.copy(
                    {
                        'is_remission_separated': True,
                        'scheduled_date': scheduled_date,
                    }
                )
                for new_move in new_picking.move_ids:
                    if new_move.product_id.id == move.product_id.id:
                        new_move.write({'product_uom_qty': 1, 'state': 'confirmed'})

                        continue
                    new_move.unlink()
                new_picking.write({'state': 'confirmed'})
                self.create_notes_in_pickings(sale_order, new_picking)

    def separate_remissions(self):
        for rec in self:
            first_move_id = rec.move_ids[0].id
            sale_order = rec.env['sale.order'].search(
                [('name', '=', rec.group_id.name), ('company_id', '=', rec.env.company.id)])
            scheduled_date = rec.scheduled_date
            rec.write({'is_remission_separated': True})
            rec.single_product_separation(rec.move_ids, sale_order, scheduled_date)
            for move in rec.move_ids:
                if move.id != first_move_id:
                    move.unlink()

    def separate_construction_remissions(self):
        for rec in self:
            sale_order = rec.env['sale.order'].search(
                [('name', '=', rec.group_id.name), ('company_id', '=', rec.env.company.id)]
            )
            scheduled_date = rec.scheduled_date

            if len(rec.move_ids) == 1:
                rec.single_product_separation(rec.move_ids, sale_order, scheduled_date)
                continue
    
            door_moves = rec.move_ids.filtered(
                lambda m: m.product_id.categ_id.complete_name == 'Ventas / XE / Puertas / Puertas' and 
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
                    grouped_moves.append((move, 1))
                    move.write({'product_uom_qty': move.product_uom_qty - 1})
    
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
                        if new_move.product_id.id == original_move.product_id.id:
                            new_move.write({'product_uom_qty': qty, 'state': 'confirmed'})
                            break
                    else:
                        new_move.unlink()
    
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
                        if new_move.product_id.id == original_move.product_id.id:
                            new_move.write({'product_uom_qty': qty, 'state': 'confirmed'})
                            break
                    else:
                        new_move.unlink()

                rec.create_notes_in_pickings(sale_order, new_picking)
        
                if not door_moves and not lock_moves:
                    if door_installation_moves:
                        rec.single_product_separation(
                            door_installation_moves, sale_order, scheduled_date, True
                        )
                    if lock_installation_moves:
                        rec.single_product_separation(
                            lock_installation_moves, sale_order, scheduled_date, True
                        )
        
                rec.unlink()
            elif not rec.move_ids.filtered(lambda m: m.product_uom_qty > 0):
                rec.unlink()

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
            scheduled_date = rec.scheduled_date
    
            if service_moves:
                rec._create_separated_picking_by_categ(
                    rec, service_moves, sale_order, scheduled_date)
    
            if installation_moves:
                rec._create_separated_picking_by_categ(
                    rec, installation_moves, sale_order, scheduled_date)

            rec.write({'is_remission_separated': True})
                
    def combine_remissions(self):
        if not self:
            return

        primary_picking = self[0]

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
                    lambda m: m.product_id == move.product_id and m.product_uom == move.product_uom
                )
                if existing_move:
                    existing_move.product_uom_qty += move.product_uom_qty
                else:
                    move.write({'picking_id': primary_picking.id})
            
            picking.action_cancel()
            picking.unlink()

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

