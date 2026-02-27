import json
from odoo import models, api, fields
from odoo.tools import html2plaintext


class ProjectTask(models.Model):
    _inherit = 'project.task'

    # ============================================================
    # MAIN ENTRY
    # ============================================================
    def action_process_coupon_task(self):
        for rec in self:
            try:
                rec.sudo().write({'state': '02_changes_requested'})
                action, company, program = rec._get_company_and_program()
                data = rec._parse_json_description()

                if action == 'CreateCoupons':
                    rec._process_create_coupons(program, data)

                elif action == 'DeleteCoupons':
                    rec._process_delete_coupons(program, data)

                else:
                    rec._log_warning(
                        name='Unsupported Coupon Action',
                        message=f"Acción no soportada detectada: {action}",
                        func='action_process_coupon_task'
                    )
                    continue

            except Exception as e:
                rec._log_error(
                    name='Coupon Task Fatal Error',
                    message=f"Error general procesando tarea ID {rec.id}: {str(e)}",
                    func='action_process_coupon_task'
                )

    # ============================================================
    # STEP 1 - COMPANY + PROGRAM
    # ============================================================
    def _get_company_and_program(self):
        self.ensure_one()

        try:
            parts = (self.name or '').split('|')

            if len(parts) != 2:
                self._log_warning(
                    name='Invalid Task Title Format',
                    message=f"Título inválido: {self.name}",
                    func='_get_company_and_program'
                )
                return None, None, None

            action = parts[0].strip()
            company_id_str = parts[1].strip()

            if not company_id_str.isdigit():
                self._log_warning(
                    name='Invalid Company ID',
                    message=f"Company ID inválido en título: {self.name}",
                    func='_get_company_and_program'
                )
                return action, None, None

            company = self.env['res.company'].browse(int(company_id_str))

            if not company.exists():
                self._log_warning(
                    name='Company Not Found',
                    message=f"No se encontró compañía con ID: {company_id_str}",
                    func='_get_company_and_program'
                )
                return action, None, None

            program = self.env['loyalty.program'].search([
                ('company_id', '=', company.id),
                ('externally_managed', '=', True),
            ], limit=1)

            if not program:
                self._log_warning(
                    name='Program Not Found',
                    message=f"No se encontró programa externally_managed para compañía ID {company_id_str}",
                    func='_get_company_and_program'
                )
                return action, company, None

            return action, company, program

        except Exception as e:
            self._log_error(
                name='Error Resolving Company/Program',
                message=str(e),
                func='_get_company_and_program'
            )
            return None, None, None

    # ============================================================
    # STEP 2 - JSON PARSE
    # ============================================================
    def _parse_json_description(self):
        self.ensure_one()

        if not self.description:
            return []

        try:
            clean_text = html2plaintext(self.description)
            clean_text = clean_text.encode('utf-8', 'ignore').decode('utf-8')
            clean_text = clean_text.replace('\u00a0', '')
            clean_text = clean_text.replace('\xa0', '')
            clean_text = clean_text.lstrip()
            first_bracket = clean_text.find('[')
            if first_bracket != -1:
                clean_text = clean_text[first_bracket:]
            data = json.loads(clean_text)
            if isinstance(data, list):
                return data

            return []

        except Exception as e:
            self._log_error(
                name='JSON Parse Error',
                message=f"{str(e)} | RAW START: {clean_text[:50]}",
                func='_parse_json_description'
            )
            return []

    # ============================================================
    # CREATE LOGIC
    # ============================================================
    def _process_create_coupons(self, program, data):

        if not program:
            return

        try:
            warehouse_names = list(set([d.get('warehouse_id') for d in data if d.get('warehouse_id')]))
            product_codes = list(set([d.get('product_id') for d in data if d.get('product_id')]))

            warehouses = self.env['stock.warehouse'].search([
                ('name', 'in', warehouse_names)
            ])
            products = self.env['product.product'].search([
                ('default_code', 'in', product_codes)
            ])

            warehouse_map = {w.name: w for w in warehouses}
            product_map = {p.default_code: p for p in products}

            LoyaltyCard = self.env['loyalty.card']

            created_count = 0

            for item in data:
                try:
                    warehouse = warehouse_map.get(item.get('warehouse_id'))
                    product = product_map.get(item.get('product_id'))
                    damage_type = item.get('damage_type')

                    if not warehouse:
                        self._log_warning(
                            name='Warehouse Missing',
                            message=f"Warehouse no encontrado: {item.get('warehouse_id')}",
                            func='_process_create_coupons'
                        )
                        continue

                    if not product:
                        self._log_warning(
                            name='Product Missing',
                            message=f"Producto no encontrado: {item.get('product_id')}",
                            func='_process_create_coupons'
                        )
                        continue

                    product_pricelist = None
                    if damage_type == 'damage_2':
                        pricelist_name = 'Outlet (Frontera)'
                        tax_id = self.env['pos.config'].sudo().search([
                            ('picking_type_id.warehouse_id', '=', warehouse.id)
                        ], limit=1).tax_id
                        if int(tax_id.amount) == 16:
                            pricelist_name = 'Outlet'
                        product_pricelist = self.env['product.pricelist'].sudo().search([
                            ('name', '=', pricelist_name)
                        ], limit=1).id

                    LoyaltyCard.create({
                        'program_id': program.id,
                        'product_id': product.id,
                        'warehouse_id': warehouse.id,
                        'damage_type': damage_type,
                        'pricelist_id': product_pricelist,
                        'partner_id': False,
                        'points': 1.0,
                    })

                    created_count += 1

                except Exception as line_error:
                    self._log_error(
                        name='Coupon Creation Line Error',
                        message=str(line_error),
                        func='_process_create_coupons'
                    )

            self._log_info(
                name='Coupons Created Successfully',
                message=f"Se crearon {created_count} cupones para tarea {self.id}",
                func='_process_create_coupons'
            )

            if created_count:
                self.sudo().write({'state': '1_done'})

        except Exception as e:
            self._log_error(
                name='Coupon Creation Fatal Error',
                message=str(e),
                func='_process_create_coupons'
            )

    # ============================================================
    # DELETE LOGIC
    # ============================================================
    def _process_delete_coupons(self, program, data):

        if not program:
            return

        try:
            warehouse_names = list(set([d.get('warehouse_id') for d in data if d.get('warehouse_id')]))
            product_codes = list(set([d.get('product_id') for d in data if d.get('product_id')]))

            warehouses = self.env['stock.warehouse'].search([
                ('name', 'in', warehouse_names)
            ])
            products = self.env['product.product'].search([
                ('default_code', 'in', product_codes)
            ])

            warehouse_map = {w.name: w for w in warehouses}
            product_map = {p.default_code: p for p in products}

            LoyaltyCard = self.env['loyalty.card']

            deleted_count = 0

            for item in data:
                try:
                    warehouse = warehouse_map.get(item.get('warehouse_id'))
                    product = product_map.get(item.get('product_id'))
                    damage_type = item.get('damage_type')

                    if not warehouse or not product:
                        continue
                
                    self.env.cr.execute("""
                        DELETE FROM loyalty_card
                        WHERE id = (
                            SELECT id
                            FROM loyalty_card
                            WHERE program_id = %s
                            AND product_id = %s
                            AND warehouse_id = %s
                            AND damage_type = %s
                            AND partner_id IS NULL
                            LIMIT 1
                        )
                    """, (
                        program.id,
                        product.id,
                        warehouse.id,
                        damage_type,
                    ))

                    deleted_count += self.env.cr.rowcount

                except Exception as line_error:
                    self._log_error(
                        name='Coupon Delete Line Error',
                        message=str(line_error),
                        func='_process_delete_coupons'
                    )

            self._log_info(
                name='Coupons Deleted Successfully',
                message=f"Se eliminaron {deleted_count} cupones en tarea {self.id}",
                func='_process_delete_coupons'
            )

            if deleted_count:
                self.sudo().write({'state': '1_done'})

        except Exception as e:
            self._log_error(
                name='Coupon Delete Fatal Error',
                message=str(e),
                func='_process_delete_coupons'
            )

    # ============================================================
    # LOG HELPERS
    # ============================================================
    def _log_info(self, name, message, func):
        self.env['ir.logging'].sudo().create({
            'name': name,
            'type': 'server',
            'dbname': self.env.cr.dbname,
            'level': 'INFO',
            'message': message,
            'path': 'project.task',
            'func': func,
            'line': '0',
        })

    def _log_warning(self, name, message, func):
        self.env['ir.logging'].sudo().create({
            'name': name,
            'type': 'server',
            'dbname': self.env.cr.dbname,
            'level': 'WARNING',
            'message': message,
            'path': 'project.task',
            'func': func,
            'line': '0',
        })

    def _log_error(self, name, message, func):
        self.env['ir.logging'].sudo().create({
            'name': name,
            'type': 'server',
            'dbname': self.env.cr.dbname,
            'level': 'ERROR',
            'message': message,
            'path': 'project.task',
            'func': func,
            'line': '0',
        })
