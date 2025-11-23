# -*- coding: utf-8 -*-
{
    'name': 'XE Auto Logout',
    'version': '17.0.0.0.1',
    'category': 'Extra Tools',
    'summary': """Auto logout idle user with fixed time on sundays""",
    'description': """If the user
     is in idle mode the user will logout from session automatically """,
    "license": "LGPL-3",
    "author": "XE Brands",
    'depends': ['web'],
    'data': [],
    'assets': {
        'web.assets_backend': [
            'xe_auto_logout/static/src/xml/auto_logout_biweekly.xml',
            'xe_auto_logout/static/src/js/auto_logout_biweekly.js',
        ],
    },
    'installable': True,
    'application': False,
}
