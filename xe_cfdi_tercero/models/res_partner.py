from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Dependiente de compania a proposito: el contacto MERCADO LIBRE es global
    # (company_id = False) y lo comparten XE Brands, Romax y Pacific Rim. Marcar
    # la bandera en XE Brands no debe apagar el timbrado de las otras companias.
    xe_cfdi_issued_by_third_party = fields.Boolean(
        string="CFDI emitido por tercero",
        company_dependent=True,
        tracking=True,
        help="Si esta marcado, Odoo no timbrara las facturas ni las notas de "
        "credito de este cliente. Se usa cuando el CFDI lo emite la plataforma "
        "del marketplace por cuenta de la empresa. El XML se relaciona despues.",
    )
