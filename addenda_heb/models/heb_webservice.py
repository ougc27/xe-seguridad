# -*- coding: utf-8 -*-
"""SOAP client for the HEB invoice reception web service
(MexicoDigitalInvoiceService).

Contract (validated against the official HEB documentation and a successful
submission in the V33 test environment):

  Endpoint    : https://recepcionfeV33.heb.com.mx:50012/MexicoDigitalInvoiceService
  Transport   : SOAP 1.2
      - Envelope namespace : http://www.w3.org/2003/05/soap-envelope
      - Content-Type: application/soap+xml; charset=UTF-8; action="<soapAction>"
        (In SOAP 1.2, the SOAPAction is included in the Content-Type header.)
  Security    : WS-Security UsernameToken
      Username + Password (PasswordText) + Nonce (Base64) + Created.
  Operations (soapAction = /MexicoDigitalInvoiceService/<operation>):
      - getMessage        -> connectivity test (returns MESSAGE_REPLY).
      - setDigitalInvoice -> invoice submission.
  Request body (namespace http://xmlns.heb.com/ei/DIGITAL_INVOICE_REQUEST):
      <SET_DIGITAL_INVOICE_REQUEST>
          <ISBUREAU>1</ISBUREAU>          <!-- 1 = direct connection -->
          <cfdi:Comprobante> ... </cfdi:Comprobante>
      </SET_DIGITAL_INVOICE_REQUEST>
  Addenda: inside <cfdi:Addenda>, the <requestForPayment> node and its child
      elements must be sent WITHOUT a prefix and with
      xmlns="http://www.sat.gob.mx/cfd/4".
  Response: SET_DIGITAL_INVOICE_RESPONSE containing an
      AckErrorApplication node:
      - documentStatus="ACCEPTED" and errorCode="INF0000"
        (VALID DOCUMENT) => Success.
      - The acknowledgment number is available in
        ReferenceNumber/referenceIdentification.
"""
from lxml import etree
from urllib.parse import urlparse

import requests
import urllib3
import base64
import os
import re
from datetime import datetime, timezone

from odoo import models, _
from odoo.exceptions import UserError


# --- Namespaces WS-Security ---
NS_WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-wssecurity-secext-1.0.xsd")
NS_WSU = ("http://docs.oasis-open.org/wss/2004/01/"
          "oasis-200401-wss-wssecurity-utility-1.0.xsd")
NS_PWD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-username-token-profile-1.0#PasswordText")
NS_NONCE_B64 = ("http://docs.oasis-open.org/wss/2004/01/"
                "oasis-200401-wss-soap-message-security-1.0#Base64Binary")

# --- Namespaces SOAP / HEB / SAT ---
NS_SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
NS_HEB_INVOICE = "http://xmlns.heb.com/ei/DIGITAL_INVOICE_REQUEST"
NS_HEB_MESSAGE = "http://xmlns.heb.com/ei/Message_Request"
NS_CFDI = "http://www.sat.gob.mx/cfd/4"

DEFAULT_TIMEOUT = 60


class HebInvoiceWS(models.AbstractModel):
    """Encapsulates SOAP communication with HEB. No data is persisted."""
    _name = "heb.invoice.ws"
    _description = "HEB - Invoice Web Service Client"

    def _get_config(self, company):
        """Reads and validates the company's HEB configuration."""
        if not company.heb_ws_url_invoice:
            raise UserError(_("Missing HEB web service URL in Settings."))

        if not company.heb_ws_username or not company.heb_ws_password:
            raise UserError(_(
                "Missing HEB web service username and/or password in the "
                "company Settings (HEB section)."
            ))

        return {
            "url": company.heb_ws_url_invoice.strip(),
            "username": company.heb_ws_username.strip(),
            "password": company.heb_ws_password,
            "isbureau": "1",
            "timeout": DEFAULT_TIMEOUT,
            "verify": bool(company.heb_ws_verify_ssl),
        }

    def _soap_action(self, url, operation):
        """SOAPAction = ruta del servicio + '/' + operacion."""
        path = (urlparse(url).path or "/MexicoDigitalInvoiceService").rstrip("/")
        return "%s/%s" % (path, operation)

    # =================================================================
    # Construccion del envelope SOAP 1.2
    # =================================================================
    def _build_security_header(self, username, password):
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        created = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return (
            '<wsse:Security xmlns:wsse="{wsse}" xmlns:wsu="{wsu}"'
            ' soap:mustUnderstand="true">'
            '<wsse:UsernameToken>'
            '<wsse:Username>{user}</wsse:Username>'
            '<wsse:Password Type="{pwd_type}">{pwd}</wsse:Password>'
            '<wsse:Nonce EncodingType="{nonce_enc}">{nonce}</wsse:Nonce>'
            '<wsu:Created>{created}</wsu:Created>'
            '</wsse:UsernameToken>'
            '</wsse:Security>'
        ).format(wsse=NS_WSSE, wsu=NS_WSU, user=username, pwd_type=NS_PWD_TEXT,
                 pwd=password, nonce_enc=NS_NONCE_B64, nonce=nonce, created=created)

    def _build_envelope(self, username, password, body_inner):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="{soap}">'
            '<soap:Header>{header}</soap:Header>'
            '<soap:Body>{body}</soap:Body>'
            '</soap:Envelope>'
        ).format(soap=NS_SOAP12,
                 header=self._build_security_header(username, password),
                 body=body_inner)

    def _build_invoice_body(self, isbureau, comprobante_xml):
        return (
            '<dig:SET_DIGITAL_INVOICE_REQUEST xmlns:dig="{ns}">'
            '<ISBUREAU>{isbureau}</ISBUREAU>'
            '{comprobante}'
            '</dig:SET_DIGITAL_INVOICE_REQUEST>'
        ).format(ns=NS_HEB_INVOICE, isbureau=isbureau, comprobante=comprobante_xml)

    def _build_message_body(self):
        return '<msg:MESSAGE_REQUEST xmlns:msg="%s"/>' % NS_HEB_MESSAGE


    def _prepare_comprobante(self, xml_bytes):
        """Prepares the <cfdi:Comprobante> node for embedding by removing the XML declaration/BOM
        and normalizing the Addenda to the format required by HEB."""
        xml_text = (xml_bytes.decode("utf-8")
                    if isinstance(xml_bytes, bytes) else xml_bytes)
        xml_text = xml_text.lstrip("\ufeff").strip()
        xml_text = re.sub(r'^\s*<\?xml[^>]*\?>\s*', "", xml_text).strip()
        return self._normalize_addenda(xml_text)

    def _normalize_addenda(self, comprobante_xml):
        """Within <cfdi:Addenda>, removes the cfdi: prefix from the requestForPayment
        node and its child elements, and ensures the xmlns="http://www.sat.gob.mx/cfd/4"
        namespace is present. The Addenda is not covered by the SAT digital signature,
        so it can be safely rewritten."""
        match = re.search(r'(<cfdi:Addenda\s*>)(.*?)(</cfdi:Addenda\s*>)',
                          comprobante_xml, re.DOTALL)
        if not match:
            return comprobante_xml
        inner = match.group(2)
        inner = inner.replace('<cfdi:', '<').replace('</cfdi:', '</')
        if re.search(r'<requestForPayment\b', inner) and not re.search(
                r'<requestForPayment\b[^>]*\bxmlns=', inner):
            inner = re.sub(r'(<requestForPayment\b)',
                           r'\1 xmlns="%s"' % NS_CFDI, inner, count=1)
        return comprobante_xml[:match.start(2)] + inner + comprobante_xml[match.end(2):]

    def _post(self, cfg, operation, body_inner):
        if not cfg["verify"]:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        envelope = self._build_envelope(cfg["username"], cfg["password"], body_inner)
        action = self._soap_action(cfg["url"], operation)
        headers = {
            "Content-Type":
                'application/soap+xml; charset=UTF-8; action="%s"' % action,
        }
        try:
            response = requests.post(
                cfg["url"], data=envelope.encode("utf-8"), headers=headers,
                verify=cfg["verify"], timeout=cfg["timeout"])
        except requests.exceptions.RequestException as exc:
            raise UserError(
                _("Could not connect to the HEB web service.\n"
                  "URL: %s\nDetail: %s") % (cfg["url"], exc))
        # Attach the sent envelope for diagnostics (with masked password).
        response._heb_request = self._mask_password(envelope)
        return response

    def _mask_password(self, envelope):
        """Masks the UsernameToken password before exposing the SOAP request."""
        return re.sub(r'(<wsse:Password[^>]*>).*?(</wsse:Password>)',
                      r'\1********\2', envelope or "", flags=re.DOTALL)

    def _localname(self, tag):
        return tag.split("}")[-1] if "}" in tag else tag.split(":")[-1]

    def _parse_invoice_response(self, response):
        """Interprets the setDigitalInvoice response.

        Success: documentStatus="ACCEPTED" or errorCode="INF0000".

        Returns a dictionary containing:
            ok,
            accepted,
            reference,
            transaction_id,
            error_code,
            error_text,
            raw.
        """
        raw = response.text or ""
        result = {
            "status": response.status_code,
            "raw": raw,
            "ok": False,
            "reference": None,
            "error_code": None,
            "error_text": None,
        }

        # SOAP transport fault (e.g. "Unsupported Request Type").
        response_lower = raw.lower()
        if response.status_code != 200 or "<fault" in response_lower or ":fault>" in response_lower:
            result["error_text"] = self._extract_fault(raw)
            return result

        try:
            root = etree.fromstring(raw.encode("utf-8"))
        except Exception:
            result["error_text"] = _("Unable to parse the HEB response.")
            return result

        document_status, error_code, error_text, reference = None, None, None, None
        transaction_id = None

        for element in root.iter():
            name = self._localname(element.tag)

            if name == "AckErrorApplication":
                document_status = element.get("documentStatus") or document_status

            elif name == "errorCode" and element.text:
                error_code = element.text.strip()

            elif name == "text" and element.text and error_text is None:
                # <errorDescription><text>...</text></errorDescription>
                parent = element.getparent()
                if parent is not None and self._localname(parent.tag) == "errorDescription":
                    error_text = element.text.strip()

            elif name == "uniqueCreatorIdentification" and element.text:
                # Internal HEB transaction identifier
                # (inside ackErrorApplicationIdentification).
                # Useful for HEB support.
                parent = element.getparent()
                if (
                    parent is not None
                    and self._localname(parent.tag) == "ackErrorApplicationIdentification"
                ):
                    transaction_id = element.text.strip()

            elif name == "ReferenceNumber":
                for child in element.iter():
                    if (
                        self._localname(child.tag) == "referenceIdentification"
                        and child.text
                    ):
                        reference = child.text.strip()
                        break

        # Accepted only if HEB marks the document as ACCEPTED and it is not
        # explicitly marked as REJECTED.
        status_upper = (document_status or "").upper()
        accepted = status_upper == "ACCEPTED" and status_upper != "REJECTED"

        # INF0000 = "VALID DOCUMENT". Any other error code indicates rejection.
        if error_code and error_code != "INF0000":
            accepted = False
        elif error_code == "INF0000":
            accepted = accepted or status_upper != "REJECTED"

        result.update({
            "ok": bool(accepted),
            "accepted": bool(accepted),
            "document_status": document_status,
            "reference": reference,
            "transaction_id": transaction_id,
            "error_code": error_code,
            "error_text": error_text,
        })
        return result

    def _extract_fault(self, raw):
        for pattern in (
            r"<[^>]*Reason[^>]*>.*?<[^>]*Text[^>]*>(.*?)</",
            r"<[^>]*faultstring[^>]*>(.*?)</",
        ):
            match = re.search(pattern, raw or "", re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
        return (raw or "").strip()[:500] or _("Empty response.")


    def send_invoice(self, company, xml_bytes):
        """Sends the CFDI (including the Addenda) to HEB using the
        setDigitalInvoice operation and returns the parsed result."""
        cfg = self._get_config(company)
        operation = "setDigitalInvoice"
        comprobante = self._prepare_comprobante(xml_bytes)
        body = self._build_invoice_body(cfg["isbureau"], comprobante)
        response = self._post(cfg, operation, body)
        result = self._parse_invoice_response(response)
        result["operation"] = operation
        result["request"] = getattr(response, "_heb_request", "")
        return result

    def test_connection(self, company):
        """Performs a connectivity test using the getMessage operation."""
        cfg = self._get_config(company)
        response = self._post(cfg, "getMessage", self._build_message_body())
        raw = response.text or ""
        low = raw.lower()
        return {
            "operation": "getMessage",
            "status": response.status_code,
            "raw": raw,
            "request": getattr(response, "_heb_request", ""),
            "reachable": ("hello" in low or "message_reply" in low),
        }
