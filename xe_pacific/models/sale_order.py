from odoo import models, api, fields, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_exception = fields.Boolean(string="Excepción")

    pos_store = fields.Many2one('res.partner', domain="[('is_pos_store', '=', True)]", copy=False)

    reference = fields.Char(
        string="OC Cliente",
        help="The payment communication of this sale order.",
        copy=False,
        index=True
    )

    helpdesk_ticket_ids = fields.One2many('helpdesk.ticket',
        'sale_id',
        string="Helpdesk Tickets",
        copy=False)

    ticket_count = fields.Integer(compute='_compute_helpdesk_ticket_ids', copy=False)

    x_studio_pedido_fisico = fields.Char(string="Physical order", copy=False)

    survey_id = fields.Many2one('survey.survey', copy=False)

    user_input_id = fields.Many2one('survey.user_input', copy=False)

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
                    picking.update_tag_ids_to_pickings(False)
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

    def action_send_survey(self):
        """ Open a window to compose an email, pre-filled with the survey message """
        self.ensure_one()
        if self.user_input_id:
            raise UserError(_('A survey response already exists for this sales order.'))
        return self.survey_id.with_context(
            **{
                k: v.id
                for k, v in {
                    'partner_id': self.partner_id,
                    'partner_invoice_id': self.partner_invoice_id,
                    'partner_shipping_id': self.partner_shipping_id,
                }.items()
                if v and v.email
            },
            existing_mode='new'
        ).action_send_survey()
