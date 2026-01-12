# -*- coding: utf-8 -*-
{
    'name': "Connect Google Spreadsheet",

    'summary': """
        Connect Odoo to Google Spreadsheet
        """,

    'description': """
        By using this module you can connect any of Odoo models to Google Spreadsheet
    """,

    'author': "Tri Nanda",
    'website': "https://www.github.com/trinanda",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/16.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Productivity',
    'version': '17.0.3.6.12',
    'support': 'trinanda357@gmail.com',
    'license': 'OPL-1',
    'price': 272.4,
    'currency': 'USD',
    'images': ['static/description/thumbnail2.png'],

    # any module necessary for this one to work correctly
    'depends': ['base', 'mail'],
    'external_dependencies': {
        'python3': ['google-api-python-client', 'google-auth-httplib2', 'google-auth-oauthlib'],
    },

    # always loaded
    'data': [
        'security/group.xml',
        'security/ir.model.access.csv',
        'views/scope_views.xml',
        'views/credential_views.xml',
        'views/connect_spreadsheet_views.xml',
        'views/menu_views.xml',
        'data/cron.xml',
    ],

    'installable': True,
    'application': True,
}
