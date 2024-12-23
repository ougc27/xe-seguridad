# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI XE Brands Whatsapp",
    "version": "17.0.0.0.2",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'whatsapp',
    ],
    "category": "Whatsapp extended",
    "summary": "MX-EDI XE Brands",
    "demo": [],
    'data': [
        'views/whatsapp_team_member.xml',
        'views/whatsapp_menus.xml',
        'security/ir_rules.xml',
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'auto_install': False,
}
