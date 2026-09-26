# FIFO Customer Payment Reconcile (`xe_fifo_payment_reconcile`)

Aplica cada pago de cliente liberado (`account.payment`) contra las
facturas abiertas más antiguas del mismo contacto comercial, en orden
FIFO, hasta agotar el pago. Corre en segundo plano, un lote por ejecución
de la acción planificada, y encadena los lotes con `ir.cron._trigger()`.

Caso inicial: MERCADO LIBRE (XE Brands), cuyos depósitos semanales cubren
más de 1,000 facturas. La lógica no depende de `xe_meli_connector` y sirve
para cualquier contacto.

## Solo PUE (regla fija)

El proceso concilia **únicamente facturas PUE**. Siempre se excluyen:

- facturas sin forma de pago (`l10n_mx_edi_payment_method_id` vacío), y
- facturas con forma de pago `99` (Por definir), la que usan las PPD.

Conciliar facturas PPD obligaría a emitir complementos de pago (CFDI de
pago), lo que queda fuera de alcance. No es un filtro configurable.

## Configuración

1. **Grupo:** asignar "Release payments for FIFO reconcile" (Liberar pagos
   para conciliación FIFO) a los usuarios que liberarán pagos. El grupo no
   tiene categoría: se asigna desde la pestaña de permisos técnicos del
   usuario (modo desarrollador). La restricción es solo de vista.
2. **Contacto:** activar "FIFO Auto-Reconcile" (Conciliación FIFO
   automática) en el contacto comercial, pestaña Facturación, grupo
   Asientos contables.
3. **Tamaño de lote:** parámetro de sistema
   `xe_fifo_payment_reconcile.fifo_reconcile_batch_size` (facturas por
   lote, valor inicial 250). Si falta, no es entero o es ≤ 0, se usa 250.

## Uso

En un pago publicado, no conciliado, de un contacto con la bandera
activa, el usuario del grupo activa "Release for FIFO Reconcile". El pago
se guarda al activarlo, pasa a **Pendiente** y el proceso arranca de
inmediato.

| Situación | Estado | Booleano |
|---|---|---|
| Liberado, esperando lote | Pendiente | Activo |
| Quedan saldo y facturas elegibles | En proceso | Activo |
| El pago se agota | Terminado | Se apaga |
| Queda saldo sin facturas elegibles | Terminado con saldo | Se apaga |
| Excepción real (lote revertido) | Error | Se apaga |
| Conflicto de concurrencia | Sin cambio; se reintenta en 60 s (máx. 5) | Activo |

- **Pausar:** desactivar el booleano a mitad del proceso. El pago queda en
  Pendiente/En proceso y el proceso no lo toma.
- **Continuar o reintentar:** volver a activarlo. El estado regresa a
  Pendiente y el contador de reintentos se reinicia.

Cada lote publica en el chatter del pago: facturas aplicadas (y líneas),
monto aplicado, primera y última factura, factura parcial, residual
restante y tiempo del lote.

## Cómo funciona un lote

1. Toma el pago liberado más antiguo (`date`, `id`).
2. Valida que la moneda del pago sea la de la compañía y que el pago tenga
   una sola línea por cobrar abierta.
3. Busca hasta N facturas de cliente publicadas, no pagadas o parciales,
   de la misma compañía, contacto comercial y moneda, con forma de pago
   distinta de 99, ordenadas por `invoice_date`, `sequence_number`, `id`.
4. Recorre sus líneas por cobrar abiertas (`reconciled = False`) por
   vencimiento y se detiene al agotar el pago: las que caben completas y
   la primera que no cabe (parcial).
5. Concilia en dos llamadas (completas, luego parcial), verifica
   invariantes y, al salir del savepoint, relee de la BD que la
   conciliación persistió.

## Antes de instalar en una base con el prototipo (`updates17`)

- Acción planificada 218 y acción automatizada 139: deben estar
  desactivadas (ya lo están).
- Borrar el parámetro huérfano `xe_meli_connector.fifo_reconcile_batch_size`.
- Los campos `x_`, vistas y grupo del prototipo pueden retirarse después
  de validar el módulo.

## Interacciones conocidas

- **Refacturación de `xe_meli_connector`:** reconcilia el mismo pago
  contra la factura nueva. Puede causar conflictos de concurrencia
  (cubiertos por los reintentos) y un pago Terminado puede recuperar saldo;
  en ese caso se reactiva a mano.
- **Notas de crédito posteriores:** si una factura ya se pagó por FIFO,
  su nota de crédito queda como saldo a favor abierto.
