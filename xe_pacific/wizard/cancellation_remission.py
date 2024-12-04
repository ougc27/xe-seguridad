from odoo import fields, models, api


class CancelledRemissionWizard(models.TransientModel):
    _name = 'cancelled.remission.wizard'
    _description = 'Wizard for Cancelled Remission'

    picking_id = fields.Many2one('stock.picking', string="Transfer Folio", required=True)
    cancelled_reason = fields.Many2one('cancelled.remission.reason',
        string="Cancellation Reason",
        required=True)
    comments = fields.Html(string="Comments", required=True)
    tag_ids = fields.Many2many('inventory.tag', string="Tags")
    team_name = fields.Char()

    @api.model
    def default_get(self, fields):
        res = super(CancelledRemissionWizard, self).default_get(fields)
        picking_id = self.env.context.get('active_id', False)
        picking_record = self.env['stock.picking'].browse(picking_id)
        team_name = 'final_client'
        if 'constructoras' in picking_record.x_studio_canal_de_distribucin.lower():
            team_name = 'construction'
        if picking_id:
            res.update({
                'picking_id': picking_id,
                'tag_ids': picking_record.tag_ids,
                'team_name': team_name
            })
        return res

    def action_confirm(self):
        self.env['cancelled.remission'].create({
            'picking_id': self.picking_id.id,
            'remission_folio': self.picking_id.x_studio_folio_rem,
            'cancelled_date': fields.Datetime.now(),
            'user_id': self.env.user.id,
            'cancelled_reason': self.cancelled_reason.id,
            'comments': self.comments,
            'tag_ids': self.tag_ids
        })
        self.picking_id.write({'x_studio_folio_rem': ''})
        self.picking_id.action_cancel_transit()
        return {'type': 'ir.actions.act_window_close'}
