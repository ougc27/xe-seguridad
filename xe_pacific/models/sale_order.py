from odoo import models, tools, api, fields, _
from odoo.exceptions import UserError
import base64

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_exception = fields.Boolean(string="Excepción")

    pos_store = fields.Many2one('res.partner', domain="[('is_pos_store', '=', True)]")

    reference = fields.Char(
        string="OC Cliente",
        help="The payment communication of this sale order.",
        copy=False,
        index=True
    )

    helpdesk_ticket_ids = fields.One2many('helpdesk.ticket', 'sale_id', string="Helpdesk Tickets")

    ticket_count = fields.Integer(compute='_compute_helpdesk_ticket_ids')

    x_studio_pedido_fisico = fields.Char(string="Physical order")

    description = fields.Html()

    partner_commercial_name = fields.Char(
        related='partner_id.commercial_name',
        string='Commercial Name',
        store=True,
    )

    purchase_request_ids = fields.One2many(
        'purchase.request',
        'sale_order_id',
        string='Purchase Requests'
    )

    has_purchase_request = fields.Boolean(compute='_compute_has_purchase_request', copy=False, store=True)
    
    @api.depends('purchase_request_ids.state')
    def _compute_has_purchase_request(self):
        for record in self:
            record.has_purchase_request = any(
                pr.state != 'rejected' for pr in record.purchase_request_ids
            )

    @api.depends('team_id')
    def _compute_note(self):
        for order in self:
            if not order.team_id:
                return
            order.note = order.team_id.terms_and_conditions

    @api.depends('name', 'x_studio_pedido_fisico')
    @api.depends_context('from_helpdesk_ticket')
    def _compute_display_name(self):
        if not self._context.get('from_helpdesk_ticket'):
            return super()._compute_display_name()
        for rec in self:
            name = rec.name
            if rec.x_studio_pedido_fisico:
                name = f'{name} - {rec.x_studio_pedido_fisico}'
            rec.display_name = name

    @api.depends('helpdesk_ticket_ids')
    def _compute_helpdesk_ticket_ids(self):
        for order in self:
            order.ticket_count = len(order.helpdesk_ticket_ids)

    def write(self, vals):
        if 'order_line' in vals:
            for order in self:
                if order.picking_ids.filtered(lambda p: p.state == 'transit'):
                    if self.env.user.has_group('base.group_system'):
                        continue
                    raise UserError(_("You cannot modify sale order lines because there is at least one transfer in remission status."))
        return super(SaleOrder, self).write(vals)

    def action_unlock(self):
        for rec in self:
            if self.env.context.get('force_unlock'):
                rec.locked = False
                continue
            if rec.picking_ids.filtered(lambda picking: picking.state == 'transit'):
                raise UserError(_('An order cannot be unlocked if it has a delivery in remission status.'))
            rec.locked = False

    def action_cancel(self):
        for rec in self:
            shipping_line = rec.order_line.filtered(
                lambda l: l.product_id.default_code == 'SHIPPING-VA' and l.product_id.type == 'service'
            )
            for line in shipping_line:
                line.qty_delivered = 0
            delivered = rec.order_line.filtered(lambda line: line.qty_delivered > 0)
            transit_pickings = rec.picking_ids.filtered(lambda picking: picking.state == 'transit')
            if delivered or transit_pickings:
                raise UserError(_('It contains a delivered product or deliveries with the status "remission".'))
        return super().action_cancel()

    def _action_confirm(self):
        result = super()._action_confirm()
        for rec in self:
            separate_remissions = self.env['ir.config_parameter'].sudo().get_param(
                'separate_remissions_activate', None)
            if rec.company_id.id == 4 and separate_remissions == "1":
                no_separate = rec.order_line.filtered(
                    lambda record: record.product_id.default_code in ['FLTLOCAL', 'INSFOR'])
                if no_separate:
                    continue
                if rec.team_id.name.lower() == 'constructoras':
                    rec.picking_ids.separate_construction_remissions()
            for picking in rec.picking_ids:
                if picking.company_id.id == 4:
                    picking.update_tag_ids_to_pickings()
                moves = picking.move_ids.filtered(lambda move: (
                     move.product_id.product_tmpl_id.not_automatic_lot_number
                ))
                if moves:
                    picking.do_unreserve()
                    picking.action_assign()
        return result

    def action_open_helpdesk_tickets(self):
        self.ensure_one()
        tickets = self.helpdesk_ticket_ids

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
                'name': 'Tickets Relacionados',
                'res_model': 'helpdesk.ticket',
                'view_mode': 'tree,form',
                'domain': [('id', 'in', tickets.ids)],
                'target': 'current',
            }

    def action_open_purchase_requests(self):
        self.ensure_one()
        purchase_request_ids = self.purchase_request_ids

        if len(purchase_request_ids) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'purchase.request',
                'view_mode': 'form',
                'res_id': purchase_request_ids[0].id,
                'target': 'current',
            }
        else:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Purchase Requests'),
                'res_model': 'purchase.request',
                'view_mode': 'tree,form',
                'domain': [('id', 'in', purchase_request_ids.ids)],
                'target': 'current',
            }

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        if self.env.context.get('from_helpdesk_ticket'):
            query = """
                SELECT id
                    FROM sale_order
                    WHERE
                    (
                        name ilike %s
                    OR
                        x_studio_pedido_fisico ilike %s
                    )
                    AND
                        company_id = %s
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

    # def convert_attachments_to_snapshot(self):
    #     _logger.info("entre en el convert_attachments_to_snapshot")
    #     ruta = r"C:\Users\XE Sistemas\Downloads\snapshot.txt"
    #     with open(ruta, "rb") as f:
    #         #contenido_b64 = f.read().strip()
    #         contenido_b64 = f.read()

    #     # Decodificar base64 → bytes reales del snapshot
    #     snapshot_bytes = base64.b64encode(contenido_b64)
    #     #_logger.info(snapshot_bytes)
    #     # Interpretar bytes del snapshot → JSON
    #     #snapshot_json = json.loads(snapshot_bytes)

    #     attachment_description = self.env['ir.attachment'].browse(627984).description

    #     attachment = self.env['ir.attachment'].create({
    #         'name': 'snapshot.txt',
    #         'type': 'binary',
    #         "datas": attachment_description,
    #         'description': attachment_description,
    #         'res_model': self._name,
    #         'res_id': self.id,
    #         'mimetype': 'text/plain',
    #     })

    def convert_attachments_to_snapshot(self, odoo_root):

        with tools.file_open(odoo_root, 'rb') as f:
            contenido_bytes = f.read()

        contenido_b64 = base64.b64encode(contenido_bytes)

        attachment = self.env['ir.attachment'].create({
            'name': 'snapshot.txt',
            'type': 'binary',
            "datas": contenido_b64,
            'description': contenido_b64,
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'text/plain',
        })

        return attachment

    def action_open_generate_purchase_request_wizard(self):
        self.ensure_one()

        wizard = self.env['purchase.request.wizard'].create({
            'sale_order_id': self.id,
            'company_id': self.company_id.id,
            'requested_by': self.env.user.id,
        })

        lines = []
        for line in self.order_line:
            lines.append((0, 0, {
                'wizard_id': wizard.id,
                'product_id': line.product_id.id,
                'name': line.name,
                'product_qty': line.product_uom_qty,
                'product_uom_id': line.product_uom.id,
            }))

        wizard.write({'line_ids': lines})

        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Request'),
            'res_model': 'purchase.request.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }
