import logging
from collections import defaultdict
from odoo import models, api, fields, Command

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_exception = fields.Boolean(string="Excepción")

    def mark_tasks_as_exception(self):
        for line in self.order_line:
            task_stage = self.env['project.task.type'].sudo().search([
                ('name', 'ilike', '%EXCEPCIÓN'),
                ('project_ids', '=', line.task_id.project_id.id)
            ], limit=1)
            if task_stage:
                line.task_id.write({
                    'stage_id': task_stage.id
                })

    def write(self, vals):
        res = super().write(vals)
        for rec in self:
            _logger.info("entre en el write")
            if rec.is_exception and rec.state == 'sale':
                rec.mark_tasks_as_exception()
        return res

    def _action_confirm(self):
        result = super()._action_confirm()
        for rec in self:
            if rec.team_id.name == 'CONSTRUCTORAS':
                for line in rec.order_line:
                    sku = line.product_id.default_code
                    if sku in ('INS10', 'INSBASCDI', 'VISREF', 'VISTEC_COMPRADA'):
                        project_id = self.env['project.project'].search(
                            [('name', '=', 'E.- INSTALACIONES CONSTRUCTORAS')]).id
                        line.task_id.write({
                            'sale_order_id': rec.id,
                            'project_id': project_id,
                            'sale_line_id': line.id,
                            'partner_id': rec.partner_id.id 
                        })
            else:
                for line in rec.order_line:
                    line.task_id.write({
                        'sale_order_id': rec.id,
                    })
        #if rec.is_exception:
                """for line in rec.order_line:
                    task_stage = self.env['project.task.type'].sudo().search([
                        ('name', 'ilike', '%EXCEPCIÓN'),
                        ('project_ids', '=', line.task_id.project_id.id)
                    ], limit=1)
                    if task_stage:
                        line.task_id.write({
                            'stage_id': task_stage.id
                        })"""
                #rec.mark_tasks_as_exception()
        return result

    @api.depends('order_line.product_id.project_id', 'order_line.project_id')
    def _compute_tasks_ids(self):
        tasks_per_so = self.env['project.task']._read_group(
            domain=['&', ('project_id', '!=', False), '|', ('sale_line_id', 'in', self.order_line.ids), ('sale_order_id', 'in', self.ids)],
            groupby=['sale_order_id'],
            aggregates=['id:recordset', '__count']
        )
        so_with_tasks = self.env['sale.order']
        for order, tasks_ids, tasks_count in tasks_per_so:
            if order:
                order.tasks_ids = tasks_ids
                order.tasks_count = tasks_count
                so_with_tasks += order
            else:
                # tasks that have no sale_order_id need to be associated with the SO from their sale_line_id
                for task in tasks_ids:
                    task_so = task.sale_line_id.order_id
                    task_so.tasks_ids = [Command.link(task.id)]
                    task_so.tasks_count += 1
                    so_with_tasks += task_so
        remaining_orders = self - so_with_tasks
        if remaining_orders:
            remaining_orders.tasks_ids = [Command.clear()]
            remaining_orders.tasks_count = 0

    @api.depends('order_line.product_id', 'order_line.project_id')
    def _compute_project_ids(self):
        is_project_manager = self.user_has_groups('project.group_project_manager')
        projects = self.env['project.project'].search([('sale_order_id', 'in', self.ids)])
        projects_per_so = defaultdict(lambda: self.env['project.project'])
        for project in projects:
            projects_per_so[project.sale_order_id.id] |= project
        for order in self:
            projects = order.order_line.mapped('task_id.project_id')
            if not projects:
                projects |= order.order_line.mapped('product_id.project_id')
            projects |= order.project_id
            projects |= projects_per_so[order.id or order._origin.id]
            if not is_project_manager:
                projects = projects._filter_access_rules('read')
            order.project_ids = projects
            order.project_count = len(projects)