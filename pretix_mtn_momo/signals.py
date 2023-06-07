import logging
from datetime import timedelta
from django.dispatch import receiver
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment, OrderRefund
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import periodic_task, register_payment_providers

from .tasks import check_payment, check_refund

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="payment_mtn_momo")
def register_payment_provider(sender, **kwargs):
    from .payment import MTNMoMo

    return MTNMoMo


@receiver(periodic_task, dispatch_uid="payment_mtn_momo_periodic")
@scopes_disabled()
def register_periodic_task(sender, **kwargs):
    for op in OrderPayment.objects.filter(
        provider="mtn_momo",
        state=OrderPayment.PAYMENT_STATE_PENDING,
        created__gt=now() - timedelta(days=1),
    ):
        check_payment.apply_async(args=(op.pk,))

    for r in OrderRefund.objects.filter(
        provider="mtn_momo",
        state=OrderRefund.REFUND_STATE_TRANSIT,
        created__gt=now() - timedelta(days=1),
    ):
        check_refund.apply_async(args=(r.pk,))


settings_hierarkey.add_default(
    "payment_mtn_momo_baseurl",
    value="https://sandbox.momodeveloper.mtn.com/",
    default_type=str,
)
settings_hierarkey.add_default(
    "payment_mtn_momo_environment", value="sandbox", default_type=str
)
