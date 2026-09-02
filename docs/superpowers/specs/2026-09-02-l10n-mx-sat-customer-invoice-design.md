# l10n_mx_sat_customer_invoice — Descarga de CFDIs emitidos (facturas de venta)

## Contexto

`l10n_mx_sat_vendor_bill` ya descarga CFDIs **recibidos** (facturas de
proveedor) vía el Web Service de Descarga Masiva del SAT (`cfdiclient`,
FIEL) y crea `account.move` (`in_invoice`/`in_refund`) automáticamente.

El usuario necesita ahora descargar CFDIs **emitidos** (las facturas de
venta que la propia empresa timbró), pero **no** quiere que se generen
facturas en Odoo a partir de ellos — el objetivo es únicamente obtener los
XML y guardarlos, para después enlazarlos manualmente a facturas ya
existentes en Odoo que aún no están timbradas. El mecanismo de enlace se
definirá en una iteración posterior (el usuario lo indicará explícitamente
cuando esté listo) y queda fuera de este spec.

Se investigó si el Web Service tiene un límite diario que afecte el
volumen (>1,100 ventas/día, ver `CLAUDE.md`): el límite de 2,000
comprobantes/día es del **portal web** del SAT, no del Web Service SOAP
que usa este módulo — el Web Service permite hasta 200,000 comprobantes
por solicitud y no tiene límite documentado de solicitudes diarias (los
únicos topes reales son el error 5003 — exceso de elementos por consulta,
ya manejado reduciendo el rango de fechas — y el 5011 — límite de
re-descargas del mismo folio/paquete en un día).

## Alcance

**Incluido:**
- Método nuevo `SatClient.request_download_emitidos()` en el módulo base
  `l10n_mx_sat` (usa `SolicitaDescargaEmitidos` de `cfdiclient`, ya
  disponible en el venv del proyecto junto a `SolicitaDescargaRecibidos`).
- Módulo nuevo `l10n_mx_sat_customer_invoice`: solicita, verifica y
  descarga CFDIs emitidos, y guarda cada XML como un registro con sus
  metadatos (UUID, RFC/nombre del receptor, folio, fecha, total, tipo).
- Cron diario (nueva solicitud) + cron horario (verificar/descargar),
  mismo patrón que vendor_bill.
- Configuración en Ajustes (fecha de inicio de sincronización, última
  sincronización, botón "Sync Now").
- Vistas, menú, seguridad (`ir.model.access.csv`) y `i18n/es.po`.

**Explícitamente fuera de este módulo:**
- Creación de `account.move` a partir de los XML descargados.
- Matching de cliente por RFC, matching de producto por `ClaveProdServ`.
- Cualquier mecanismo de enlace XML ↔ factura existente (pendiente,
  decisión futura del usuario).
- Modificar `l10n_mx_sat_vendor_bill` — el módulo nuevo es independiente,
  no comparte modelos ni vistas con él (solo el `SatClient`/FIEL del
  módulo base `l10n_mx_sat`).

## Cambio en el módulo base `l10n_mx_sat`

`services/sat_client.py` — nuevo método, análogo a `request_download`
existente pero usando `SolicitaDescargaEmitidos`:

```python
def request_download_emitidos(self, token, rfc, fecha_inicial, fecha_final, **kwargs):
    """Send a download request to the SAT for CFDIs emitidos (Descarga Masiva).

    :return: dict with keys cod_estatus, id_solicitud, mensaje
    """
    kwargs.setdefault("estado_comprobante", "Vigente")
    solicitud = SolicitaDescargaEmitidos(self._fiel)
    return solicitud.solicitar_descarga(
        token, rfc, fecha_inicial, fecha_final, **kwargs
    )
```

`verify_download` y `download_package` no cambian — son genéricos
(reciben `token`, `rfc`, `id_solicitud`/`id_paquete`, sin distinguir
emitidos/recibidos). No se toca ningún archivo de
`l10n_mx_sat_vendor_bill`.

## Modelo de datos (todo nuevo, en `l10n_mx_sat_customer_invoice`)

### `l10n_mx_sat.customer.invoice.request`

Mismos campos y state machine que `l10n_mx_sat.download.request`
(`draft → requested → processing → ready → downloading → done/error`),
reutilizando la misma estructura de `_action_request` /
`_action_verify` / `_action_download` / crons, con estas diferencias:

- `_action_request` llama a `client.request_download_emitidos(token,
  company.vat, fecha_inicial, fecha_final, rfc_emisor=company.vat,
  tipo_solicitud="CFDI", estado_comprobante="Vigente")` (en vez de
  `rfc_receptor`).
- Sin campos `move_ids` ni `pending_cfdi_ids`. En su lugar: `xml_ids`
  (`One2many` a `l10n_mx_sat.customer.invoice.xml`, inverse `request_id`).
- `_process_package` valida `Emisor.Rfc == company.vat` (en vez de
  `Receptor.Rfc`) antes de aceptar cada XML del ZIP — si no coincide, se
  descarta con el mismo log de advertencia que usa vendor_bill para el
  caso contrario.
- Reutiliza `_get_sync_companies()` con el mismo criterio (compañías con
  FIEL configurada) — no hay campo nuevo de "auto" a nivel compañía para
  esto, la sincronización aplica a cualquier compañía con FIEL.

### `l10n_mx_sat.customer.invoice.package`

Espejo exacto de `l10n_mx_sat.download.package` (`id_paquete`, `state`:
`pending`/`processed`/`error`, `request_id`).

### `l10n_mx_sat.customer.invoice.xml`

Registro "buzón" por cada CFDI emitido descargado — reemplaza la creación
de factura de vendor_bill:

- `request_id`: `Many2one`, `l10n_mx_sat.customer.invoice.request`,
  `ondelete=cascade`.
- `company_id`: `Many2one`, `res.company`.
- `uuid`: `Char`, requerido, índice. `_sql_constraints`: único por
  `(uuid, company_id)` — mismo patrón anti-duplicado que vendor_bill.
- `receptor_rfc` / `receptor_nombre`: `Char`, readonly — el cliente al
  que se le emitió el CFDI (dato informativo para cuando se busque el
  XML a enlazar; no se resuelve contra `res.partner`).
- `serie_folio`: `Char` (mismo cálculo `Serie-Folio` que vendor_bill).
- `fecha`: `Datetime` (misma lógica: `FechaTimbrado` del
  `TimbreFiscalDigital`, con fallback a `Fecha` del comprobante).
- `total`: `Monetary`, `currency_id`: `Many2one res.currency`.
- `move_type_cfdi`: `Selection([("I", "Factura"), ("E", "Nota de
  Crédito")])`, tomado de `TipoDeComprobante` (solo se aceptan I/E, igual
  que vendor_bill descarta cualquier otro tipo).
- `attachment_id`: `Many2one ir.attachment` — el XML crudo.

Sin campo de enlace a `account.move` todavía (se agrega en una iteración
futura cuando el usuario defina el mecanismo).

## Sincronización

Dos `ir.cron`, mismo patrón que `l10n_mx_sat_vendor_bill/data/ir_cron_data.xml`:

- **Diario** — `_cron_request_new()`: reenvía drafts pendientes y crea la
  siguiente ventana de fechas por compañía (misma lógica de
  `_create_next_request`: continúa desde el último `fecha_final`
  completado, o desde `l10n_mx_sat_customer_invoice_sync_from`, o 30 días
  atrás por defecto).
- **Horario** — `_cron_advance_requests()`: verifica y descarga
  solicitudes en curso.

`res.company` (y su espejo en `res.config.settings`):
- `l10n_mx_sat_customer_invoice_sync_from` (`Date`).
- `l10n_mx_sat_customer_invoice_last_sync` (`Datetime`, readonly).
- Botón "Sync Now" → `l10n_mx_sat_customer_invoice_sync_now()`, dispara
  `_cron_process_requests()` para la compañía actual (mismo texto de
  advertencia sobre qué hace "Sync from date").

## Vistas, menú y seguridad

- Lista/formulario de `l10n_mx_sat.customer.invoice.request`: igual que
  la vista de `l10n_mx_sat.download.request`
  ([l10n_mx_sat_download_request_views.xml](l10n_mx_sat_vendor_bill/views/l10n_mx_sat_download_request_views.xml))
  pero sin las pestañas "Created Bills"/"Pending CFDIs" — en su lugar una
  pestaña "XML Files" con la lista de `xml_ids` (UUID, receptor, folio,
  fecha, total, adjunto).
- Lista standalone de `l10n_mx_sat.customer.invoice.xml` (filtrable por
  RFC/nombre de receptor, fecha, folio) — es el punto de entrada para
  cuando se implemente el enlace manual a facturas.
- Menú bajo Contabilidad, junto al de "SAT Downloads" ya existente de
  vendor_bill (mismo `parent="account.account_account_menu"`).
- Setting nuevo en el bloque `l10n_mx_sat_settings` de Ajustes → Facturación
  (mismo `xpath` que usa vendor_bill sobre
  `l10n_mx_sat.res_config_settings_view_form`).
- `ir.model.access.csv`: mismo patrón que vendor_bill — 
  `account.group_account_invoice` con solo lectura,
  `account.group_account_manager` con control total, para los 3 modelos
  nuevos.

## i18n

`i18n/es.po` con todas las cadenas del módulo (botones "Verificar"/
"Descargar", mensajes de error del state machine, nombres de los cron,
etiquetas de vista, texto del setting), mismo formato/encabezado que
[l10n_mx_sat_vendor_bill/i18n/es.po](l10n_mx_sat_vendor_bill/i18n/es.po).

## Testing

Mirror de `l10n_mx_sat_vendor_bill/tests/test_download_request.py` con el
`SatClient` mockeado (sin llamadas reales al SAT):
- State machine completo: `_action_request` → `_action_verify` →
  `_action_download` (paquete `ready` → `done`, con `xml_ids` creados).
- Códigos de error del SAT (5003, 5011, 5004, `SAT_REJECT_CODES`) se
  manejan igual que en vendor_bill.
- Validación de `Emisor.Rfc == company.vat`: un XML cuyo Emisor no
  coincide con la compañía se descarta (log + no crea registro), espejo
  del test equivalente de `Receptor.Rfc` en vendor_bill.
- Deduplicación por UUID: reprocesar el mismo paquete no duplica
  `l10n_mx_sat.customer.invoice.xml`.
- `_create_next_request`: continuidad de fechas igual que vendor_bill.

Puerto de pruebas: `--http-port=8169` (ver `CLAUDE.md`, raíz del repo) para
no chocar con la instancia local del usuario en 8069.
