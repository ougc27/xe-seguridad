from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Backfill categ_ids on every calendar.event that already has an
    opportunity_id, mirroring the same tag_ids -> categ_ids sync that now
    runs live on calendar.event/crm.lead writes, so historical events end
    up tagged too.
    """
    if not version:
        return
    #env = api.Environment(cr, SUPERUSER_ID, {})
    #env['calendar.event'].sudo().search([('opportunity_id', '!=', False)])._sync_tags_from_opportunity()
