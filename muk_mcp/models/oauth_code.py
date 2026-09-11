import base64
import hashlib
import hmac
import secrets

from odoo import api, fields, models


class MCPOAuthCode(models.Model):

    _name = 'muk_mcp.oauth.code'
    _description = "MCP OAuth Authorization Code"
    _order = 'create_date desc'

    # ----------------------------------------------------------
    # Fields
    # ----------------------------------------------------------

    code_hash = fields.Char(
        string="Code Hash",
        required=True,
        index=True,
    )

    client_id = fields.Many2one(
        comodel_name='muk_mcp.oauth.client',
        string="Client",
        required=True,
        ondelete='cascade',
    )

    user_id = fields.Many2one(
        comodel_name='res.users',
        string="User",
        required=True,
        ondelete='cascade',
    )

    redirect_uri = fields.Char(
        string="Redirect URI",
        required=True,
    )

    scope = fields.Selection(
        selection=[
            ('read', "Read Only"),
            ('write', "Read & Write"),
        ],
        string="Scope",
        required=True,
        default='write',
    )

    code_challenge = fields.Char(
        string="Code Challenge",
        required=True,
    )

    expires_at = fields.Datetime(
        string="Expires At",
        required=True,
    )

    used = fields.Boolean(
        string="Used",
        default=False,
    )

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    @staticmethod
    def _hash_code(code):
        return hashlib.sha256(code.encode()).hexdigest()

    @staticmethod
    def _verify_pkce(code_verifier, code_challenge):
        if not code_verifier or not code_challenge:
            return False
        digest = hashlib.sha256(code_verifier.encode()).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
        return hmac.compare_digest(computed, code_challenge)

    # ----------------------------------------------------------
    # Functions
    # ----------------------------------------------------------

    @api.model
    def _generate(self, client, user, redirect_uri, scope, code_challenge):
        raw_code = secrets.token_urlsafe(32)
        self.sudo().create({
            'code_hash': self._hash_code(raw_code),
            'client_id': client.id,
            'user_id': user.id,
            'redirect_uri': redirect_uri,
            'scope': scope,
            'code_challenge': code_challenge,
            'expires_at': fields.Datetime.add(
                fields.Datetime.now(), minutes=5,
            ),
        })
        return raw_code

    @api.model
    def _consume(self, code, client_id, redirect_uri, code_verifier):
        """Validates and single-use-consumes an authorization code.

        Returns the (already-used) record on success, or None if the
        code is unknown, expired, already used, or the client/redirect/
        PKCE verifier don't match what was issued.
        """
        record = self.sudo().search([
            ('code_hash', '=', self._hash_code(code)),
        ], limit=1)
        if (
            not record or record.used or
            record.expires_at < fields.Datetime.now() or
            record.client_id.client_id != client_id or
            record.redirect_uri != redirect_uri or
            not self._verify_pkce(code_verifier, record.code_challenge)
        ):
            return None
        record.used = True
        return record

    # ----------------------------------------------------------
    # Cron
    # ----------------------------------------------------------

    @api.autovacuum
    def _autovacuum_codes(self):
        self.search([
            ('expires_at', '<', fields.Datetime.now()),
        ]).unlink()
