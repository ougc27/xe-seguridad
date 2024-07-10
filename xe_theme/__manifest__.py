# -*- coding: utf-8 -*-
# Copyright 2024 Morwi Encoders Consulting SA de CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

{
    "name": "XE JS and CSS files for Website",
    'category': 'Hidden',
    'version': '1.0',
    'license': 'LGPL-3',
    'description': "XE JS and CSS files for Website",
    'summary': "XE JS and CSS files for Website",
    "depends": [
        "web",
    ],
    # Add js css and css files next
    
    'assets': {
        'web.assets_frontend': [
            'aguillon_customs/static/src/css/*.css',
            'aguillon_customs/static/src/js/*.js',
            'aguillon_customs/static/src/scss/*.scss',
        ],
    },
}
