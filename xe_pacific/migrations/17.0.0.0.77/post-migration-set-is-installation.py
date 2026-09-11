from odoo import api, SUPERUSER_ID

INSTALLATION_CODES = ('INS10', 'INSBAS')
BATCH_SIZE = 1000


def set_is_installation(env):
    """Backfill is_installation on cancelled.remission records created
    before the field existed, based on the products of the original
    transfer folio.

    Uses a single search with an ORM path domain (resolved as one SQL
    query with joins) instead of looping record by record and filtering
    move_ids in Python, since there are over 14000 cancelled.remission
    records and per-record filtering would trigger a query each.
    """
    domain = [
        ('is_installation', '=', False),
        ('picking_id.move_ids.product_id.default_code', 'in', list(INSTALLATION_CODES)),
    ]
    remission_ids = env['cancelled.remission'].sudo().search(domain).ids

    for i in range(0, len(remission_ids), BATCH_SIZE):
        batch_ids = remission_ids[i:i + BATCH_SIZE]
        env['cancelled.remission'].sudo().browse(batch_ids).write({'is_installation': True})
        env.cr.commit()


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    set_is_installation(env)
