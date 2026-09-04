{
    "name": "XE - CFDI emitido por tercero",
    "version": "17.0.1.0.0",
    "summary": "Impide el timbrado desde Odoo para contactos cuyo CFDI emite un tercero",
    "description": """
Bloqueo de timbrado para CFDI emitidos por terceros
====================================================

Algunos marketplaces (Mercado Libre, entre otros) emiten el CFDI de la venta
por cuenta del vendedor. En esos casos Odoo NO debe timbrar, porque generaria
un CFDI duplicado ante el SAT por la misma operacion.

Este modulo agrega una bandera por contacto y aplica dos niveles de bloqueo:

1. La factura deja de declararse como "necesita CFDI", por lo que la casilla
   de CFDI desaparece del asistente de Enviar e imprimir y los procesos
   automaticos no la consideran.
2. Un guard duro en el metodo de timbrado que lanza UserError ante cualquier
   llamada directa (RPC, accion de servidor, boton personalizado).

No modifica ningun campo almacenado ni calculado del estandar, por lo que su
instalacion no dispara recalculos ni altera documentos historicos.
""",
    "author": "XE Brands",
    "website": "https://www.xeseguridad.com",
    "license": "LGPL-3",
    "category": "Accounting/Localizations/EDI",
    "depends": ["l10n_mx_edi"],
    "data": [
        "views/res_partner_views.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "auto_install": False,
}
