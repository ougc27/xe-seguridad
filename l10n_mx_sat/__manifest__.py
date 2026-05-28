# Copyright 2026 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

{
    "name": "Mexico - SAT Connection",
    "version": "18.0.1.0.0",
    "category": "Accounting/Localizations",
    "summary": "Connect to the SAT portal using FIEL credentials & manage downloads",
    "author": "Open Source Integrators, Odoo Community Association (OCA), Cloud Lotus",
    "website": "https://github.com/OCA/l10n-mexico",
    "license": "AGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["cfdiclient"]},
    "data": [
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "development_status": "Alpha",
    "maintainers": ["max3903"],
}
