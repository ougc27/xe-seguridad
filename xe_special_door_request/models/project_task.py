from odoo import api, models

MODULE = 'xe_special_door_request'

# Etapas 1 a 5: la OC todavía no se recibe; cancelar aquí es "sin OC"
STAGES_BEFORE_PO_RECEPTION = (
    'validate_measures',
    'validate_skus',
    'replace_skus',
    'authorize_purchase',
    'confirm_po',
)
STAGE_DELIVERED = 'delivered'
STAGE_CANCELLED_NO_PO = 'cancelled_no_po'
STAGE_CANCELLED_WITH_PO = 'cancelled_with_po'
CANCELLED_STAGES = (STAGE_CANCELLED_NO_PO, STAGE_CANCELLED_WITH_PO)

# Evita que la propia sincronización vuelva a dispararse
SYNC_CONTEXT_KEY = 'special_door_sync'


class ProjectTask(models.Model):
    _inherit = 'project.task'

    @api.model
    def _special_door_project(self):
        return self.env.ref('%s.project_special_door' % MODULE, raise_if_not_found=False)

    @api.model
    def _special_door_stages(self, keys):
        # Etapas del proyecto por clave (xmlid stage_special_door_<clave>)
        stages = self.env['project.task.type']
        for key in keys:
            stages |= self.env.ref(
                '%s.stage_special_door_%s' % (MODULE, key), raise_if_not_found=False) or stages.browse()
        return stages

    @api.model
    def _special_door_active_request_domain(self, order_ids):
        # Solicitud en curso: cualquier tarea de la OV en el proyecto que no esté cancelada
        project = self._special_door_project()
        return [
            ('project_id', '=', project.id),
            ('sale_order_id', 'in', order_ids),
            ('stage_id', 'not in', self._special_door_stages(CANCELLED_STAGES).ids),
            ('state', '!=', '1_canceled'),
        ]

    @api.model_create_multi
    def create(self, vals_list):
        tasks = super().create(vals_list)
        tasks._sync_special_door_state_stage()
        return tasks

    def write(self, vals):
        res = super().write(vals)
        if ('stage_id' in vals or 'state' in vals) and not self.env.context.get(SYNC_CONTEXT_KEY):
            self._sync_special_door_state_stage()
        return res

    def _sync_special_door_state_stage(self):
        """Mantiene alineados el estado y la etapa, solo en el proyecto de puertas especiales.

        - Estado Cancelado en etapas 1 a 5 -> "Cancelada sin OC"; en las demás -> "Cancelada con OC confirmada"
        - Movida a una etapa de cancelación -> estado Cancelado
        - Movida a "Entregada al cliente" -> estado Hecho
        Cambiar de etapa conserva el estado cuando ya está cerrado, así que no se reabre.
        """
        project = self._special_door_project()
        tasks = self.filtered(lambda task: project and task.project_id == project)
        if not tasks:
            return
        before_po_reception = self._special_door_stages(STAGES_BEFORE_PO_RECEPTION)
        cancelled_no_po = self._special_door_stages([STAGE_CANCELLED_NO_PO])
        cancelled_with_po = self._special_door_stages([STAGE_CANCELLED_WITH_PO])
        cancelled = cancelled_no_po | cancelled_with_po
        delivered = self._special_door_stages([STAGE_DELIVERED])

        for task in tasks.with_context(**{SYNC_CONTEXT_KEY: True}):
            if task.state == '1_canceled' and task.stage_id not in cancelled:
                target = cancelled_no_po if task.stage_id in before_po_reception else cancelled_with_po
                if target:
                    task.write({'stage_id': target.id})
            elif task.stage_id in cancelled and task.state != '1_canceled':
                task.write({'state': '1_canceled'})
            elif delivered and task.stage_id == delivered and task.state != '1_done':
                task.write({'state': '1_done'})
