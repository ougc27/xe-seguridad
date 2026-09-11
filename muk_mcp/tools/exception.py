import werkzeug

from odoo.exceptions import AccessError


class MCPScopeDenied(AccessError):
    pass


class MCPUnauthorized(werkzeug.exceptions.Unauthorized):
    """Raised by the 'mcp' auth method on a missing/invalid Bearer token.

    Carries the OAuth Protected Resource metadata URL (RFC 9728) in the
    WWW-Authenticate header so remote MCP clients (e.g. Claude Desktop's
    custom connectors) can discover the OAuth server and start the
    authorization flow instead of just failing outright.
    """

    def __init__(self, resource_metadata_url):
        super().__init__()
        self.resource_metadata_url = resource_metadata_url

    def get_headers(self, environ=None, scope=None):
        headers = super().get_headers(environ, scope)
        headers.append((
            'WWW-Authenticate',
            f'Bearer resource_metadata="{self.resource_metadata_url}"',
        ))
        return headers
