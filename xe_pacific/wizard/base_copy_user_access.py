from lxml import etree

from odoo import api, fields, models


class WizardBaseCopyUserAccess(models.TransientModel):
    _name = "base.copy_user_access"
    _description = "Wizard Copy User Access"

    user_id = fields.Many2one(string="User", comodel_name="res.users", required=True)

    @api.model
    def fields_view_get(
        self, view_id=None, view_type="form", toolbar=False, submenu=False
    ):
        res = super(WizardBaseCopyUserAccess, self).fields_view_get(
            view_id=view_id, view_type=view_type, toolbar=toolbar, submenu=submenu
        )
        doc = etree.XML(res["arch"])
        for node in doc.xpath("//field[@name='user_id']"):
            active_ids = self._context.get("active_ids")
            domain = "[('id', 'not in', " + str(active_ids) + ")]"
            node.set("domain", domain)
        res["arch"] = etree.tostring(doc)
        return res

    def copy_access_right(self):
        res = []
        self.ensure_one()

        obj_user = self.env["res.users"]

        context = self._context
        record_id = context["active_ids"]

        user = self.user_id

        for group in user.groups_id:
            res.append(group.id)

        for data in record_id:
            user_id = obj_user.sudo().browse(data)
            vals = {
                "groups_id": [(6, 0, res)],
                'company_ids': [(6, 0, user.company_ids.ids)]
            }

            user_id.sudo().write(vals)

        return {"type": "ir.actions.act_window_close"}
