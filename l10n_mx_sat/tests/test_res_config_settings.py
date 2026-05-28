# Copyright 2026 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestResConfigSettings(TransactionCase):
    def test_test_connection_proxies_to_company(self):
        settings = self.env["res.config.settings"].create(
            {"company_id": self.env.company.id}
        )
        expected = {"type": "ir.actions.client", "tag": "display_notification"}
        with patch.object(
            type(settings.company_id),
            "l10n_mx_sat_test_connection",
            return_value=expected,
        ) as mock_method:
            result = settings.l10n_mx_sat_test_connection()

        self.assertEqual(result, expected)
        mock_method.assert_called_once_with()
