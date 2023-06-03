from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment, OrderRefund
from pretix.celery_app import app


@app.task
@scopes_disabled()
def check_payment(payment_id: int):
    p = OrderPayment.objects.get(pk=payment_id)
    p.payment_provider._update_payment(p)


@app.task
@scopes_disabled()
def check_refund(refund_id: int):
    r = OrderRefund.objects.get(pk=refund_id)
    r.payment_provider._update_refund(r)
