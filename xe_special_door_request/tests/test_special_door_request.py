from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import TransactionCase, new_test_user, tagged

MODULE = 'xe_special_door_request'


@tagged('post_install', '-at_install')
class TestSpecialDoorRequest(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # En la copia de producción la empresa por defecto del usuario técnico puede no tener
        # almacén para la OV: se trabaja en la empresa de un almacén existente
        Warehouse = cls.env['stock.warehouse']
        cls.warehouse = Warehouse.search([('company_id', '=', cls.env.company.id)], limit=1) \
            or Warehouse.search([], limit=1)
        cls.company = cls.warehouse.company_id
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))

        cls.Task = cls.env['project.task']
        cls.project = cls.env.ref('%s.project_special_door' % MODULE)
        cls.placeholder_right = cls.env.ref('%s.product_placeholder_right' % MODULE).product_variant_id
        cls.placeholder_left = cls.env.ref('%s.product_placeholder_left' % MODULE).product_variant_id

        # Las pruebas también corren sobre la copia de producción (staging): se usan impuestos
        # existentes y solo se crean si la base no tiene (base limpia de desarrollo)
        Tax = cls.env['account.tax']
        company_domain = [('company_id', '=', cls.company.id)]
        cls.sale_tax = sale_tax = Tax.search(company_domain + [('type_tax_use', '=', 'sale')], limit=1) or Tax.create({
            'name': 'IVA venta prueba', 'amount': 16, 'type_tax_use': 'sale'})
        purchase_tax = Tax.search(company_domain + [('type_tax_use', '=', 'purchase')], limit=1) or Tax.create({
            'name': 'IVA compra prueba', 'amount': 16, 'type_tax_use': 'purchase'})
        (cls.placeholder_right | cls.placeholder_left).product_tmpl_id.write({
            'taxes_id': [Command.set(sale_tax.ids)],
            'supplier_taxes_id': [Command.set(purchase_tax.ids)],
        })

        cls.category = cls.env['product.category'].create({'name': 'Puertas especiales prueba'})
        Tag = cls.env['product.tag']
        cls.tag_series = Tag.create({'name': 'Serie prueba'})
        cls.tag_single = Tag.create({'name': 'Sencilla prueba'})
        cls.tag_fixed = Tag.create({'name': 'Con fijo prueba'})
        cls.tag_double = Tag.create({'name': 'Doble prueba'})
        cls.sale_tag = cls.env['crm.tag'].create({'name': 'PuertaEspecial prueba'})
        cls.validator = new_test_user(cls.env, login='special_door_validator', groups='base.group_user')

        # Parámetros escritos directo: execute() de Ajustes corre los ajustes de todos los módulos
        ICP = cls.env['ir.config_parameter'].sudo()
        for param, record in (
            ('sku_validator_user_id', cls.validator),
            ('tag_single_id', cls.tag_single),
            ('tag_fixed_id', cls.tag_fixed),
            ('tag_double_id', cls.tag_double),
            ('sale_tag_id', cls.sale_tag),
        ):
            ICP.set_param('%s.%s' % (MODULE, param), record.id)

        # Códigos que no existen en los catálogos reales (son únicos)
        cls.door_model = cls.env['special.door.model'].create({
            'name': 'Modelo prueba', 'code': 'qzt', 'categ_id': cls.category.id,
            'product_tag_ids': [Command.set(cls.tag_series.ids)], 'single_max_width': 120,
        })
        cls.color = cls.env['special.door.color'].create({'name': 'Color prueba', 'code': 'qz'})
        cls.partner = cls.env['res.partner'].create({'name': 'Cliente puertas'})

    def _create_order(self, products=None, user=None):
        # El almacén se indica al crear: cambiar el vendedor después lo recalcula
        products = products or self.placeholder_right
        return self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'user_id': (user or self.env.user).id,
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'order_line': [Command.create({'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 100.0})
                           for product in products],
        })

    def _open_wizard(self, order):
        action = order.action_open_special_door_request()
        return self.env['special.door.request.wizard'].browse(action['res_id'])

    def _fill_row(self, row, width=90, height=210):
        row.write({'model_id': self.door_model.id, 'color_id': self.color.id, 'width': width, 'height': height})

    def _order_tasks(self, order):
        return self.Task.search([('project_id', '=', self.project.id), ('sale_order_id', '=', order.id)])

    def test_sku_and_name(self):
        order = self._create_order(self.placeholder_right | self.placeholder_left)
        wizard = self._open_wizard(order)
        right, left = wizard.line_ids
        self.assertEqual((right.opening, left.opening), ('DE', 'IZ'))
        self.assertFalse(right.sku, "Sin modelo, medidas ni color no hay SKU")
        self._fill_row(right)
        self.assertEqual(right.sku, 'QZT90210QZDE')
        self.assertEqual(right.sku_name, 'PTA MEDIDA ESPECIAL MODELO PRUEBA 90-210 COLOR PRUEBA DER')
        self.assertEqual(right.existing_status, 'new')

    def test_existing_status(self):
        Product = self.env['product.product']
        order = self._create_order()
        row = self._open_wizard(order).line_ids
        self._fill_row(row)
        product = Product.create({'name': 'Existente', 'default_code': 'QZT90210QZDE', 'type': 'product'})
        row.invalidate_recordset()
        self.assertEqual(row.existing_status, 'active')
        product.write({'is_special_door': True, 'sale_ok': False, 'purchase_ok': False})
        row.invalidate_recordset()
        self.assertEqual(row.existing_status, 'pending')
        product.active = False
        row.invalidate_recordset()
        self.assertEqual(row.existing_status, 'archived')

    def test_configuration_tag(self):
        row = self._open_wizard(self._create_order()).line_ids
        config = {'tag_single': self.tag_single, 'tag_fixed': self.tag_fixed, 'tag_double': self.tag_double}
        for width, tag in ((120, self.tag_single), (121, self.tag_fixed), (159, self.tag_fixed),
                           (160, self.tag_double)):
            self._fill_row(row, width=width)
            self.assertEqual(row._get_configuration_tag(config), tag, "Ancho %s" % width)

    def test_blocking_errors(self):
        wizard = self._open_wizard(self._create_order())
        with self.assertRaises(UserError):
            wizard.action_submit()
        self._fill_row(wizard.line_ids, width=50, height=320)
        with self.assertRaisesRegex(UserError, 'ancho'):
            wizard.action_submit()
        wizard.line_ids.include = False
        with self.assertRaisesRegex(UserError, 'al menos una puerta'):
            wizard.action_submit()
        self.assertFalse(self._order_tasks(wizard.sale_order_id))

    def test_create_request(self):
        order = self._create_order()
        self.assertTrue(order.show_special_door_button)
        wizard = self._open_wizard(order)
        self._fill_row(wizard.line_ids)
        wizard.notes = 'Urgente <b>'
        result = wizard.action_submit()
        self.assertEqual(result['type'], 'ir.actions.act_window_close')

        task = self._order_tasks(order)
        self.assertEqual(len(task), 1)
        self.assertEqual(task.stage_id, self.env.ref('%s.stage_special_door_validate_measures' % MODULE))
        self.assertEqual(task.state, '01_in_progress')
        self.assertNotIn('»', task.name)
        self.assertIn('QZT90210QZDE', task.description)
        self.assertIn('Urgente &lt;b&gt;', task.description)
        self.assertIn(self.sale_tag, order.tag_ids)
        order.invalidate_recordset(['show_special_door_button'])
        self.assertFalse(order.show_special_door_button)

        product = self.env['product.product'].search([('default_code', '=', 'QZT90210QZDE')])
        self.assertEqual(len(product), 1)
        self.assertEqual(product.type, 'product')
        self.assertFalse(product.sale_ok or product.purchase_ok)
        self.assertTrue(product.is_special_door)
        self.assertEqual(product.categ_id, self.category)
        self.assertEqual(product.product_tag_ids, self.tag_series | self.tag_single)
        # El placeholder real tiene impuestos por empresa; basta con que se copie el de la prueba
        self.assertIn(self.sale_tax, product.sudo().taxes_id)
        self.assertEqual(product.product_tmpl_id.activity_ids.user_id, self.validator)

        with self.assertRaisesRegex(UserError, 'ya tiene una solicitud'):
            order.action_open_special_door_request()

    def test_same_measures_do_not_duplicate_sku(self):
        first = self._open_wizard(self._create_order())
        self._fill_row(first.line_ids)
        first.action_submit()

        second = self._open_wizard(self._create_order())
        self._fill_row(second.line_ids)
        self.assertEqual(second.line_ids.existing_status, 'pending')
        second.action_submit()
        self.assertEqual(self.env['product.product'].search_count([('default_code', '=', 'QZT90210QZDE')]), 1)

    def test_warnings_step(self):
        wizard = self._open_wizard(self._create_order())
        self._fill_row(wizard.line_ids, width=200, height=180)
        wizard.action_submit()
        self.assertEqual(wizard.step, 'confirm')
        self.assertIn('mayor que el alto', wizard.warning_message)
        self.assertFalse(self._order_tasks(wizard.sale_order_id))

        wizard.action_back()
        self.assertEqual(wizard.step, 'edit')
        self.assertEqual(wizard.line_ids.width, 200, "Regresar conserva lo capturado")

        wizard.action_submit()
        wizard.action_confirm_warnings()
        self.assertEqual(len(self._order_tasks(wizard.sale_order_id)), 1)

    def test_cancelled_request_allows_new_one(self):
        order = self._create_order()
        wizard = self._open_wizard(order)
        self._fill_row(wizard.line_ids)
        wizard.action_submit()
        self._order_tasks(order).state = '1_canceled'
        order.invalidate_recordset(['show_special_door_button'])
        self.assertTrue(order.show_special_door_button)

    def test_salesman_can_request(self):
        salesman = new_test_user(
            self.env, login='special_door_salesman', groups='sales_team.group_sale_salesman',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        order = self._create_order(user=salesman).with_user(salesman)
        wizard = self._open_wizard(order)
        self._fill_row(wizard.line_ids)
        wizard.action_submit()
        task = self._order_tasks(order)
        self.assertEqual(len(task), 1)
        # Que el vendedor quede como seguidor se valida a mano: en la copia de producción el
        # entorno de prueba no lo refleja (el uso real sí, validado en updates17)

    def test_state_stage_sync(self):
        def stage(key):
            return self.env.ref('%s.stage_special_door_%s' % (MODULE, key))

        def new_task(stage_key):
            return self.Task.create({'name': 'Prueba', 'project_id': self.project.id,
                                     'stage_id': stage(stage_key).id})

        task = new_task('confirm_po')
        task.state = '1_canceled'
        self.assertEqual(task.stage_id, stage('cancelled_no_po'))

        task = new_task('validate_reception')
        task.state = '1_canceled'
        self.assertEqual(task.stage_id, stage('cancelled_with_po'))
        self.assertEqual(task.state, '1_canceled')

        task = new_task('deliver')
        task.stage_id = stage('cancelled_no_po')
        self.assertEqual(task.state, '1_canceled')

        task = new_task('deliver')
        task.stage_id = stage('delivered')
        self.assertEqual(task.state, '1_done')

        # Fuera del proyecto no se toca nada
        other = self.Task.create({'name': 'Otra', 'project_id': self.env['project.project'].create(
            {'name': 'Otro proyecto'}).id})
        other.state = '1_canceled'
        self.assertEqual(other.state, '1_canceled')
