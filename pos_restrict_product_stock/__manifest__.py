# -*- coding: utf-8 -*-
#############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2023-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author:Anjhana A K(<https://www.cybrosys.com>)
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
{
    'name': 'Display Stock in POS | Restrict Out-of-Stock Products in POS',
    'version': '17.0.1.0.16',
    'category': 'Point of Sale',
    'summary': """Enhance your Point of Sale experience by preventing the 
    ordering of out-of-stock products during your session""",
    'description': """This module enables you to limit the ordering of 
     out-of-stock products in POS as well as display the available quantity for
      each product (on-hand quantity and virtual quantity).""",
    'author': 'Cybrosys Techno Solutions',
    'company': 'Cybrosys Techno Solutions',
    'maintainer': 'Cybrosys Techno Solutions',
    'website': 'https://www.cybrosys.com',
    #'depends': ['point_of_sale', 'sale'],
    'depends': ['point_of_sale', 'sale', 'base_vat', 'base'],
    'data': [
        'views/res_config_settings_views.xml',
        'views/pos_ticket_view.xml',
        'views/pos_order_views.xml',
        'security/point_of_sale_security.xml'
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            '/pos_restrict_product_stock/static/src/js/RestrictStockPopup.js',
            '/pos_restrict_product_stock/static/src/js/order.js',
            '/pos_restrict_product_stock/static/src/js/ProductScreen.js',
            '/pos_restrict_product_stock/static/src/js/ProductItem.js',
            '/pos_restrict_product_stock/static/src/js/PaymentScreen.js',
            '/pos_restrict_product_stock/static/src/js/CashOpeningPopup.js',
            '/pos_restrict_product_stock/static/src/js/SetSaleOrdenButton.js',
            '/pos_restrict_product_stock/static/src/js/RefundButton.js',
            '/pos_restrict_product_stock/static/src/css/display_stock.css',
            '/pos_restrict_product_stock/static/src/css/payment_screens.css',
            '/pos_restrict_product_stock/static/src/xml/ProductItem.xml',
            '/pos_restrict_product_stock/static/src/xml/RestrictStockPopup.xml',
            '/pos_restrict_product_stock/static/src/xml/ProductInfoPopup.xml',
            '/pos_restrict_product_stock/static/src/xml/OrderReceipt.xml',
            '/pos_restrict_product_stock/static/src/xml/PaymentScreen.xml',
            '/pos_restrict_product_stock/static/src/xml/ActionPad.xml',
            '/pos_restrict_product_stock/static/src/xml/ReceiptHeader.xml',
            '/pos_restrict_product_stock/static/src/xml/SetSaleOrdenButton.xml',
            '/pos_restrict_product_stock/static/src/xml/RefundButton.xml'
            #cash opening no se necesita
        ],
    },
    #'images': ['static/description/banner.jpg'],
    'license': 'AGPL-3',
    'installable': True,
    'auto_install': True,
    'application': False,
}
