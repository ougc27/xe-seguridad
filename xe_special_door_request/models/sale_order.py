from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

SPECIAL_DOOR_ORDER_STATES = ('draft', 'sent', 'sale')


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    show_special_door_button = fields.Boolean(compute='_compute_show_special_door_button')

    @api.depends('state', 'order_line.display_type', 'order_line.product_id.is_special_door')
    def _compute_show_special_door_button(self):
        for order in self:
            order.show_special_door_button = bool(
                order._origin.id
                and order.state in SPECIAL_DOOR_ORDER_STATES
                and order._get_special_door_lines()
                and not order._has_special_door_request()
            )

    def _get_special_door_lines(self):
        return self.order_line.filtered(
            lambda line: not line.display_type and line.product_id.is_special_door)

    def _has_special_door_request(self):
        # El vendedor no necesariamente lee tareas: se busca con sudo
        self.ensure_one()
        Task = self.env['project.task'].sudo()
        if not Task._special_door_project():
            return False
        return bool(Task.search_count(Task._special_door_active_request_domain(self._origin.ids)))

    def _check_special_door_request_allowed(self):
        self.ensure_one()
        if self.state not in SPECIAL_DOOR_ORDER_STATES:
            raise UserError("Solo se puede solicitar en cotizaciones u órdenes confirmadas.")
        if not self.env['project.task'].sudo()._special_door_project():
            raise UserError("No existe el proyecto de puertas de medida especial. Avisa a Sistemas.")
        if self._has_special_door_request():
            raise UserError(
                "La orden %s ya tiene una solicitud de puerta de medida especial en curso. "
                "Agrega las puertas adicionales en la tarea existente." % self.name)

    def action_open_special_door_request(self):
        """Abre el asistente con una fila por cada línea de puerta de medida especial."""
        self.ensure_one()
        self._check_special_door_request_allowed()
        door_lines = self._get_special_door_lines()
        if not door_lines:
            raise UserError("La orden no tiene líneas de puerta de medida especial.")

        WizardLine = self.env['special.door.request.wizard.line']
        openings = WizardLine._get_placeholder_openings()
        line_commands = []
        for line in door_lines:
            product = line.product_id
            is_placeholder = WizardLine._is_placeholder_product(product)
            values = {
                'sale_line_id': line.id,
                'product_id': product.id,
                'is_placeholder': is_placeholder,
                'include': True,
                'quantity': line.product_uom_qty,
            }
            if is_placeholder:
                values['opening'] = openings.get(product.id, False)
            line_commands.append(Command.create(values))

        wizard = self.env['special.door.request.wizard'].create({
            'sale_order_id': self.id,
            'line_ids': line_commands,
        })
        return wizard._get_reopen_action()
