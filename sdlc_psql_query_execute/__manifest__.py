{
    'name': 'SDLC PSQL Query Execute',
    'version': '17.0.1.0.0',
    'category': 'Technical',
    'summary': 'Run PSQL queries directly from the Odoo user interface',
    'description': """
        Execute PostgreSQL queries directly within Odoo's interface.
        Features:
        - Execute raw SQL queries from Settings > Technical > PSQL Query
        - View query results in a formatted table
        - Export query results to XLSX
        - Error handling with validation messages
    """,
    'author': 'SDLC Corp',
    'website': 'https://sdlccorp.com',
    'depends': ['mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/psql_query_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sdlc_psql_query_execute/static/src/css/psql_query.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
    "images":["static/description/banner.jpg"],
}
