from odoo import http, _
from odoo.http import request
from odoo.addons.web.controllers.binary import Binary as WebBinary
from odoo.exceptions import AccessError
import unicodedata
import json
import logging

_logger = logging.getLogger(__name__)


class BinaryInherit(WebBinary):

    @http.route('/web/binary/upload_attachment', type='http', auth="user")
    def upload_attachment(self, model, id, ufile, callback=None):
        # === Aquí empieza tu copia del método original ===
        files = request.httprequest.files.getlist('ufile')
        Model = request.env['ir.attachment']
        out = """<script language="javascript" type="text/javascript">
                    var win = window.top.window;
                    win.jQuery(win).trigger(%s, %s);
                </script>"""
        args = []
        for ufile in files:

            filename = ufile.filename
            if request.httprequest.user_agent.browser == 'safari':
                filename = unicodedata.normalize('NFD', ufile.filename)

            try:
                attachment = Model.create({
                    'name': filename,
                    'raw': ufile.read(),
                    'res_model': model,
                    'res_id': int(id)
                })
                _logger.info("Llame el post_add_create")
                attachment.sudo()._post_add_create()
            except AccessError:
                args.append({'error': _("You are not allowed to upload an attachment here.")})
            except Exception:
                args.append({'error': _("Something horrible happened")})
                _logger.exception("Fail to upload attachment %s", ufile.filename)
            else:
                args.append({
                    'filename': filename.replace('<', ''),
                    'mimetype': attachment.mimetype,
                    'id': attachment.id,
                    'size': attachment.file_size
                })
        return out % (json.dumps(filename), json.dumps(args)) if callback else json.dumps(args)
        # === Aquí termina tu copia del método original ===
