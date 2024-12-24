from odoo import models
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
