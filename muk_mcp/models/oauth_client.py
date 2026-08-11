import hashlib
import secrets

from odoo import api, fields, models


class MCPOAuthClient(models.Model):

    _name = 'muk_mcp.oauth.client'
    _description = "MCP OAuth Client"
    _order = 'create_date desc'

    # ----------------------------------------------------------
    # Fields
    # ----------------------------------------------------------

    client_id = fields.Char(
        string="Client ID",
        required=True,
        readonly=True,
        index=True,
    )

    client_name = fields.Char(
        string="Client Name",
        required=True,
        readonly=True,
    )

    client_secret_hash = fields.Char(
        string="Client Secret Hash",
        readonly=True,
    )

    token_endpoint_auth_method = fields.Selection(
        selection=[
            ('none', "None (PKCE, public client)"),
            ('client_secret_post', "Client Secret"),
        ],
        string="Auth Method",
        required=True,
        default='none',
        readonly=True,
    )

    redirect_uris = fields.Json(
        string="Redirect URIs",
        readonly=True,
    )

    grant_types = fields.Json(
        string="Grant Types",
        readonly=True,
    )

    software_id = fields.Char(
        string="Software ID",
        readonly=True,
        help="Identifier reported by the client during registration "
             "(RFC 7591), for reference only.",
    )

    active = fields.Boolean(
        string="Active",
        default=True,
    )

    create_date = fields.Datetime(
        string="Registered",
        readonly=True,
    )

    _sql_constraints = [(
        'client_id_uniq', 'unique(client_id)',
        "Client ID must be unique.",
    )]

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    @staticmethod
    def _hash_secret(secret):
        return hashlib.sha256(secret.encode()).hexdigest()

    def _check_redirect_uri(self, redirect_uri):
        self.ensure_one()
        return bool(redirect_uri) and redirect_uri in (self.redirect_uris or [])

    def _check_secret(self, client_secret):
        self.ensure_one()
        if self.token_endpoint_auth_method != 'client_secret_post':
            return True
        return bool(client_secret) and self.client_secret_hash == (
            self._hash_secret(client_secret)
        )

    # ----------------------------------------------------------
    # Functions
    # ----------------------------------------------------------

    @api.model
    def register(self, metadata):
        """Dynamic Client Registration (RFC 7591).

        Returns (response_dict, None) on success, or (None, error_message)
        when the request is rejected.
        """
        redirect_uris = metadata.get('redirect_uris')
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return None, "redirect_uris is required"
        for uri in redirect_uris:
            if not isinstance(uri, str) or not uri:
                return None, "redirect_uris must be a list of URIs"

        auth_method = metadata.get('token_endpoint_auth_method', 'none')
        if auth_method not in ('none', 'client_secret_post'):
            auth_method = 'none'

        grant_types = metadata.get('grant_types') or [
            'authorization_code', 'refresh_token',
        ]
        if not isinstance(grant_types, list):
            grant_types = ['authorization_code', 'refresh_token']

        client_id = secrets.token_urlsafe(24)
        values = {
            'client_id': client_id,
            'client_name': (metadata.get('client_name') or 'MCP Client')[:256],
            'token_endpoint_auth_method': auth_method,
            'redirect_uris': redirect_uris,
            'grant_types': grant_types,
            'software_id': metadata.get('software_id'),
        }

        client_secret = None
        if auth_method == 'client_secret_post':
            client_secret = secrets.token_urlsafe(32)
            values['client_secret_hash'] = self._hash_secret(client_secret)

        record = self.sudo().create(values)

        response = {
            'client_id': client_id,
            'client_name': record.client_name,
            'redirect_uris': redirect_uris,
            'grant_types': record.grant_types,
            'response_types': ['code'],
            'token_endpoint_auth_method': auth_method,
        }
        if client_secret:
            response['client_secret'] = client_secret
        return response, None
