from odoo import models, api, fields


class ProjectTask(models.Model):
    _inherit = 'project.task'

    supervisor_id = fields.Many2one('supervisor.installer', string="Supervisor")
    installer_id = fields.Many2one('supervisor.installer', string="Installer")

    # @api.model
    # def create(self, vals):
    #     """Modify method of create record.

    #     Calculate minutes between responses of salesperson and customer

    #     :param vals: Dictionary of values to create new record.
    #     :param type: dict

    #     :returns: Created record
    #     :rtype: whatsapp.message
    #     """
    #     records = super().create(vals)
    #     for rec in records:
    #         sale_line_id = rec.sale_line_id
    #         if sale_line_id:
    #             order_id = sale_line_id.order_id
    #             sku = sale_line_id.product_id.default_code
    #             if order_id.team_id.name == 'CONSTRUCTORAS':
    #                 if sku in ('INS10', 'INSBASCDI', 'VISREF', 'VISTEC_COMPRADA'):
    #                     project_id = self.env['project.project'].search(
    #                         [('name', '=', 'E.- INSTALACIONES CONSTRUCTORAS')]).id
    #                     rec.write({
    #                         'project_id': project_id,
    #                         'sale_line_id': sale_line_id.id,
    #                         'partner_id': order_id.partner_id.id,
    #                     })
    #             rec.write({
    #                 'sale_order_id': sale_line_id.order_id.id
    #             })
    #     return records

    def write(self, vals):
        res = super().write(vals)
        for rec in self:
            if 'user_ids' in vals:
                for user in rec.user_ids:
                    supervisor_id = self.env['supervisor.installer'].search(
                        [('employee_id', '=', user.employee_id.id), ('installer_number', '=', 0)]).id
                    if supervisor_id:
                        rec.write({'supervisor_id': supervisor_id})
            # if 'stage_id' in vals:
            #     stage_map = {
            #         'Programado': 'Activo Programado',
            #         'Realizada': 'Finalizado'
            #     }
            #     for keyword, stage_name in stage_map.items():
            #         if keyword.lower() in rec.stage_id.name.lower():
            #             rec.helpdesk_ticket_id.write({
            #                 'stage_id': self.env['helpdesk.stage'].search(
            #                     [('name', 'ilike', stage_name)], limit=1).id
            #             })
            #             break
        return res
