{
    'name': 'Many2Many Field Attachment Preview',
    'version': '1.0',
    'category': 'Tools',
    'summary': 'Preview attachments for Many2Many fields',
    'author': 'Pysquad Informatics LLP',
    'website': 'https://www.pysquad.com',
    'description': """
        This module adds a preview functionality for all Many2Many fields
        using the 'Many2Many' widget in Odoo 17.
    """,

    'depends': ['base', 'web'],

    'data': [
        #'security/ir.model.access.csv',
    ],
    'assets': {
        'web.assets_backend': [
            'ps_m2m_field_attachment_preview/static/src/js/m2m_field_preview.js',
            'ps_m2m_field_attachment_preview/static/src/xml/m2m_field_preview_template.xml',
        ],
    },
    # Images
    'images': [
        'static/description/banner_img.png',
    ],

    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
