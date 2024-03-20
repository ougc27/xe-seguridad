# -*- coding: utf-8 -*-
# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

{
    "name": "Stock Picking Cancel/Reverse/Revert Odoo",
    "version": "1.0",
    "author": "BrowseInfo",
    'category': 'Warehouse,Stock',
    "website": "https://www.browseinfo.in",
    'summary': 'Apps for cancel stock picking reverse stock picking cancel done picking reverse workflow delivery order cancel incoming shipment cancel cancel picking order cancel delivery order cancel stock move cancel done picking reverse picking process done delivery',
    "depends": [
        "stock","sale_stock","sale_management","purchase",
    ],
    "demo": [],
    'description': """
    stock picking reverse workflow stock picking cancel delivery order cancel incoming shipment cancel, cancel picking order, cancel delivery order, cancel incoming shipment, cancel order, set to draft picking, cancel done picking, revese picking process, cancel done delivery order.reverse delivery order.

stock picking reverse workflow, stock picking cancel, delivery order cancel, delivering cancel, cancel picking order, cancel delivery order, cancel shipment shipment, cancel order, set to draft picking, cancel done picking, revese picking process, cancel done delivery order. orden de entrega inversa.

sélection de stock reverse workflow, sélection de stock annuler, annulation de commande, annulation de livraison, annulation de commande, annulation de livraison, annulation de livraison, annulation de la commande, annulation de la sélection, annulation de la préparation, annulation du bon de livraison. ordre de livraison inverse.
    cancel stock picking
    `cancel and reset to draft picking
    cancel reset picking
    cancel picking
    cancel delivery order
    cancel incoming shipment
    reverse stock picking
    reverse delivery order
    cancel orders
    Stock picking cancel Odoo apps is used for cancel done/validated and completed picking and set it to draft stage. Its very obvious that people or users sometimes make mistakes when working with system, When you received stock on warehouse from incoming shipment or deliver goods via delivery order and made mistakes on quantity or anything else and validate receipt or delivery then in by default odoo there is no revert option. Our Stock picking cancel Odoo apps sloved that problem and help users to cancel validated/done delivery order as well as receipt/incoming shipment.
    order cancel
    delivery cancel, picking cancel, Reverse order, reverse picking, reverse delivery, reverse shipment

    When cancelling sale order if there are related invoices or delivery orders. 
    It automatically cancels it too. All in Cancel/Reset Orders Sales cancel Purchase cancel Delivery cancel Shipment in Odoo cancel DO
    odoo Sales Order reverse workflow odoo Sales order cancel Sale order cancel Sale cancel 
    odoo cancel Sales order cancel sale order cancel confirmed sales order cancel confirm order
    odoo set to draft sales order cancel done sales order reverse sales order process cancel confirm quotation reverse delivery order.
    odoo Sale order reverse workflow Sales reverse workflow Sale cancel Sales cancel order feature on sale
    odoo Cancel sales order Reverse sales order modify confirm sales order stock picking reverse workflow
    odoo stock picking cancel delivery order cancel incoming shipment cancel cancel picking order cancel delivery order 
    odoo cancel incoming shipment cancel order set to draft picking cancel done picking reverse picking process cancel done delivery order reverse delivery order.
    odoo stock picking reverse workflow stock picking cancel delivery order cancel delivering cancel
    odoo cancel picking order cancel delivery order cancel shipment shipment cancel order set to draft picking 
    odoo cancel done picking reverse picking process cancel done delivery order orden de entrega inversa.
    When cancelling sale order if there are related invoices or delivery orders. It automatically cancels it too.
    odoo Sales Order reverse workflow Sale order cancel Sale order cancel Sales order cancel cancel Sales order cancel sale order cancel confirmed sales order cancel 
    odoo cancel confirm order set to draft sales order cancel done sales order reverse sales order process cancel confirm quotation
    odoo reverse delivery order Sale order reverse workflow Sales reverse workflow Sale cancel Sales cancel order feature on sale. 
    odoo Cancel sales order Reverse sales order modify confirm sales order stock picking reverse workflow
    odoo stock picking cancel delivery order cancel incoming shipment cancel cancel picking order cancel delivery order
    odoo cancel incoming shipment cancel order set to draft picking cancel done picking revese picking process cancel done delivery order reverse delivery order.
    odoo stock picking reverse workflow stock picking cancel delivery order cancel delivering cancel cancel picking order cancel delivery order cancel shipment shipment cancel order set to draft picking 
    odoo cancel done picking revese picking process cancel done delivery order orden de entrega inversa.

sélection de stock reverse workflow, sélection de stock annuler, annulation de commande, annulation de livraison, annulation de commande, annulation de livraison, annulation de livraison, annulation de la commande, annulation de la sélection, annulation de la préparation, annulation du bon de livraison. ordre de livraison inverse.
 -purchases Order reverse workflow, purchase order cancel, purchase order cancel, purchases order cancel, cancel purchases order, cancel purchase order, cancel confirmed purchases order, cancel confirm order, set to draft purchases order, cancel done purchases order, revese purchases order process, cancel confirm quotation.reverse delivery order.
    purchase order reverse workflow
purchases reverse workflow, purchase cancel, purchases cancel order feature on purchase. Cancel purchases order, Reverse purchases order
    modify confirm purchases order
    cancel stock picking , reset all orders
    odoo cancel and reset to draft picking cancel reset picking cancel picking cancel delivery order
    odoo cancel incoming shipment reverse stock picking reverse delivery order Cancel invoice based on sales
    odoo cancel invoice based on purchase cancel all in one order cancel all orders
    odoo all order cancel all order reset all order reverse reverse order all in one

odoo stock inventory reverse workflow stock inventory cancel inventory adjustment cancel incoming shipment cancel 
odoo cancel inventory adjustment cancel delivery order cancel incoming shipment cancel order set to draft picking cancel done picking revese picking process 
odoo cancel done delivery order reverse delivery order stock inventory adjustment reverse workflow stock inventory adjustment cancel
odoo stock adjustment reverse workflow warehouse stock cancel stock warehouse cancel cancel stock for inventory cancel stock inventory cancel inventory adjustment from done state 
odoo cancel warehouse stock adjustment cancel order set to draft picking cancel done picking revese picking process cancel done delivery order. 
odoo orden de entrega inversa sélection de stock reverse workflow sélection de stock annuler 
annulation de commande annulation de livraison annulation de commande annulation de livraison annulation de livraison
 annulation de la commande annulation de la sélection annulation de la préparation annulation du bon de livraison. ordre de livraison inverse.
 odoo cancel stock Inventory Adjustment cancel and reset to draft Inventory Adjustment odoo cancel reset Inventory Adjustment 
 odoo cancel Inventory Adjustment cancel delivery stock Adjustment cancel stock Adjustment
odoo reverse Inventory Adjustment reverse stock Inventory Adjustment
odoo cancel orders order cancel odoo cancel picking odoo cancel stock move odoo stock move cancel
odoo Inventory Adjustment Cancel Cancel Inventory Adjustment delivery cancel
odoo picking cancel Reverse order reverse picking reverse delivery reverse shipment

    """,
    'price': 49,
    'currency': "EUR",
    "data": [
        "security/picking_security.xml",
        "views/stock_view.xml"
    ],
    'license':'OPL-1',
    'live_test_url':'https://www.youtube.com/watch?v=ebUnHk_eStc&feature=youtu.be',
    "test": [],
    "js": [],
    "css": [],
    "qweb": [],
    "installable": True,
    "auto_install": False,
    "images":['static/description/Banner.png'],
}
