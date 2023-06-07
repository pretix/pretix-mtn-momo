from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment, OrderRefund


@csrf_exempt
@scopes_disabled()
def webhook(request):
    if "payment" in request.GET:
        for op in OrderPayment.objects.filter(
            provider="mtn_momo", pk=request.GET.get("payment")
        ):
            op.payment_provider._update_payment(op)
    elif "refund" in request.GET:
        for r in OrderRefund.objects.filter(
            provider="mtn_momo", pk=request.GET.get("refund")
        ):
            r.payment_provider._update_refund(r)

    return HttpResponse("OK")
