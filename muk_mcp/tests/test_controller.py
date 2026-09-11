import json

from odoo.tests import HttpCase, tagged

from odoo.addons.muk_mcp.controllers.mcp import _TOOL_MODEL_OP
from odoo.addons.muk_mcp.core.tool import get_tool_index


@tagged('post_install', '-at_install')
class TestMcpController(HttpCase):

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    def _mcp_request(self, data, headers=None):
        all_headers = {
            'Content-Type': 'application/json',
        }
        if headers:
            all_headers.update(headers)
        return self.url_open(
            '/mcp',
            data=json.dumps(data),
            headers=all_headers,
        )

    # ----------------------------------------------------------
    # Tests
    # ----------------------------------------------------------

    def test_mcp_post_without_auth_is_rejected(self):
        response = self._mcp_request({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {},
        })
        self.assertNotEqual(response.status_code, 200)

    def test_mcp_get_without_auth_is_rejected(self):
        response = self.url_open('/mcp', headers={
            'Accept': 'text/event-stream',
        })
        self.assertNotEqual(response.status_code, 200)

    def test_tool_model_op_keys_match_registered_tools(self):
        # _TOOL_MODEL_OP is what gates create_records/update_records/
        # delete_records/etc. against mcp.enabled.model. It previously
        # used stale tool names (get_model_schema, read_record,
        # create_record, update_record, delete_record,
        # get_record_messages, execute_method) that don't exist in the
        # registry, which silently disabled that restriction for every
        # write operation. This guards against that class of drift.
        registered = set(get_tool_index(self.env).keys())
        mapped = set(_TOOL_MODEL_OP.keys())
        unknown = mapped - registered
        self.assertFalse(
            unknown,
            f"_TOOL_MODEL_OP references tool names that don't exist "
            f"in the registry: {unknown}",
        )
