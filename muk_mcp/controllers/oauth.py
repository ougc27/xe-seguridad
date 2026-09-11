import json

from urllib.parse import urlencode

from odoo import http, _
from odoo.http import request
from odoo.tools.misc import str2bool

from odoo.addons.muk_mcp.tools.rate_limit import rate_limiter


class MCPOAuthController(http.Controller):
    """Minimal OAuth 2.0 Authorization Server for the MCP endpoint.

    Implements just enough of RFC 8414 (metadata discovery), RFC 7591
    (dynamic client registration), RFC 6749 (authorization code grant
    with refresh tokens) and RFC 7636 (PKCE) for remote MCP clients such
    as Claude Desktop/Claude.ai "custom connectors" to connect by URL
    only, authenticating with the user's own Odoo login instead of a
    bearer key pasted into a config file.

    Coexists with the existing muk_mcp.key bearer-key flow: both are
    accepted by the 'mcp' auth method in models/ir_http.py.
    """

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    @staticmethod
    def _oauth_enabled():
        return str2bool(
            request.env['ir.config_parameter'].sudo().get_param(
                'muk_mcp.oauth_enabled', 'False',
            ),
            default=False,
        )

    @staticmethod
    def _base_url():
        return request.httprequest.url_root.rstrip('/')

    @staticmethod
    def _error_response(error, description, status=400):
        return request.make_json_response({
            'error': error,
            'error_description': description,
        }, status=status, headers={'Cache-Control': 'no-store'})

    @staticmethod
    def _append_query(url, params):
        separator = '&' if '?' in url else '?'
        return f'{url}{separator}{urlencode(params)}'

    def _get_client(self, client_id):
        return request.env['muk_mcp.oauth.client'].sudo().search([
            ('client_id', '=', client_id),
            ('active', '=', True),
        ], limit=1)

    @staticmethod
    def _error_title():
        return _("Couldn't complete the connection")

    @staticmethod
    def _consent_i18n():
        # Translated in Python, not in the QWeb template: substituting
        # translations into raw view text is brittle (whitespace/markup
        # boundaries), while _() lookups are a simple, reliable key match.
        return {
            'title': _("Connect to Odoo"),
            'wants_to_connect': _("wants to connect to Odoo"),
            'signing_in_as': _("Signing in as"),
            'connection_scope': _("Connection scope:"),
            'read_write': _("Read & Write"),
            'read_only': _("Read Only"),
            'read_only_notice': _(
                "This connection will be read-only. A Technical Settings "
                "user can grant it read & write access from Settings > "
                "MCP > OAuth Sessions."
            ),
            'authorize_notice': _(
                "If you authorize this, the application will be able to "
                "access Odoo with your user permissions until you revoke "
                "access from Settings > MCP > OAuth Sessions."
            ),
            'cancel': _("Cancel"),
            'authorize': _("Authorize"),
        }

    # ----------------------------------------------------------
    # Discovery
    # ----------------------------------------------------------

    @http.route(
        '/.well-known/oauth-authorization-server',
        type='http', auth='public', csrf=False, methods=['GET'],
    )
    def oauth_authorization_server_metadata(self, **kw):
        if not self._oauth_enabled():
            return request.not_found()
        base_url = self._base_url()
        return request.make_json_response({
            'issuer': base_url,
            'authorization_endpoint': f'{base_url}/oauth/authorize',
            'token_endpoint': f'{base_url}/oauth/token',
            'registration_endpoint': f'{base_url}/oauth/register',
            'revocation_endpoint': f'{base_url}/oauth/revoke',
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'code_challenge_methods_supported': ['S256'],
            'token_endpoint_auth_methods_supported': [
                'none', 'client_secret_post',
            ],
            'scopes_supported': ['read', 'write'],
        })

    @http.route(
        '/.well-known/oauth-protected-resource',
        type='http', auth='public', csrf=False, methods=['GET'],
    )
    def oauth_protected_resource_metadata(self, **kw):
        if not self._oauth_enabled():
            return request.not_found()
        base_url = self._base_url()
        return request.make_json_response({
            'resource': f'{base_url}/mcp',
            'authorization_servers': [base_url],
            'bearer_methods_supported': ['header'],
        })

    # ----------------------------------------------------------
    # Dynamic Client Registration (RFC 7591)
    # ----------------------------------------------------------

    @http.route(
        '/oauth/register',
        type='http', auth='public', csrf=False, methods=['POST'],
    )
    def oauth_register(self, **kw):
        if not self._oauth_enabled():
            return request.not_found()
        if not rate_limiter.check(
            f'oauth_register:{request.httprequest.remote_addr}', 20, 60,
        ):
            return self._error_response(
                'rate_limited', "Too many registration requests", status=429,
            )
        try:
            metadata = json.loads(
                request.httprequest.get_data(as_text=True) or '{}'
            )
        except ValueError:
            return self._error_response(
                'invalid_client_metadata', "Malformed JSON body",
            )
        if not isinstance(metadata, dict):
            return self._error_response(
                'invalid_client_metadata', "Malformed JSON body",
            )
        response, error = request.env['muk_mcp.oauth.client'].register(
            metadata,
        )
        if error:
            return self._error_response('invalid_client_metadata', error)
        return request.make_json_response(response, status=201)

    # ----------------------------------------------------------
    # Authorization Endpoint
    # ----------------------------------------------------------

    @http.route(
        '/oauth/authorize',
        type='http', auth='public', csrf=False, methods=['GET'],
    )
    def oauth_authorize(self, **kw):
        if not self._oauth_enabled():
            return request.not_found()

        client = self._get_client(kw.get('client_id'))
        redirect_uri = kw.get('redirect_uri')

        if (
            kw.get('response_type') != 'code' or not client or
            not redirect_uri or not client._check_redirect_uri(redirect_uri) or
            kw.get('code_challenge_method') != 'S256' or
            not kw.get('code_challenge')
        ):
            return request.render('muk_mcp.oauth_authorize_error', {
                'title': self._error_title(),
                'message': _(
                    "This connection request is invalid or the "
                    "application is not recognized. Please restart the "
                    "connection from your AI client."
                ),
            })

        if not request.session.uid:
            return request.redirect(self._append_query('/web/login', {
                'redirect': request.httprequest.full_path,
            }))

        # Only System users may pick read vs. write for their own
        # connection; everyone else is capped at read-only until a
        # System user changes it for them from Settings > MCP > OAuth
        # Sessions.
        is_admin = request.env.user.has_group('base.group_system')
        requested_scopes = (kw.get('scope') or 'write').split()
        requested_scope = (
            'read' if requested_scopes and
            set(requested_scopes) <= {'read'} else 'write'
        )
        default_scope = requested_scope if is_admin else 'read'

        return request.render('muk_mcp.oauth_authorize_consent', {
            'client': client,
            'scope': default_scope,
            'is_admin': is_admin,
            'redirect_uri': redirect_uri,
            'state': kw.get('state', ''),
            'code_challenge': kw.get('code_challenge'),
            'csrf_token': request.csrf_token(),
            'user': request.env.user,
            'i18n': self._consent_i18n(),
        })

    @http.route(
        '/oauth/authorize',
        type='http', auth='public', csrf=False, methods=['POST'],
    )
    def oauth_authorize_confirm(self, **post):
        if not self._oauth_enabled():
            return request.not_found()

        error_page = lambda message: request.render(
            'muk_mcp.oauth_authorize_error',
            {'title': self._error_title(), 'message': message},
        )

        if not request.session.uid:
            return error_page(_(
                "Your session expired. Please restart the connection "
                "from your AI client."
            ))
        if not request.validate_csrf(post.get('csrf_token')):
            return error_page(_(
                "This form has expired. Please restart the connection "
                "from your AI client."
            ))

        client = self._get_client(post.get('client_id'))
        redirect_uri = post.get('redirect_uri')
        if not client or not client._check_redirect_uri(redirect_uri):
            return error_page(_(
                "This connection request is invalid. Please restart the "
                "connection from your AI client."
            ))

        state = post.get('state', '')

        if post.get('decision') != 'allow':
            params = {'error': 'access_denied'}
            if state:
                params['state'] = state
            return request.redirect(
                self._append_query(redirect_uri, params), local=False,
            )

        # Re-derive from the current session, never trust the posted
        # hidden field: a non-admin could edit it in devtools and try
        # to grant themselves write access.
        is_admin = request.env.user.has_group('base.group_system')
        if is_admin:
            scope = post.get('scope') if post.get('scope') in ('read', 'write') else 'write'
        else:
            scope = 'read'

        code = request.env['muk_mcp.oauth.code']._generate(
            client, request.env.user, redirect_uri, scope,
            post.get('code_challenge'),
        )
        params = {'code': code}
        if state:
            params['state'] = state
        return request.redirect(
            self._append_query(redirect_uri, params), local=False,
        )

    # ----------------------------------------------------------
    # Token Endpoint
    # ----------------------------------------------------------

    @http.route(
        '/oauth/token',
        type='http', auth='public', csrf=False, methods=['POST'],
    )
    def oauth_token(self, **post):
        if not self._oauth_enabled():
            return request.not_found()
        if not rate_limiter.check(
            f'oauth_token_ip:{request.httprequest.remote_addr}', 30, 60,
        ):
            return self._error_response(
                'rate_limited', "Too many requests", status=429,
            )

        client_id = post.get('client_id')
        client = self._get_client(client_id)
        if not client:
            return self._error_response(
                'invalid_client', "Unknown client", status=401,
            )
        if not client._check_secret(post.get('client_secret')):
            return self._error_response(
                'invalid_client', "Client authentication failed", status=401,
            )

        grant_type = post.get('grant_type')
        if grant_type == 'authorization_code':
            code = post.get('code')
            redirect_uri = post.get('redirect_uri')
            code_verifier = post.get('code_verifier')
            if not (code and redirect_uri and code_verifier):
                return self._error_response(
                    'invalid_request', "Missing parameters",
                )
            auth_code = request.env['muk_mcp.oauth.code']._consume(
                code, client_id, redirect_uri, code_verifier,
            )
            if not auth_code:
                return self._error_response(
                    'invalid_grant', "Invalid or expired code",
                )
            issued = request.env['muk_mcp.oauth.token']._issue(
                client, auth_code.user_id, auth_code.scope,
            )
        elif grant_type == 'refresh_token':
            refresh_token = post.get('refresh_token')
            if not refresh_token:
                return self._error_response(
                    'invalid_request', "Missing refresh_token",
                )
            issued = request.env['muk_mcp.oauth.token']._refresh(
                refresh_token, client_id,
            )
            if not issued:
                return self._error_response(
                    'invalid_grant', "Invalid or expired refresh token",
                )
        else:
            return self._error_response(
                'unsupported_grant_type', grant_type or '',
            )

        return request.make_json_response({
            'access_token': issued['access_token'],
            'token_type': 'Bearer',
            'expires_in': issued['expires_in'],
            'refresh_token': issued['refresh_token'],
            'scope': issued['record'].scope,
        }, headers={'Cache-Control': 'no-store'})

    # ----------------------------------------------------------
    # Revocation Endpoint (RFC 7009)
    # ----------------------------------------------------------

    @http.route(
        '/oauth/revoke',
        type='http', auth='public', csrf=False, methods=['POST'],
    )
    def oauth_revoke(self, **post):
        if not self._oauth_enabled():
            return request.not_found()
        token = post.get('token')
        if token:
            token_model = request.env['muk_mcp.oauth.token']
            token_hash = token_model._hash_token(token)
            token_model.sudo().search([
                '|',
                ('access_token_hash', '=', token_hash),
                ('refresh_token_hash', '=', token_hash),
            ]).write({'revoked': True})
        # RFC 7009: always answer 200, even for unknown tokens.
        return request.make_json_response({})
