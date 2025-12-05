from odoo import models, tools, _
import base64

class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def convert_attachments_to_snapshot(self, odoo_root):

        with tools.file_open(odoo_root, 'rb') as f:
            contenido_bytes = f.read()

        contenido_b64 = base64.b64encode(contenido_bytes)

        attachment = self.env['ir.attachment'].create({
            'name': 'snapshot.txt',
            'type': 'binary',
            "datas": contenido_b64,
            'description': contenido_b64,
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'text/plain',
        })

        return attachment
