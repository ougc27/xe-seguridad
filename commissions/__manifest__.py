# -*- coding: utf-8 -*-
{
    'name': 'Commissions',
    'author': 'Xe Brands',
    'version': '17.20250125',
    'summary': 'Commissions',
    'description': 'Commissions',
    'category': 'Sales/Sales',
    'depends': [
        'sale_management',
        'hr',
        'contacts',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/hr_views.xml',
        'views/xe_agent_views.xml',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
        'views/account_move_views.xml',
        'views/account_views.xml',
        'views/xe_commissions_views.xml',
        'views/xe_payment_commissions_views.xml',
        'views/xe_mass_payment_commissions_views.xml',
        'report/xe_payment_commissions_report.xml',
        'wizard/commission_recalculation_views.xml',
    ],
    'license': 'LGPL-3',
}
