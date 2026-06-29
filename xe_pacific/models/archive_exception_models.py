# -*- coding: utf-8 -*-

from odoo import api, fields, models, tools


class ArchiveExceptionModels(models.Model):
    """
    Whitelist of technical models that are EXEMPT from the
    'xe_pacific.group_global_archive' restriction enforced in
    `BaseModel.toggle_active()` (see models/models.py).

    Any model registered here (and active) can be archived / unarchived by
    ANY user, regardless of whether they belong to the restricted group.

    Managed entirely from the UI: add or remove models on demand, no code
    changes or server restarts required.
    """

    _name = "archive.exception.models"
    _description = "Archive Restriction Exception Models"
    _order = "model_name asc"
    _rec_name = "model_name"

    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        index=True,
        domain=[("transient", "=", False)],
        help="Technical model allowed to bypass the archive/unarchive "
        "permission check.",
    )
    model_name = fields.Char(
        string="Technical Name",
        related="model_id.model",
        store=True,
        index=True,
        readonly=True,
        help="Stored copy of the model's technical name, used for fast "
        "lookups from toggle_active().",
    )
    note = fields.Char(
        string="Reason / Note",
        help="Optional explanation of why this model does not require the "
        "archive permission.",
    )
    active = fields.Boolean(
        string="Active",
        default=True,
        help="Uncheck to re-enable the restriction for this model without "
        "deleting the configuration record.",
    )

    _sql_constraints = [
        (
            "unique_model_id",
            "UNIQUE(model_id)",
            "This model is already registered as an archive exception.",
        ),
    ]

    @api.model
    @tools.ormcache()
    def _get_exempt_model_names(self):
        """
        Return a frozenset of technical model names currently exempt from
        the archive/unarchive permission check.

        Cached with ormcache for O(1) lookups on every toggle_active() call.
        The cache is automatically invalidated by create/write/unlink below.
        """
        rows = self.sudo().search_read(
            domain=[("active", "=", True)],
            fields=["model_name"],
        )
        return frozenset(row["model_name"] for row in rows if row["model_name"])

    def _invalidate_exempt_cache(self):
        self.clear_caches()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._invalidate_exempt_cache()
        return records

    def write(self, vals):
        result = super().write(vals)
        self._invalidate_exempt_cache()
        return result

    def unlink(self):
        result = super().unlink()
        self._invalidate_exempt_cache()
        return result
