# xe_fifo_payment_reconcile — Conciliación automática FIFO de pagos de clientes

## Contexto

Mercado Libre (`res.partner` 87659, XE Brands, compañía 1) deposita cada
semana montos grandes (p. ej. $801,256.00) que cubren más de 1,000
facturas; el contacto tiene ~19,600 facturas abiertas. Administración
registra un `account.payment` por depósito y conciliarlo a mano contra las
facturas más antiguas ya no es viable.

El diseño se validó de punta a punta en `updates17` desde la interfaz
(campos `x_`, vistas heredadas, acción automatizada 139 y acción
planificada 218, ambas ya desactivadas). Este spec lleva esa lógica a un
módulo instalable.

## Decisiones

- **Addon separado `xe_fifo_payment_reconcile`**, no dentro de
  `xe_meli_connector`: la lógica no depende del conector (no usa
  `meli_invoice_document_id` ni modelos de ML), se activa por contacto con
  una bandera genérica y se podrá reutilizar con otros clientes. Además
  evita conflictos con el conector, que está en desarrollo activo y tiene
  versiones distintas en `main` y `updates17`.
- Rama `feature/17.0-fifo-payment-reconcile` creada desde `main`; PR hacia
  `main` para revisión de Rogelio en su local. Nunca se hace push directo
  a `main`.
- Versión inicial `17.0.1.0.0`. Dependencias: `account`, `mail`,
  `l10n_mx_edi`.
- Código y cadenas en inglés, traducidas en `i18n/es.po` (consistente con
  los módulos de Rogelio). Comentarios en español.
- Sin tests automáticos: las pruebas se hacen manualmente en una rama dev
  en el local de Rogelio.
- Sin migraciones de datos. No se toca el comportamiento del conector.

## Alcance

**Incluido:** campos, grupo, vistas, cron con encadenamiento por lotes,
auditoría en chatter, parámetro de tamaño de lote, README.

**Fuera de alcance:** revertir conciliaciones, reportes/tableros, aplicar
notas de crédito, validar el grupo en `write()`, facturas PPD.

### Solo PUE (regla fija)

El proceso aplica únicamente a facturas PUE. Se excluyen las facturas sin
forma de pago y las de forma de pago `99` (Por definir, usada en PPD).
Conciliar PPD requeriría emitir complementos de pago, lo que queda fuera
de alcance. Es una constante del código, no un filtro configurable, y se
documenta en el código, en la descripción del manifest y en el README.

## Estructura

```
xe_fifo_payment_reconcile/
├── __init__.py
├── __manifest__.py
├── README.md
├── i18n/es.po
├── models/
│   ├── __init__.py
│   ├── res_partner.py
│   └── account_payment.py
├── security/fifo_reconcile_security.xml
├── data/
│   ├── ir_cron.xml
│   └── ir_config_parameter.xml
└── views/
    ├── res_partner_views.xml
    └── account_payment_views.xml
```

No hay modelos nuevos, por lo que no hace falta `ir.model.access.csv`.

## Campos

| Modelo | Campo | Tipo | Notas |
|---|---|---|---|
| `res.partner` | `fifo_auto_reconcile` | Boolean | "FIFO Auto-Reconcile". `tracking=True`, `copy=False`. |
| `account.payment` | `fifo_reconcile_release` | Boolean | "Release for FIFO Reconcile". `tracking=True`, `copy=False`. Widget `boolean_toggle`. |
| `account.payment` | `fifo_reconcile_state` | Selection, `readonly=True` | `pending`, `in_progress`, `done`, `done_residual`, `error` (Pending, In Progress, Done, Done with Residual, Error). `tracking=True`, `copy=False`. |
| `account.payment` | `fifo_partner_enabled` | Boolean related, no almacenado | `partner_id.commercial_partner_id.fifo_auto_reconcile`. Solo para `invisible` en la vista. |
| `account.payment` | `fifo_retry_count` | Integer | Reintentos consecutivos por concurrencia. Sin tracking, `copy=False`. No se muestra. |

## Seguridad

Grupo `group_fifo_reconcile_release` ("Release payments for FIFO reconcile",
traducido "Liberar pagos para conciliación FIFO"),
sin categoría, para no mezclarse con el selector de roles de Contabilidad.
La restricción es solo a nivel vista (`groups=` en el campo del
formulario); no se valida el grupo en `write()` (decisión explícita).

## Lógica de `write()` en `account.payment`

- Si `vals` trae `fifo_reconcile_release = True`: se agrega
  `fifo_reconcile_state = 'pending'` y `fifo_retry_count = 0` a los
  valores, y después del `super()` se llama a `cron._trigger()`.
  Reactivar equivale a reintentar o continuar.
- Con `boolean_toggle`, Odoo 17 guarda el pago al activarlo, así que el
  cron arranca en cuanto el usuario lo activa.
- `create()` aplica la misma regla a un pago creado ya liberado
  (importación o API).
- Pausa: si el usuario lo desmarca a mitad del proceso, el pago queda en
  `pending`/`in_progress` con el booleano apagado y el cron no lo toma.

## Cron

Una sola acción planificada en XML (`noupdate="1"`), modelo
`account.payment`, código `model._cron_fifo_reconcile()`, cada 5 minutos
como respaldo; el encadenamiento real lo hace `_trigger()`.

Cada ejecución procesa **un lote de un pago**: el más antiguo
(`date asc, id asc`) con `fifo_reconcile_release = True`,
`fifo_reconcile_state in (pending, in_progress)`, `state = 'posted'` y
contacto comercial con `fifo_auto_reconcile = True`.

Toda la operación corre con `sudo()` y `with_company(payment.company_id)`.

### Tamaño de lote

Parámetro `xe_fifo_payment_reconcile.fifo_reconcile_batch_size`, creado
con valor `250` en XML `noupdate="1"`; valor por defecto en código `250`
(se usa si el parámetro falta, no es entero o es ≤ 0). El lote cuenta
**facturas**, no líneas. Se podrá subir a 500 sin desplegar.

### Validaciones previas (error real)

- Moneda del pago = moneda de la compañía (generalización de "todo MXN").
- La línea por cobrar abierta del pago (`account_type =
  'asset_receivable'`, `not reconciled`): si no hay, el estado pasa a
  `done`; si hay más de una o su residual no es acreedor, es error.

### Facturas candidatas

Búsqueda sobre `account.move`:

- `move_type = 'out_invoice'`, `state = 'posted'`
- `company_id` = compañía del pago
- `commercial_partner_id` = contacto comercial del pago
- `currency_id` = moneda del pago
- `payment_state in ('not_paid', 'partial')`
- `l10n_mx_edi_payment_method_id != False` y su `code != '99'`
- `line_ids any [account_id = cuenta del pago, reconciled = False]`

Orden `invoice_date asc, sequence_number asc, id asc`, `limit` = tamaño de
lote. No se usa `l10n_mx_edi_payment_policy` (no almacenado; Odoo ignora
la condición sin error).

### Aplanado a nivel línea y recorrido perezoso

Se recorren las facturas en orden FIFO y, dentro de cada una, sus líneas
por cobrar abiertas en la cuenta del pago (`reconciled = False`),
ordenadas por `date_maturity` y luego `id`. El recorrido se detiene en
cuanto se agota el residual del pago; no se construye antes la lista
completa del lote.

- Criterio de línea abierta: `reconciled = False`. No se usa
  `amount_residual != 0` ni `balance != 0`: las líneas de $0.00 que
  generan términos como "1 Día" o "15 Días" nacen con `reconciled = True`
  y así quedan excluidas.
- Una factura del dominio sin líneas abiertas es error real.
- Cada línea debe tener residual deudor positivo; si no, error.
- Las líneas que caben completas en el residual van al grupo de completas;
  la primera que no cabe es la parcial y ahí se detiene.

### Conciliación (modo split, fijo)

1. `(pay_line | full_lines).reconcile()`
2. En llamada aparte, `(pay_line | partial_line).reconcile()`

No se concilia todo en una llamada: Odoo 17 no respeta el orden y el
residual de la parcial cae en la línea de mayor monto del lote.

### Invariantes (tras `flush_all()`, excepción si fallan)

- Aplicado al pago = esperado (residual previo del pago si hubo parcial;
  si no, suma de residuales de las líneas del lote).
- Aplicado al pago = suma de lo aplicado a las líneas de factura.

### Transacción y candado de persistencia

- Dos savepoints anidados (`with self.env.cr.savepoint():`), sin commits
  manuales. El interno envuelve el lote.
- Al salir del interno: `invalidate_all()`, se relee desde BD el residual
  de la línea del pago y se compara con el esperado; si difiere, es error.
- El registro del estado y el resumen del chatter de un lote exitoso van
  fuera del savepoint del lote pero dentro del externo, con `flush_all()`:
  si el candado o esa escritura fallan, se revierte también el lote (el
  mensaje "el lote se revirtió" siempre es cierto) y un choque de
  concurrencia ahí entra a la lógica de reintentos.
- El registro de errores (estado `error` y chatter) va fuera de ambos.

### Estado resultante

| Situación | Estado | Booleano | Siguiente |
|---|---|---|---|
| Pago agotado | `done` | Se apaga | — |
| Queda saldo y quedan facturas elegibles | `in_progress` | Sigue activo | `_trigger()` inmediato |
| Queda saldo y no quedan facturas elegibles | `done_residual` (sobrante en chatter) | Se apaga | — |
| Excepción real | `error` (detalle en chatter) | Se apaga | — |
| Error de concurrencia | Se mantiene | Sigue activo | `_trigger(at=+60 s)` |

Tras un lote exitoso, `fifo_retry_count` vuelve a 0.

### Errores de concurrencia

Se detectan por `pgcode` (`SerializationFailure`, `LockNotAvailable`,
`DeadlockDetected`). Bajo `REPEATABLE READ` el snapshot no cambia dentro
de la misma ejecución, así que reintentar ahí es inútil:

- Se revierte el lote (savepoint), `_logger.warning`, se incrementa
  `fifo_retry_count` y se programa `_trigger(at=now + 60 s)`.
- Sin nota en el chatter (ruido y otro punto de conflicto).
- Al superar 5 reintentos consecutivos, el pago pasa a `error` con el
  detalle en el chatter y el booleano se apaga.
- Si la escritura del contador choca a su vez, se revierte toda la
  ejecución y el pago se reintenta en la siguiente corrida sin pérdida.

## Auditoría en el chatter

Cada lote publica un mensaje HTML (`Markup`, cadenas traducibles):

- Invoices applied: N (lines: M)
- Amount applied
- First invoice / Last invoice
- Partial invoice: INV/… (due YYYY-MM-DD), residual X — o "None"
- Remaining payment residual
- Batch time

Con `done_residual` se agrega el saldo sin aplicar. Los cambios de estado
quedan en el seguimiento de los campos.

## Vistas

- `res.partner`: `fifo_auto_reconcile` en la pestaña Facturación, grupo
  `accounting_entries`, después de `property_account_payable_id`
  (hereda `account.view_partner_property_form`).
- Formulario de `account.payment` (hereda
  `account.view_account_payment_form`), dentro de `group[@name='group2']`:
  - `fifo_reconcile_release` con `widget="boolean_toggle"`,
    `groups=` del grupo, visible solo si `fifo_partner_enabled`,
    `state == 'posted'` y `not is_reconciled`.
  - `fifo_reconcile_state` como badge, visible solo con valor:
    `decoration-info` (pending, in_progress), `decoration-success` (done),
    `decoration-warning` (done_residual), `decoration-danger` (error).
- Lista de `account.payment`: columna opcional de estado, oculta por
  defecto, con las mismas decoraciones, después de `state`.
- Búsqueda de `account.payment`: filtros "FIFO: Released" y uno por
  estado, después del filtro `reconciled`; agrupación "FIFO State".

## Interacciones conocidas

- **Refacturación del conector ML:** al refacturar, el conector quita el
  pago de la factura vieja y lo concilia contra la nueva. Funciona con
  nuestro pago, pero puede causar concurrencia (cubierta por los
  reintentos) y un pago en `done` puede recuperar saldo si la factura
  nueva es menor; se reactiva a mano.
- **Notas de crédito posteriores:** si FIFO paga completa una factura y
  luego llega su nota de crédito, esta queda como saldo a favor abierto.
  Comunicado a Administración.

## Antes de instalar en una base con el prototipo (`updates17`)

- Acción planificada 218 y acción automatizada 139: ya desactivadas.
- Borrar el parámetro `xe_meli_connector.fifo_reconcile_batch_size` (132),
  que queda huérfano.
- Los campos `x_`, vistas 6164–6167 y grupo 251 del prototipo pueden
  retirarse después de validar el módulo.

## Plan de prueba (manual)

1. Activar `fifo_auto_reconcile` en el contacto de prueba.
2. Snapshot con `read_group` de la cuenta por cobrar del contacto,
   separando residual deudor (`amount_residual > 0`) y acreedor (`< 0`).
3. Registrar un pago real, liberarlo y verificar: orden FIFO, parcial
   correcta, exclusión de forma 99 y sin forma de pago, chatter y
   estados, y encadenamiento entre lotes sin tiempos muertos largos.
4. Factura con términos "30% Ahora, Balance 60 Días": se cubren sus dos
   líneas.
5. Parcial dentro de una misma factura: cae en la línea de vencimiento
   más lejano.
6. Factura con término "1 Día" y línea de $0.00: sin error.
7. Forzar un error real (p. ej. pago en moneda distinta a la de la
   compañía) y verificar `error`; reactivar y verificar el reintento.
8. Snapshot posterior: el deudor baja y el acreedor sube exactamente por
   el monto aplicado (el neto no cambia).
