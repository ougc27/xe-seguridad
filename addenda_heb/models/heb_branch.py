from odoo import models, fields


class HebBranch(models.Model):
    _name = "heb.branch"
    _description = "HEB Branch Catalog"
    _order = "name asc"

    name = fields.Char(
        string="Branch Name",
        required=True,
    )
    gln = fields.Char(
        string="GLN",
        required=True,
        help="Global Location Number",
    )

    _sql_constraints = [
        ("gln_unique", "UNIQUE(gln)", "The GLN must be unique per branch."),
    ]
