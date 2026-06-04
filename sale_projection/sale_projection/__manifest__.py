# -*- coding: utf-8 -*-
{
    'name': 'Sale Projection',
    'version': '17.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Weekly sales projection matrix with scheduled and executed quantities',
    'description': """
        Sale Projection Module
        ======================
        - Weekly projection board per partner (obra/construction site)
        - Manual projected quantities capture
        - Auto-calculated scheduled quantities from stock.picking (scheduled_date)
        - Auto-calculated executed quantities from stock.picking (remission_date / date_done)
        - Amount calculated from partner pricelist
        - SKU portfolio per partner (many2many)
        - Nightly cron to refresh calculated fields
        - Grid-style board: SKUs as rows, weeks as columns
    """,
    'author': 'Custom Development',
    'website': '',
    'depends': [
        'base',
        'sale_management',
        'stock',
        'product',
    ],
    'data': [
        'security/sale_projection_security.xml',
        'security/ir.model.access.csv',
        'data/sale_projection_cron.xml',
        'views/res_partner_views.xml',
        'views/sale_projection_views.xml',
        'views/sale_projection_line_views.xml',
        'views/sale_projection_board_views.xml',
        'views/menu_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sale_projection/static/src/css/sale_projection.css',
            'sale_projection/static/src/js/sale_projection_board.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
