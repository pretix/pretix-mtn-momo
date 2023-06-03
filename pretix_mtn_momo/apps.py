from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_mtn_momo"
    verbose_name = "MTN Mobile Money"

    class PretixPluginMeta:
        name = gettext_lazy("MTN Mobile Money")
        author = "pretix team"
        description = gettext_lazy("Accept payments through MTN Mobile Money (MoMo), a popular payment method in a number of African countries.")
        visible = True
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=4.20.0"

    def ready(self):
        from . import signals  # NOQA


