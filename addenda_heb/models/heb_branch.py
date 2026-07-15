from odoo import api, models, fields


class HebBranch(models.Model):
    _name = "heb.branch"
    _description = "HEB Branch Catalog"
    _order = "name asc"
    _rec_names_search = ["name", "gln"]

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

    @api.depends("name", "gln")
    def _compute_display_name(self):
        for branch in self:
            if branch.gln and branch.name:
                branch.display_name = "%s - %s" % (branch.gln, branch.name)
            else:
                branch.display_name = branch.name or branch.gln or ""
