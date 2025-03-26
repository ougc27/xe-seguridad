# -*- coding: utf-8 -*-
# Part of Odoo Module Developed by Candidroot Solutions Pvt. Ltd.
# See LICENSE file for full copyright and licensing details.
{
    'name': "Readonly Group Tool",
    'category': 'Web',
    'summary': """Readonly Group Tool.""",
    'version': '17.0.0.1',
    'author': "Candidroot Solutions Pvt. Ltd.",
    'website': 'https://www.candidroot.com/',
    'sequence': 2,
    'description': """This module allows you to make fields readonly base on groups you set.""",
    'depends': ['web'],
    'data': [
    ],
    'assets': {
        'web.assets_backend': [
            'readonly_group_tool_cr/static/src/js/form_group.js',
        ],
    },
    'qweb': [],
    'images' : ['static/description/banner.png'],
    'installable': True,
    'live_test_url': 'https://www.youtube.com/watch?v=N-4mHlvkCSc',
    'price': 15,
    'currency': 'USD',
    'auto_install': False,
    'application': True,
}
