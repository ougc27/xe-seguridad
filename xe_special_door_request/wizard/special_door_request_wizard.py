import hashlib

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

MODULE = 'xe_special_door_request'

MIN_WIDTH = 55
MAX_WIDTH = 200
MIN_HEIGHT = 150
MAX_HEIGHT = 300
# Configuración por ancho (el modelo puede ampliar el máximo de sencilla)
DEFAULT_SINGLE_MAX_WIDTH = 119
DOUBLE_MIN_WIDTH = 160
UNSPSC_DOORS_CODE = '30171500'

OPENING_SELECTION = [('DE', 'Derecha'), ('IZ', 'Izquierda')]
OPENING_LABELS = dict(OPENING_SELECTION)
OPENING_NAME_SUFFIX = {'DE': 'DER', 'IZ': 'IZQ'}
# Apertura de cada placeholder del módulo
PLACEHOLDER_OPENINGS = {
    'product_placeholder_right': 'DE',
    'product_placeholder_left': 'IZ',
}

STATUS_LABELS = {
    'new': "No",
    'active': "Sí",
    'pending': "Sí (por validar)",
    'archived': "Sí (archivado)",
}

# Parámetros de Ajustes de Ventas: clave -> (parámetro del sistema, modelo)
CONFIG_PARAMS = {
    'validator': ('%s.sku_validator_user_id' % MODULE, 'res.users'),
    'tag_single': ('%s.tag_single_id' % MODULE, 'product.tag'),
    'tag_fixed': ('%s.tag_fixed_id' % MODULE, 'product.tag'),
    'tag_double': ('%s.tag_double_id' % MODULE, 'product.tag'),
    'sale_tag': ('%s.sale_tag_id' % MODULE, 'crm.tag'),
}
CONFIG_LABELS = {
    'validator': "Validador de SKUs",
    'tag_single': "Etiqueta Sencilla",
    'tag_fixed': "Etiqueta Con fijo",
    'tag_double': "Etiqueta Doble",
    'sale_tag': "Etiqueta de la orden de venta",
}

DETAIL_HEADER = [
    ("Modelo", 12), ("Ancho", 7), ("Alto", 7), ("Color", 12), ("Apertura", 9),
    ("Cantidad", 8), ("SKU sugerido", 14), ("Nombre", 22), ("¿Ya existía?", 9),
]
SKU_COLUMN_INDEX = 6


def format_qty(value):
    # Muestra la cantidad sin decimales cuando es entera
    return ('%d' % value) if value == int(value) else ('%.2f' % value)


def record_link(model_name, record_id, label):
    # Enlace interno al formulario de un registro
    return Markup('<a href="/web#id=%s&amp;model=%s&amp;view_type=form">%s</a>') % (
        record_id, model_name, label)


class SpecialDoorRequestWizard(models.TransientModel):
    _name = 'special.door.request.wizard'
    _description = "Solicitud de puerta de medida especial"

    sale_order_id = fields.Many2one(
        'sale.order', string="Orden de venta", required=True, readonly=True, ondelete='cascade')
    line_ids = fields.One2many('special.door.request.wizard.line', 'wizard_id', string="Puertas")
    notes = fields.Text(string="Observaciones para compras")
    # Captura -> ventana de advertencias (mismo asistente)
    step = fields.Selection(
        [('edit', "Captura"), ('confirm', "Advertencias")], default='edit', required=True)
    warning_message = fields.Html(string="Advertencias", readonly=True)

    def _get_reopen_action(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Solicitud de puerta de medida especial",
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(self.env.ref('%s.special_door_request_wizard_view_form' % MODULE).id, 'form')],
            'target': 'new',
            'context': {'dialog_size': 'extra-large'},
        }

    # ------------------------------------------------------------------
    # Botones
    # ------------------------------------------------------------------
    def action_submit(self):
        self.ensure_one()
        rows = self._check_blocking_errors()
        warnings = self._get_warnings(rows)
        if warnings:
            self.write({
                'step': 'confirm',
                'warning_message': Markup(
                    '<p><b>Revisa lo siguiente antes de crear la solicitud:</b></p><ul>%s</ul>'
                ) % Markup('').join(Markup('<li>%s</li>') % text for text in warnings),
            })
            return self._get_reopen_action()
        return self._create_request(rows)

    def action_confirm_warnings(self):
        # Las advertencias ya se revisaron; los errores que bloquean se vuelven a validar
        self.ensure_one()
        rows = self._check_blocking_errors()
        return self._create_request(rows)

    def action_back(self):
        self.ensure_one()
        self.write({'step': 'edit', 'warning_message': False})
        return self._get_reopen_action()

    # ------------------------------------------------------------------
    # Validaciones
    # ------------------------------------------------------------------
    def _get_config(self, keys):
        # Lee de Ajustes los registros configurados; los faltantes quedan vacíos
        ICP = self.env['ir.config_parameter'].sudo()
        config = {}
        for key in keys:
            param, model_name = CONFIG_PARAMS[key]
            value = ICP.get_param(param) or ''
            config[key] = self.env[model_name].sudo().browse(
                int(value) if value.isdigit() else 0).exists()
        return config

    def _get_rows_to_create(self, rows):
        return rows.filtered(lambda row: row.is_placeholder and row.existing_status == 'new')

    def _check_blocking_errors(self):
        """Valida todo lo que impide crear la solicitud y regresa las filas incluidas."""
        self.ensure_one()
        order = self.sale_order_id
        order._check_special_door_request_allowed()

        rows = self.line_ids.filtered('include')
        if not rows:
            raise UserError("Incluye al menos una puerta en la solicitud.")

        errors = []
        for row in rows:
            label = row.product_id.display_name or "Fila sin producto"
            if row.quantity <= 0:
                errors.append("%s: la cantidad debe ser mayor a cero." % label)
            if not row.is_placeholder:
                continue
            if not row.model_id:
                errors.append("%s: falta el modelo." % label)
            elif not row.model_id.categ_id:
                errors.append("%s: el modelo %s no tiene categoría de producto configurada. Avisa a Sistemas."
                              % (label, row.model_id.name))
            if not row.color_id:
                errors.append("%s: falta el color." % label)
            if not row.opening:
                errors.append("%s: falta la apertura." % label)
            if row.width < MIN_WIDTH or row.width > MAX_WIDTH:
                errors.append("%s: el ancho debe estar entre %s y %s cm." % (label, MIN_WIDTH, MAX_WIDTH))
            if row.height < MIN_HEIGHT or row.height > MAX_HEIGHT:
                errors.append("%s: el alto debe estar entre %s y %s cm." % (label, MIN_HEIGHT, MAX_HEIGHT))
        if errors:
            raise UserError("Corrige lo siguiente antes de continuar:\n\n" + "\n".join(errors))

        # Configuración en Ajustes: la etiqueta de la OV siempre; el resto solo si hay SKUs por crear
        rows_to_create = self._get_rows_to_create(rows)
        required_keys = ['sale_tag']
        if rows_to_create:
            required_keys += ['validator', 'tag_single', 'tag_fixed', 'tag_double']
        config = self._get_config(required_keys)
        missing = [CONFIG_LABELS[key] for key in required_keys if not config[key]]
        if config.get('validator') and not config['validator'].active:
            missing.append(CONFIG_LABELS['validator'])
        if missing:
            raise UserError("Falta configurar en Ajustes de Ventas › Puertas de medida especial: %s. "
                            "Avisa a Sistemas." % ", ".join(missing))

        # Los SKUs nuevos copian los impuestos del placeholder
        placeholders = rows_to_create.product_id.sudo()
        without_taxes = placeholders.filtered(lambda product: not product.taxes_id or not product.supplier_taxes_id)
        if without_taxes:
            raise UserError("El placeholder %s no tiene impuestos de venta y de compra. Avisa a Sistemas."
                            % ", ".join(without_taxes.mapped('display_name')))
        return rows

    def _get_warnings(self, rows):
        """Advertencias que no bloquean; se confirman en la ventana de advertencias."""
        order = self.sale_order_id
        warehouse = order.warehouse_id
        warnings = []
        for row in rows:
            if row.is_placeholder and row.width > row.height:
                warnings.append("%s: el ancho (%s) es mayor que el alto (%s). ¿Medidas invertidas?"
                                % (row.sku, row.width, row.height))
            if row.existing_status == 'archived':
                warnings.append("%s: el SKU existe pero está archivado." % row.sku)
            # Existencia libre del SKU (existente o ya dado de alta) en el almacén de la OV
            stock_product = row.existing_product_id if row.is_placeholder else row.product_id
            if warehouse and stock_product and stock_product.active:
                free_qty = stock_product.sudo().with_company(order.company_id).with_context(
                    warehouse=warehouse.id, warehouse_id=warehouse.id).free_qty
                if free_qty > 0:
                    warnings.append("%s: hay %s disponibles en %s. Revisa si es necesario pedirla."
                                    % (row.sku, format_qty(free_qty), warehouse.name))
        return warnings

    # ------------------------------------------------------------------
    # Creación
    # ------------------------------------------------------------------
    def _lock_sale_order(self):
        # Bloquea la OV para que dos clics o dos usuarios no creen dos tareas.
        # Si otra transacción la tiene bloqueada, Odoo reintenta la petición y la
        # revalidación encuentra la tarea ya creada.
        self.env.cr.execute(
            'SELECT id FROM sale_order WHERE id = %s FOR UPDATE NOWAIT', [self.sale_order_id.id])

    def _lock_sku(self, sku):
        # Candado por SKU para que dos solicitudes simultáneas no den de alta el mismo código
        key = int(hashlib.sha1(sku.encode()).hexdigest()[:15], 16)
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [key])
        if not self.env.cr.fetchone()[0]:
            raise UserError("Otra solicitud está dando de alta el SKU %s en este momento. "
                            "Intenta de nuevo en unos segundos." % sku)

    def _create_request(self, rows):
        self.ensure_one()
        order = self.sale_order_id
        self._lock_sale_order()
        order._check_special_door_request_allowed()

        Task = self.env['project.task'].sudo()
        first_stage = Task._special_door_stages(['validate_measures'])
        # Nunca "»" en el nombre: hay acciones automatizadas ajenas que lo parten (y una borra la tarea)
        task_name = ('%s – %s' % (order.name, order.partner_id.display_name)).replace('»', '-')
        task = Task.create({
            'name': task_name,
            'project_id': Task._special_door_project().id,
            'stage_id': first_stage.id,
            'partner_id': order.partner_id.id,
            'user_ids': [Command.set([])],
        })

        created_by_sku = self._create_missing_products(rows, task)
        detail_html = self._build_detail_html(rows, created_by_sku)

        # sale_project limpia sale_order_id al crear porque el proyecto no es facturable:
        # se escribe en un segundo paso. El estado se fija porque una acción automatizada
        # ajena al flujo puede dejarlo en "Cambios solicitados"
        task.write({
            'description': detail_html,
            'sale_order_id': order.id,
            'state': '01_in_progress',
        })
        task.message_subscribe(partner_ids=self.env.user.partner_id.ids)
        # Registro original de la solicitud en el chatter
        task.message_post(body=detail_html, message_type='comment', subtype_xmlid='mail.mt_note')

        # Etiqueta en la OV para que los filtros actuales sigan funcionando
        sale_tag = self._get_config(['sale_tag'])['sale_tag']
        order.sudo().write({'tag_ids': [Command.link(sale_tag.id)]})
        return {'type': 'ir.actions.act_window_close'}

    def _create_missing_products(self, rows, task):
        """Da de alta los SKUs nuevos sin venta ni compra hasta que Sistemas los valide."""
        rows_to_create = self._get_rows_to_create(rows)
        if not rows_to_create:
            return {}
        config = self._get_config(['validator', 'tag_single', 'tag_fixed', 'tag_double'])
        uom = self.env.ref('uom.product_uom_unit')
        buy_route = self.env.ref('purchase_stock.route_warehouse0_buy', raise_if_not_found=False)
        unspsc = self.env['product.unspsc.code'].sudo().search([('code', '=', UNSPSC_DOORS_CODE)], limit=1)
        ProductTemplate = self.env['product.template'].sudo()
        Product = self.env['product.product'].sudo().with_context(active_test=False)

        created_by_sku = {}
        for row in rows_to_create:
            if row.sku in created_by_sku:
                continue
            self._lock_sku(row.sku)
            # Vuelve a buscar justo antes de crear (otra solicitud pudo crearlo mientras tanto)
            if Product.search_count([('default_code', '=', row.sku)]):
                continue
            placeholder = row.product_id.sudo()
            template = ProductTemplate.create({
                'name': row.sku_name,
                'default_code': row.sku,
                'detailed_type': 'product',
                'categ_id': row.model_id.categ_id.id,
                'product_tag_ids': [Command.set(
                    row.model_id.product_tag_ids.ids + row._get_configuration_tag(config).ids)],
                'uom_id': uom.id,
                'uom_po_id': uom.id,
                'tracking': 'lot',
                'route_ids': [Command.set(buy_route.ids if buy_route else [])],
                'unspsc_code_id': unspsc.id,
                'invoice_policy': 'delivery',
                'list_price': 0.0,
                # Impuestos tomados del placeholder: se mantienen en un solo lugar
                'taxes_id': [Command.set(placeholder.taxes_id.ids)],
                'supplier_taxes_id': [Command.set(placeholder.supplier_taxes_id.ids)],
                'sale_ok': False,
                'purchase_ok': False,
                'company_id': False,
                'is_special_door': True,
            })
            template.activity_schedule(
                'mail.mail_activity_data_todo',
                summary="Validar SKU de puerta de medida especial",
                note=Markup(
                    '<p>Creado desde la solicitud %s (%s).</p>'
                    '<p>Revisa medidas, categoría y etiquetas (serie y configuración). '
                    'Al validarlo activa "Se puede vender" y "Se puede comprar".</p>'
                ) % (record_link('project.task', task.id, task.name), self.env.user.name),
                user_id=config['validator'].id,
            )
            created_by_sku[row.sku] = template
        return created_by_sku

    def _build_detail_html(self, rows, created_by_sku):
        # Detalle en HTML (descripción vigente y registro original en el chatter).
        # Encabezados con ancho relativo (%); el SKU no se parte en dos renglones
        order = self.sale_order_id
        html_rows = []
        for row in rows:
            if row.is_placeholder:
                cells = [row.model_id.name, row.width, row.height,
                         row.color_id.name, OPENING_LABELS.get(row.opening, '')]
            else:
                # SKU especial existente: no se descompone, el nombre ya trae todo
                cells = ['—'] * 5
            created = created_by_sku.get(row.sku)
            if created:
                sku_name = created.name
                status_label = "No (creado, por validar)"
            else:
                # Si el SKU ya existe se muestra el nombre real del producto encontrado
                sku_name = row.existing_product_id.name if row.existing_product_id else row.sku_name
                status_label = STATUS_LABELS.get(row.existing_status, '')
            cells += [format_qty(row.quantity), row.sku, sku_name, status_label]
            html_rows.append(Markup('<tr>%s</tr>') % Markup('').join(
                (Markup('<td class="text-nowrap">%s</td>') if index == SKU_COLUMN_INDEX
                 else Markup('<td>%s</td>')) % (cell if cell not in (False, None) else '')
                for index, cell in enumerate(cells)))

        detail_html = Markup(
            '<p><b>Solicitud de puerta de medida especial</b></p>'
            '<p>Orden de venta: <b>%s</b> · Cliente: %s · Solicitó: %s</p>'
            '<table class="table table-sm table-bordered">'
            '<thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
        ) % (
            order.name,
            order.partner_id.display_name,
            self.env.user.name,
            Markup('').join(Markup('<th width="%s%%">%s</th>') % (width, title)
                            for title, width in DETAIL_HEADER),
            Markup('').join(html_rows),
        )
        if created_by_sku:
            detail_html += Markup('<p><b>SKUs creados, por validar:</b> %s</p>') % Markup(', ').join(
                record_link('product.template', template.id, sku) for sku, template in created_by_sku.items())
        if self.notes:
            detail_html += Markup('<p><b>Observaciones:</b> %s</p>') % Markup('<br/>').join(
                self.notes.splitlines())
        return detail_html


class SpecialDoorRequestWizardLine(models.TransientModel):
    _name = 'special.door.request.wizard.line'
    _description = "Puerta de la solicitud de medida especial"

    wizard_id = fields.Many2one(
        'special.door.request.wizard', required=True, ondelete='cascade')
    sale_line_id = fields.Many2one('sale.order.line', string="Línea de venta", readonly=True)
    product_id = fields.Many2one('product.product', string="Producto en OV", readonly=True)
    is_placeholder = fields.Boolean(readonly=True)
    include = fields.Boolean(string="Incluir", default=True)
    model_id = fields.Many2one('special.door.model', string="Modelo")
    width = fields.Integer(string="Ancho")
    height = fields.Integer(string="Alto")
    color_id = fields.Many2one('special.door.color', string="Color")
    opening = fields.Selection(OPENING_SELECTION, string="Apertura")
    quantity = fields.Float(string="Cantidad", digits='Product Unit of Measure')
    sku = fields.Char(string="SKU", compute='_compute_sku')
    sku_name = fields.Char(string="Nombre del SKU", compute='_compute_sku')
    existing_product_id = fields.Many2one(
        'product.product', string="Producto existente", compute='_compute_existing_product_id')
    existing_status = fields.Selection(
        [('new', "No existe"), ('active', "Ya existe"), ('pending', "Por validar"),
         ('archived', "Existe archivado")],
        string="¿Ya existe?",
        compute='_compute_existing_status',
    )

    @api.model
    def _is_placeholder_product(self, product):
        # Placeholder = servicio; SKU especial = almacenable
        return product.type == 'service'

    @api.model
    def _get_placeholder_openings(self):
        # {product.product id: apertura} de los placeholders del módulo
        openings = {}
        for xmlid, opening in PLACEHOLDER_OPENINGS.items():
            template = self.env.ref('%s.%s' % (MODULE, xmlid), raise_if_not_found=False)
            if template:
                openings[template.product_variant_id.id] = opening
        return openings

    @api.depends('is_placeholder', 'model_id.code', 'model_id.name', 'width', 'height',
                 'color_id.code', 'color_id.name', 'opening', 'product_id.default_code', 'product_id.name')
    def _compute_sku(self):
        # Placeholder: modelo + ancho + alto + color + apertura. SKU existente: el del producto
        for row in self:
            sku = sku_name = False
            if row.is_placeholder:
                if row.model_id and row.color_id and row.opening and row.width and row.height:
                    sku = ('%s%s%s%s%s' % (
                        row.model_id.code, row.width, row.height, row.color_id.code, row.opening)).upper()
                    sku_name = ('PTA MEDIDA ESPECIAL %s %s-%s %s %s' % (
                        row.model_id.name, row.width, row.height, row.color_id.name,
                        OPENING_NAME_SUFFIX[row.opening])).upper()
            elif row.product_id:
                sku = row.product_id.default_code
                sku_name = row.product_id.name
            row.sku = sku
            row.sku_name = sku_name

    @api.depends('sku')
    def _compute_existing_product_id(self):
        # Busca el SKU en el código interno, incluyendo archivados; prioriza el activo
        # (hay códigos duplicados en productos archivados)
        Product = self.env['product.product'].with_context(active_test=False)
        for row in self:
            found = Product
            if row.sku:
                found = Product.search([('default_code', '=', row.sku)], order='active desc, id desc', limit=1)
            row.existing_product_id = found

    @api.depends('sku', 'existing_product_id.active', 'existing_product_id.sale_ok',
                 'existing_product_id.purchase_ok', 'existing_product_id.is_special_door')
    def _compute_existing_status(self):
        # "Por validar": SKU especial creado por una solicitud y aún sin venta ni compra activas
        for row in self:
            status = False
            product = row.existing_product_id
            if row.sku:
                if not product:
                    status = 'new'
                elif not product.active:
                    status = 'archived'
                elif product.is_special_door and not product.sale_ok and not product.purchase_ok:
                    status = 'pending'
                else:
                    status = 'active'
            row.existing_status = status

    def _get_configuration_tag(self, config):
        # Sencilla / Con fijo / Doble según el ancho; el modelo puede ampliar el máximo de sencilla
        self.ensure_one()
        single_max = self.model_id.single_max_width or DEFAULT_SINGLE_MAX_WIDTH
        if self.width <= single_max:
            return config['tag_single']
        if self.width < DOUBLE_MIN_WIDTH:
            return config['tag_fixed']
        return config['tag_double']
