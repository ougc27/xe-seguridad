# -*- coding: utf-8 -*-
# Copyright 2024 Morwi Encoders Consulting SA de CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

{
    "name": "XE Theme",
    'category': 'Hidden',
    'version': '1.0',
    'license': 'LGPL-3',
    'description': "XE JS and CSS files for Website",
    'summary': "XE JS and CSS files for Website",
    "depends": [
        "web",
    ],
    'assets': {
        'web.assets_frontend': [
            'xe_theme/static/src/css/*.css',
            'xe_theme/static/src/js/*.js',
            'xe_theme/static/src/scss/*.scss',
        ],
    },
}
