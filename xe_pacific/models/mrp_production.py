from odoo import fields, models, _
from odoo.exceptions import ValidationError, UserError
from odoo.tools import float_is_zero


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    def button_mark_done(self):
        res = self.pre_button_mark_done()
        if res is not True:
            return res
        
        for move_line in self.move_raw_line_ids:
            available_qty = self.check_available_stock_excluding_transit(move_line, True)
            if available_qty is not False:
                if available_qty < 0:
                    move_line_qty = move_line.quantity
                    raise ValidationError(
                        _(
                            "You cannot reserve the component {product_name} with lot {lot_name} "
                            "because the total available quantity is {qty}, but you are trying to remit "
                            "{requested_qty}, which exceeds the available stock."
                        ).format(
                            product_name=move_line.product_id.display_name,
                            lot_name=move_line.lot_id.name,
                            qty=available_qty + move_line_qty,
                            requested_qty=move_line_qty,
                        )
                )


        if self.env.context.get('mo_ids_to_backorder'):
            productions_to_backorder = self.browse(self.env.context['mo_ids_to_backorder'])
            productions_not_to_backorder = self - productions_to_backorder
        else:
            productions_not_to_backorder = self
            productions_to_backorder = self.env['mrp.production']
        productions_not_to_backorder = productions_not_to_backorder.with_context(no_procurement=True)
        self.workorder_ids.button_finish()

        backorders = productions_to_backorder and productions_to_backorder._split_productions()
        backorders = backorders - productions_to_backorder

        productions_not_to_backorder._post_inventory(cancel_backorder=True)
        productions_to_backorder._post_inventory(cancel_backorder=True)

        # if completed products make other confirmed/partially_available moves available, assign them
        done_move_finished_ids = (productions_to_backorder.move_finished_ids | productions_not_to_backorder.move_finished_ids).filtered(lambda m: m.state == 'done')
        done_move_finished_ids._trigger_assign()

        # Moves without quantity done are not posted => set them as done instead of canceling. In
        # case the user edits the MO later on and sets some consumed quantity on those, we do not
        # want the move lines to be canceled.
        (productions_not_to_backorder.move_raw_ids | productions_not_to_backorder.move_finished_ids).filtered(lambda x: x.state not in ('done', 'cancel')).write({
            'state': 'done',
            'product_uom_qty': 0.0,
        })
        for production in self:
            production.write({
                'date_finished': fields.Datetime.now(),
                'priority': '0',
                'is_locked': True,
                'state': 'done',
            })

        # It is prudent to reserve any quantity that has become available to the backorder
        # production's move_raw_ids after the production which spawned them has been marked done.
        backorders_to_assign = backorders.filtered(
            lambda order:
            order.picking_type_id.reservation_method == 'at_confirm'
        )
        for backorder in backorders_to_assign:
            backorder.action_assign()

        report_actions = self._get_autoprint_done_report_actions()
        if self.env.context.get('skip_redirection'):
            if report_actions:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'do_multi_print',
                    'context': {},
                    'params': {
                        'reports': report_actions,
                    }
                }
            return True
        another_action = False
        if not backorders:
            if self.env.context.get('from_workorder'):
                another_action = {
                    'type': 'ir.actions.act_window',
                    'res_model': 'mrp.production',
                    'views': [[self.env.ref('mrp.mrp_production_form_view').id, 'form']],
                    'res_id': self.id,
                    'target': 'main',
                }
            elif self.user_has_groups('mrp.group_mrp_reception_report'):
                mos_to_show = self.filtered(lambda mo: mo.picking_type_id.auto_show_reception_report)
                lines = mos_to_show.move_finished_ids.filtered(lambda m: m.product_id.type == 'product' and m.state != 'cancel' and m.picked and not m.move_dest_ids)
                if lines:
                    if any(mo.show_allocation for mo in mos_to_show):
                        another_action = mos_to_show.action_view_reception_report()
            if report_actions:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'do_multi_print',
                    'params': {
                        'reports': report_actions,
                        'anotherAction': another_action,
                    }
                }
            if another_action:
                return another_action
            return True
        context = self.env.context.copy()
        context = {k: v for k, v in context.items() if not k.startswith('default_')}
        for k, v in context.items():
            if k.startswith('skip_'):
                context[k] = False
        another_action = {
            'res_model': 'mrp.production',
            'type': 'ir.actions.act_window',
            'context': dict(context, mo_ids_to_backorder=None)
        }
        if len(backorders) == 1:
            another_action.update({
                'views': [[False, 'form']],
                'view_mode': 'form',
                'res_id': backorders[0].id,
            })
        else:
            another_action.update({
                'name': _("Backorder MO"),
                'domain': [('id', 'in', backorders.ids)],
                'views': [[False, 'list'], [False, 'form']],
                'view_mode': 'tree,form',
            })
        if report_actions:
            return {
                'type': 'ir.actions.client',
                'tag': 'do_multi_print',
                'params': {
                    'reports': report_actions,
                    'anotherAction': another_action,
                }
            }
        return another_action