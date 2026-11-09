import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings


class ProxySecurityTests(TestCase):
    def settings_for(self, trust):
        with patch.dict(os.environ, {'DJANGO_TRUST_PROXY': str(trust).lower(), 'DJANGO_SECURE_COOKIES': 'true', 'DJANGO_SECURE_SSL_REDIRECT': 'true', 'DJANGO_CSRF_TRUSTED_ORIGINS': 'https://demo.test:8443'}):
            return runpy.run_path(str(Path(__file__).resolve().parents[1]/'config/settings.py'))

    def test_proxy_trust_is_explicit_and_cookies_are_secure(self):
        values = self.settings_for(True)
        self.assertEqual(values.get('SECURE_PROXY_SSL_HEADER'), ('HTTP_X_FORWARDED_PROTO', 'https'))
        self.assertTrue(values.get('SESSION_COOKIE_SECURE'))
        self.assertTrue(values.get('CSRF_COOKIE_SECURE'))
        self.assertEqual(values.get('CSRF_TRUSTED_ORIGINS'), ['https://demo.test:8443'])
        self.assertIsNone(self.settings_for(False).get('SECURE_PROXY_SSL_HEADER'))

    def test_proxy_login_with_csrf_sets_secure_session_and_rejects_missing_token(self):
        values = self.settings_for(True)
        names = ['SECURE_PROXY_SSL_HEADER','SESSION_COOKIE_SECURE','CSRF_COOKIE_SECURE','SECURE_SSL_REDIRECT','CSRF_TRUSTED_ORIGINS']
        self.assertTrue(values.get('SESSION_COOKIE_SECURE'), 'TLS settings not implemented')
        with override_settings(**{k: values[k] for k in names}, ALLOWED_HOSTS=['demo.test']):
            get_user_model().objects.create_user('tls-user', password='Testing-tls-428!')
            client = Client(enforce_csrf_checks=True, HTTP_HOST='demo.test:8443', HTTP_X_FORWARDED_PROTO='https')
            response = client.get('/accounts/login/')
            self.assertEqual(response.status_code, 200)
            token = client.cookies['csrftoken'].value
            data = {'username':'tls-user','password':'Testing-tls-428!'}
            self.assertEqual(client.post('/accounts/login/', data, HTTP_ORIGIN='https://demo.test:8443').status_code, 403)
            data['csrfmiddlewaretoken'] = token
            response = client.post('/accounts/login/', data, HTTP_ORIGIN='https://demo.test:8443')
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.cookies['sessionid']['secure'])
            self.assertEqual(client.get('/').status_code, 200)

    def test_spoofed_proxy_header_without_opt_in_does_not_disable_redirect(self):
        values = self.settings_for(False)
        self.assertTrue(values.get('SECURE_SSL_REDIRECT'), 'TLS redirect setting missing')
        with override_settings(SECURE_PROXY_SSL_HEADER=values.get('SECURE_PROXY_SSL_HEADER'), SECURE_SSL_REDIRECT=True):
            response = self.client.get('/accounts/login/', HTTP_X_FORWARDED_PROTO='https')
            self.assertEqual(response.status_code, 301)
