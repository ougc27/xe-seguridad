import base64
import hashlib
import json
import re
import secrets

from urllib.parse import urlencode, urlparse, parse_qsl

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged


def _pkce_pair():
    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return verifier, challenge


class TestMcpOAuthModels(TransactionCase):

    # ----------------------------------------------------------
    # Setup
    # ----------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_model = cls.env['muk_mcp.oauth.client']
        cls.code_model = cls.env['muk_mcp.oauth.code']
        cls.token_model = cls.env['muk_mcp.oauth.token']
        cls.redirect_uri = 'http://localhost:12345/callback'
        response, error = cls.client_model.register({
            'client_name': 'Test Client',
            'redirect_uris': [cls.redirect_uri],
        })
        assert error is None, error
        cls.client_response = response
        cls.client = cls.client_model.sudo().search([
            ('client_id', '=', response['client_id']),
        ], limit=1)

    # ----------------------------------------------------------
    # Tests: Dynamic Client Registration
    # ----------------------------------------------------------

    def test_register_requires_redirect_uris(self):
        response, error = self.client_model.register({'client_name': 'X'})
        self.assertIsNone(response)
        self.assertTrue(error)

    def test_register_defaults_to_public_client(self):
        self.assertEqual(self.client.token_endpoint_auth_method, 'none')
        self.assertNotIn('client_secret', self.client_response)

    def test_check_redirect_uri(self):
        self.assertTrue(self.client._check_redirect_uri(self.redirect_uri))
        self.assertFalse(self.client._check_redirect_uri(
            'http://evil.example.com/callback',
        ))

    # ----------------------------------------------------------
    # Tests: PKCE authorization codes
    # ----------------------------------------------------------

    def test_code_pkce_round_trip(self):
        verifier, challenge = _pkce_pair()
        code = self.code_model._generate(
            self.client, self.env.user, self.redirect_uri, 'write', challenge,
        )
        record = self.code_model._consume(
            code, self.client.client_id, self.redirect_uri, verifier,
        )
        self.assertTrue(record)
        self.assertEqual(record.user_id, self.env.user)

    def test_code_cannot_be_used_twice(self):
        verifier, challenge = _pkce_pair()
        code = self.code_model._generate(
            self.client, self.env.user, self.redirect_uri, 'write', challenge,
        )
        self.assertTrue(self.code_model._consume(
            code, self.client.client_id, self.redirect_uri, verifier,
        ))
        self.assertFalse(self.code_model._consume(
            code, self.client.client_id, self.redirect_uri, verifier,
        ))

    def test_code_rejects_wrong_verifier(self):
        _verifier, challenge = _pkce_pair()
        code = self.code_model._generate(
            self.client, self.env.user, self.redirect_uri, 'write', challenge,
        )
        self.assertFalse(self.code_model._consume(
            code, self.client.client_id, self.redirect_uri, 'wrong-verifier',
        ))

    def test_code_rejects_mismatched_redirect_uri(self):
        verifier, challenge = _pkce_pair()
        code = self.code_model._generate(
            self.client, self.env.user, self.redirect_uri, 'write', challenge,
        )
        self.assertFalse(self.code_model._consume(
            code, self.client.client_id,
            'http://localhost:9999/other', verifier,
        ))

    # ----------------------------------------------------------
    # Tests: Access / refresh tokens
    # ----------------------------------------------------------

    def test_token_issue_and_authenticate(self):
        issued = self.token_model._issue(self.client, self.env.user, 'write')
        found = self.token_model.authenticate(issued['access_token'])
        self.assertEqual(found.id, issued['record'].id)

    def test_token_authenticate_rejects_unknown_token(self):
        self.assertFalse(self.token_model.authenticate('bogus-token'))

    def test_token_refresh_rotates_and_invalidates_old(self):
        issued = self.token_model._issue(self.client, self.env.user, 'write')
        refreshed = self.token_model._refresh(
            issued['refresh_token'], self.client.client_id,
        )
        self.assertTrue(refreshed)
        self.assertNotEqual(
            refreshed['access_token'], issued['access_token'],
        )
        self.assertFalse(self.token_model._refresh(
            issued['refresh_token'], self.client.client_id,
        ))

    def test_token_revoke_blocks_authentication(self):
        issued = self.token_model._issue(self.client, self.env.user, 'write')
        issued['record'].action_revoke()
        self.assertFalse(
            self.token_model.authenticate(issued['access_token'])
        )

    # ----------------------------------------------------------
    # Tests: scope is admin-only, even on your own token
    # ----------------------------------------------------------

    def test_non_admin_cannot_write_scope_on_own_token(self):
        non_admin = self.env['res.users'].create({
            'name': 'Non Admin',
            'login': 'oauth_non_admin_scope',
            'email': 'oauth_non_admin_scope@example.com',
            'groups_id': [(4, self.env.ref('base.group_user').id)],
        })
        issued = self.token_model._issue(self.client, non_admin, 'read')
        record = issued['record'].with_user(non_admin)
        # Allowed: revoking their own connection doesn't touch scope.
        record.write({'revoked': True})
        self.assertTrue(record.revoked)
        # Denied: the scope field itself requires base.group_system.
        with self.assertRaises(AccessError):
            record.write({'scope': 'write'})

    def test_admin_can_write_scope_on_any_token(self):
        non_admin = self.env['res.users'].create({
            'name': 'Non Admin 2',
            'login': 'oauth_non_admin_scope_2',
            'email': 'oauth_non_admin_scope_2@example.com',
            'groups_id': [(4, self.env.ref('base.group_user').id)],
        })
        issued = self.token_model._issue(self.client, non_admin, 'read')
        issued['record'].write({'scope': 'write'})
        self.assertEqual(issued['record'].scope, 'write')


@tagged('post_install', '-at_install')
class TestMcpOAuthController(HttpCase):

    # ----------------------------------------------------------
    # Setup
    # ----------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param(
            'muk_mcp.oauth_enabled', 'True',
        )

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    def _register_client(self, redirect_uri):
        response = self.url_open(
            '/oauth/register',
            data=json.dumps({
                'client_name': 'HttpCase Client',
                'redirect_uris': [redirect_uri],
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def _make_user(self, login):
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': f'{login}@example.com',
            'password': 'test-password-1234',
            'groups_id': [(4, self.env.ref('base.group_user').id)],
        })

    def _authorize_and_get_code(self, client_id, redirect_uri, challenge, scope='write'):
        authorize_url = '/oauth/authorize?' + urlencode({
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'scope': scope,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        authorize_response = self.url_open(authorize_url)
        self.assertEqual(authorize_response.status_code, 200)
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"', authorize_response.text,
        )
        self.assertTrue(match, "csrf token not found on consent page")
        confirm_response = self.url_open(
            '/oauth/authorize',
            data={
                'csrf_token': match.group(1),
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'scope': scope,
                'code_challenge': challenge,
                'decision': 'allow',
            },
            allow_redirects=False,
        )
        self.assertIn(confirm_response.status_code, (301, 302, 303))
        location = confirm_response.headers['Location']
        return dict(parse_qsl(urlparse(location).query))['code']

    def _exchange_code(self, client_id, redirect_uri, code, verifier):
        response = self.url_open(
            '/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
                'client_id': client_id,
                'code_verifier': verifier,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _mcp_initialize(self, access_token):
        return self.url_open(
            '/mcp',
            data=json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {},
            }),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
            },
        )

    # ----------------------------------------------------------
    # Tests: Discovery
    # ----------------------------------------------------------

    def test_discovery_metadata(self):
        response = self.url_open('/.well-known/oauth-authorization-server')
        self.assertEqual(response.status_code, 200)
        metadata = response.json()
        self.assertTrue(
            metadata['authorization_endpoint'].endswith('/oauth/authorize')
        )
        self.assertIn('S256', metadata['code_challenge_methods_supported'])

    def test_protected_resource_metadata(self):
        response = self.url_open('/.well-known/oauth-protected-resource')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['resource'].endswith('/mcp'))

    def test_discovery_disabled_returns_404(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'muk_mcp.oauth_enabled', 'False',
        )
        try:
            response = self.url_open(
                '/.well-known/oauth-authorization-server'
            )
            self.assertEqual(response.status_code, 404)
        finally:
            self.env['ir.config_parameter'].sudo().set_param(
                'muk_mcp.oauth_enabled', 'True',
            )

    def test_mcp_401_advertises_resource_metadata(self):
        response = self._mcp_initialize('not-a-real-token')
        self.assertEqual(response.status_code, 401)
        self.assertIn(
            'resource_metadata',
            response.headers.get('WWW-Authenticate', ''),
        )

    # ----------------------------------------------------------
    # Tests: Full authorization code + PKCE flow
    # ----------------------------------------------------------

    def test_full_authorization_code_flow(self):
        redirect_uri = 'http://localhost:12345/callback'
        registration = self._register_client(redirect_uri)
        client_id = registration['client_id']
        verifier, challenge = _pkce_pair()

        self.authenticate('admin', 'admin')

        authorize_url = '/oauth/authorize?' + urlencode({
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'scope': 'write',
            'state': 'xyz',
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        authorize_response = self.url_open(authorize_url)
        self.assertEqual(authorize_response.status_code, 200)
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"', authorize_response.text,
        )
        self.assertTrue(match, "csrf token not found on consent page")
        csrf_token = match.group(1)

        confirm_response = self.url_open(
            '/oauth/authorize',
            data={
                'csrf_token': csrf_token,
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'scope': 'write',
                'state': 'xyz',
                'code_challenge': challenge,
                'decision': 'allow',
            },
            allow_redirects=False,
        )
        self.assertIn(confirm_response.status_code, (301, 302, 303))
        location = confirm_response.headers['Location']
        self.assertTrue(location.startswith(redirect_uri))
        query = dict(parse_qsl(urlparse(location).query))
        self.assertEqual(query.get('state'), 'xyz')
        code = query['code']

        token_response = self.url_open(
            '/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
                'client_id': client_id,
                'code_verifier': verifier,
            },
        )
        self.assertEqual(token_response.status_code, 200)
        tokens = token_response.json()
        self.assertIn('access_token', tokens)
        self.assertIn('refresh_token', tokens)

        mcp_response = self._mcp_initialize(tokens['access_token'])
        self.assertEqual(mcp_response.status_code, 200)

        # The authorization code is single-use.
        replay_response = self.url_open(
            '/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
                'client_id': client_id,
                'code_verifier': verifier,
            },
        )
        self.assertEqual(replay_response.status_code, 400)

        refresh_response = self.url_open(
            '/oauth/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': tokens['refresh_token'],
                'client_id': client_id,
            },
        )
        self.assertEqual(refresh_response.status_code, 200)
        new_tokens = refresh_response.json()
        self.assertNotEqual(
            new_tokens['access_token'], tokens['access_token'],
        )

        revoke_response = self.url_open(
            '/oauth/revoke', data={'token': new_tokens['access_token']},
        )
        self.assertEqual(revoke_response.status_code, 200)

        after_revoke = self._mcp_initialize(new_tokens['access_token'])
        self.assertEqual(after_revoke.status_code, 401)

    def test_authorize_rejects_unregistered_redirect_uri(self):
        registration = self._register_client(
            'http://localhost:12345/callback'
        )
        _verifier, challenge = _pkce_pair()
        self.authenticate('admin', 'admin')

        authorize_url = '/oauth/authorize?' + urlencode({
            'response_type': 'code',
            'client_id': registration['client_id'],
            'redirect_uri': 'http://not-registered.example.com/callback',
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        response = self.url_open(authorize_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('name="csrf_token"', response.text)

    # ----------------------------------------------------------
    # Tests: scope selection is admin-only on the consent screen
    # ----------------------------------------------------------

    def test_non_admin_consent_hides_scope_choice_and_is_forced_read(self):
        redirect_uri = 'http://localhost:12345/callback'
        registration = self._register_client(redirect_uri)
        client_id = registration['client_id']
        verifier, challenge = _pkce_pair()

        user = self._make_user('oauth_http_non_admin')
        self.authenticate(user.login, 'test-password-1234')

        authorize_url = '/oauth/authorize?' + urlencode({
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        page = self.url_open(authorize_url)
        self.assertEqual(page.status_code, 200)
        # No radio buttons offered to a non-admin.
        self.assertNotIn('name="scope"', page.text)

        # Even if they tamper with the (now absent) scope value, the
        # server must ignore it and force read-only.
        code = self._authorize_and_get_code(
            client_id, redirect_uri, challenge, scope='write',
        )
        tokens = self._exchange_code(client_id, redirect_uri, code, verifier)
        self.assertEqual(tokens['scope'], 'read')

    def test_admin_consent_can_choose_read_only(self):
        redirect_uri = 'http://localhost:12345/callback'
        registration = self._register_client(redirect_uri)
        client_id = registration['client_id']
        verifier, challenge = _pkce_pair()

        self.authenticate('admin', 'admin')

        authorize_url = '/oauth/authorize?' + urlencode({
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        page = self.url_open(authorize_url)
        self.assertEqual(page.status_code, 200)
        # An admin does get the choice.
        self.assertIn('name="scope"', page.text)

        code = self._authorize_and_get_code(
            client_id, redirect_uri, challenge, scope='read',
        )
        tokens = self._exchange_code(client_id, redirect_uri, code, verifier)
        self.assertEqual(tokens['scope'], 'read')

    def test_token_rejects_unknown_client(self):
        response = self.url_open(
            '/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': 'whatever',
                'redirect_uri': 'http://localhost:12345/callback',
                'client_id': 'unknown-client-id',
                'code_verifier': 'whatever',
            },
        )
        self.assertEqual(response.status_code, 401)
