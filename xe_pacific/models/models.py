from odoo.exceptions import AccessError
from odoo import models, _
from markupsafe import Markup


class BaseModel(models.AbstractModel):
    _inherit = 'base'

    def _get_html_link(self, title=None):
        """Generate the record html reference for chatter use.

        :param str title: optional reference title, the record display_name
            is used if not provided. The title/display_name will be escaped.
        :returns: generated html reference,
            in the format <a href data-oe-model="..." data-oe-id="...">title</a>
        :rtype: str
        """
        self.ensure_one()
        model_name = self.env.context.get("model_name", self._name)
        record_id = self.env.context.get("record_id", self.id)
        return Markup("<a href=# data-oe-model='%s' data-oe-id='%s'>%s</a>") % (
            model_name, record_id, title or self.display_name)

    def toggle_active(self):
        exempt_models = self.env["archive.exception.models"]._get_exempt_model_names()
        if self._name not in exempt_models:
            if not self.env.user.has_group("xe_pacific.group_global_archive"):
                raise AccessError(_("You are not allowed to archive/unarchive records of model '%s'.") % self._name)
        return super().toggle_active()

    # def unlink(self):
    #     if not self.env.user.has_group("xe_pacific.group_global_unlink"):
    #         raise AccessError(
    #             _("You are not allowed to delete records.")
    #         )
    #     return super().unlink()
