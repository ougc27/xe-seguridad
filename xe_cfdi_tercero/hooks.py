import logging

_logger = logging.getLogger(__name__)

THIS_MODULE = "xe_cfdi_tercero"

# Metodos del estandar sobre los que se apoya el bloqueo. Si el nombre cambiara
# en una version futura de l10n_mx_edi, el override quedaria colgando sin efecto
# y el bloqueo fallaria en silencio. Se valida en la instalacion para que el
# fallo sea ruidoso y no silencioso.
REQUIRED_METHODS = (
    "_l10n_mx_edi_need_cfdi",
    "_l10n_mx_edi_cfdi_invoice_try_send",
)

OPTIONAL_METHODS = ("_l10n_mx_edi_cfdi_global_invoice_try_send",)


def _upstream_defines(env, method_name):
    """Verifica que el metodo exista en el estandar y no solo en este modulo."""
    cls = type(env["account.move"])
    owners = [k for k in cls.__mro__ if method_name in vars(k)]
    return any(
        THIS_MODULE not in (getattr(k, "__module__", "") or "") for k in owners
    )


def post_init_hook(env):
    missing = [m for m in REQUIRED_METHODS if not _upstream_defines(env, m)]
    if missing:
        raise RuntimeError(
            "xe_cfdi_tercero: los siguientes metodos de l10n_mx_edi no existen "
            "en esta version: %s. El bloqueo de timbrado NO quedaria activo. "
            "Revise los nombres contra el codigo fuente de l10n_mx_edi antes de "
            "instalar." % ", ".join(missing)
        )

    for method in OPTIONAL_METHODS:
        if not _upstream_defines(env, method):
            _logger.warning(
                "xe_cfdi_tercero: el metodo opcional %s no existe en esta "
                "version; su override no tendra efecto.",
                method,
            )

    _logger.info("xe_cfdi_tercero: bloqueo de timbrado instalado correctamente.")
