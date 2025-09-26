# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI XE Brands Whatsapp",
    "version": "17.0.0.0.8",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'whatsapp',
        'crm_livechat'
    ],
    "category": "Whatsapp extended",
    "summary": "MX-EDI XE Brands",
    "demo": [],
    'data': [
        'views/whatsapp_team_member.xml',
        'views/whatsapp_menus.xml',
        'views/res_partner_views.xml',
        'views/discuss_channel_views.xml',
        'security/ir_rules.xml',
        'security/xe_whatsapp_security.xml',
        'security/ir.model.access.csv',
    ],
    'assets': {
        'web.assets_backend': [
            'xe_whatsapp/static/src/js/*.js',
            "xe_whatsapp/static/src/xml/*.xml",
            'xe_whatsapp/static/src/views/**/*',
            'xe_whatsapp/static/src/scss/*.scss',
        ],
    },
    'installable': True,
    'auto_install': False,
}
