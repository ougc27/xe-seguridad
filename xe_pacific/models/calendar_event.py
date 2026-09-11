# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models


class CalendarEvent(models.Model):
    _inherit = 'calendar.event'

    def _sync_tags_from_opportunity(self):
        """Mirror each event's opportunity tag_ids onto categ_ids, matching
        calendar.event.type by name and auto-creating it if missing.

        Categories are added, never removed, so any category set manually
        on the event survives the sync.
        """
        CalendarEventType = self.env['calendar.event.type']
        for event in self:
            tags = event.opportunity_id.tag_ids
            if not tags:
                continue
            categs = CalendarEventType
            for tag in tags:
                categ = CalendarEventType.search([('name', '=', tag.name)], limit=1)
                categs |= categ if categ else CalendarEventType.create({'name': tag.name})
            event.categ_ids = [(4, categ.id) for categ in categs]

    def create(self, vals_list):
        events = super().create(vals_list)
        events.filtered('opportunity_id')._sync_tags_from_opportunity()
        return events

    def write(self, vals):
        res = super().write(vals)
        if 'opportunity_id' in vals:
            self.filtered('opportunity_id')._sync_tags_from_opportunity()
        return res
