from odoo import tools, api, SUPERUSER_ID


def calculate_door_count(env):
    """Calculate door count in ROMAX and XE Brands"""
    pickings = env['stock.picking'].sudo().search([
        ('state', 'not in', ['done', 'cancel']),
        ('picking_type_code', '=', 'outgoing'),
        ('company_id', 'in', [1, 3])
    ])

    for record in pickings:
        door_count = 0
        for line in record.move_ids_without_package:
            if 'Puertas / Puertas' in line.product_id.categ_id.complete_name:
                door_count += line.product_uom_qty
            record.write({'door_count': door_count})


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    calculate_door_count(env)
