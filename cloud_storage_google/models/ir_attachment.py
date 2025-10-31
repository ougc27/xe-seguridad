# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import re
import base64
import requests
import os
from urllib.parse import unquote, quote

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
except ImportError:
    service_account = Request = None

from odoo import models, tools
from odoo.exceptions import ValidationError, UserError

from ..utils.cloud_storage_google_utils import generate_signed_url_v4

CloudStorageGoogleCredentials = {}  # {db_name: (account_info, credential)}


def get_cloud_storage_google_credential(env):
    """ Get the credentials object of currently used account info.
    This method is cached to because from_service_account_info is slow.
    """
    cached_account_info, cached_credential = CloudStorageGoogleCredentials.get(env.registry.db_name, (None, None))
    account_info = json.loads(env['ir.config_parameter'].sudo().get_param('cloud_storage_google_account_info'))
    if cached_account_info == account_info:
        return cached_credential
    credential = service_account.Credentials.from_service_account_info(account_info)
    CloudStorageGoogleCredentials[env.registry.db_name] = (account_info, credential)
    return credential


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'
    _cloud_storage_google_url_pattern = re.compile(r'https://storage\.googleapis\.com/(?P<bucket_name>[\w\-.]+)/(?P<blob_name>[^?]+)')

    def _get_cloud_storage_google_info(self):
        match = self._cloud_storage_google_url_pattern.match(self.url)
        if not match:
            raise ValidationError('%s is not a valid Google Cloud Storage URL.', self.url)
        return {
            'bucket_name': match['bucket_name'],
            'blob_name': unquote(match['blob_name']),
        }

    def _generate_cloud_storage_google_url(self, blob_name):
        bucket_name = self.env['ir.config_parameter'].get_param('cloud_storage_google_bucket_name')
        return f"https://storage.googleapis.com/{bucket_name}/{quote(blob_name)}"

    def _generate_cloud_storage_google_signed_url(self, bucket_name, blob_name, **kwargs):
        quote_blob_name = quote(blob_name)
        return generate_signed_url_v4(
            credentials=get_cloud_storage_google_credential(self.env),
            resource=f'/{bucket_name}/{quote_blob_name}',
            **kwargs,
        )

    # OVERRIDES
    def _generate_cloud_storage_url(self):
        if self.env['ir.config_parameter'].sudo().get_param('cloud_storage_provider') != 'google':
            return super()._generate_cloud_storage_url()
        blob_name = self._generate_cloud_storage_blob_name()
        return self._generate_cloud_storage_google_url(blob_name)

    def _generate_cloud_storage_download_info(self):
        if self.env['ir.config_parameter'].sudo().get_param('cloud_storage_provider') != 'google':
            return super()._generate_cloud_storage_download_info()
        info = self._get_cloud_storage_google_info()
        return {
            'url': self._generate_cloud_storage_google_signed_url(info['bucket_name'], info['blob_name'], method='GET', expiration=self._cloud_storage_download_url_time_to_expiry),
            'time_to_expiry': self._cloud_storage_download_url_time_to_expiry,
        }

    def _generate_cloud_storage_upload_info(self):
        if self.env['ir.config_parameter'].sudo().get_param('cloud_storage_provider') != 'google':
            return super()._generate_cloud_storage_upload_info()
        info = self._get_cloud_storage_google_info()
        return {
            'url': self._generate_cloud_storage_google_signed_url(info['bucket_name'], info['blob_name'], method='PUT', expiration=self._cloud_storage_upload_url_time_to_expiry),
            'method': 'PUT',
            'response_status': 200,
        }

    def migrate_attachments_to_gcs(self):
        for rec in self:
            try:
                rec.write({
                    'raw': False,
                    'type': 'cloud_storage',
                    'url': rec._generate_cloud_storage_url(),
                })

                upload_info = rec._generate_cloud_storage_upload_info()
                upload_url = upload_info.get('url')

                file_data = base64.b64decode(rec.datas)
                response = requests.put(upload_url, data=file_data, headers={
                    'Content-Type': rec.mimetype or 'application/octet-stream'
                })

                if response.status_code != 200:
                    raise UserError(f"Error al subir {rec.name}: {response.status_code} - {response.text}")

                rec.write({
                    'url': rec.url.replace(
                        "https://storage.googleapis.com",
                        "https://storage.cloud.google.com"
                    ),
                    'datas': False,
                })

            except Exception as e:
                raise UserError(f"Error en {rec.name} (ID {rec.id}): {str(e)}")

    """def migrate_attachments_to_gcs(self):
        for rec in self:
            try:
                file_data = b''
                if rec.datas:
                    file_data = base64.b64decode(rec.datas)
                elif rec.store_fname:
                    filestore = tools.config.filestore(self.env.cr.dbname)
                    file_path = os.path.join(filestore, rec.store_fname)
                    if os.path.exists(file_path):
                        with open(file_path, 'rb') as f:
                            file_data = f.read()

                if not file_data:
                    raise UserError(f"Archivo vacío o no encontrado: {rec.name}")

                # 🔹 Generamos la URL del Cloud Storage (sin tocar el tipo aún)
                upload_url_base = rec._generate_cloud_storage_url()

                # 🔹 Forzamos tipo antes de generar signed URL
                rec.write({
                    'raw': False,
                    'type': 'cloud_storage',
                    'url': upload_url_base,
                })

                upload_info = rec._generate_cloud_storage_upload_info()
                upload_url = upload_info.get('url')

                # 🔹 Subimos a GCS con headers completos
                response = requests.put(
                    upload_url,
                    data=file_data,
                    headers={
                        'Content-Type': rec.mimetype or 'application/octet-stream',
                        'Content-Length': str(len(file_data)),
                    },
                )

                if response.status_code != 200:
                    raise UserError(f"Error al subir {rec.name}: {response.status_code} - {response.text}")

                # 🔹 Reescribimos la URL final (versión visual cloud.google)
                rec.write({
                    'url': rec.url.replace(
                        "https://storage.googleapis.com",
                        "https://storage.cloud.google.com"
                    ),
                    'datas': False,  # limpia el binario local
                })

            except Exception as e:
                raise UserError(f"Error en {rec.name} (ID {rec.id}): {str(e)}")"""
