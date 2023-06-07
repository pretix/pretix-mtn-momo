import hashlib
import json
import logging
import phonenumbers
import requests
import uuid
from collections import OrderedDict
from django import forms
from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from phonenumber_field.formfields import PhoneNumberField
from pretix.base.forms import SecretKeySettingsField
from pretix.base.forms.questions import (
    WrappedPhoneNumberPrefixWidget,
    guess_phone_prefix_from_request,
)
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.presale.views.cart import cart_session
from urllib.parse import urljoin

from .tasks import check_payment

logger = logging.getLogger(__name__)
ENVIRONMENTS = (
    ("sandbox", "Sandbox"),
    ("mtnuganda", "MTN Uganda"),
    ("mtnghana", "MTN Ghana"),
    ("mtnivorycoast", "MTN Ivory Coast"),
    ("mtnzambia", "MTN Zambia"),
    ("mtncameroon", "MTN Cameroon"),
    ("mtnbenin", "MTN Benin"),
    ("mtncongo", "MTN Congo"),
    ("mtnswaziland", "MTN Swaziland"),
    ("mtnguineaconakry", "MTN Guinea Conakry"),
    ("mtnsouthafrica", "MTN South Africa"),
    ("mtnliberia", "MTN Liberia"),
)


def get_token(base_url, subscription_key, api_user_id, api_secret, api="collection"):
    # Then we get all the items that make up the current credentials and create a hash to detect changes
    checksum = hashlib.sha256(
        "".join([subscription_key, api_user_id, api_secret, api]).encode()
    ).hexdigest()
    cache_key_hash = f"pretix_mtn_momo_token_hash_{checksum}"
    token_hash = cache.get(cache_key_hash)

    if token_hash:
        return token_hash["access_token"]

    r = requests.post(
        urljoin(
            base_url,
            f"/{api}/token/",
        ),
        auth=(api_user_id, api_secret),
        headers={
            "Ocp-Apim-Subscription-Key": subscription_key,
        },
    )
    r.raise_for_status()
    token_hash = r.json()

    cache.set(cache_key_hash, token_hash, token_hash["expires_in"] - 120)
    return token_hash["access_token"]


class MTNMoMo(BasePaymentProvider):
    identifier = "mtn_momo"
    verbose_name = _("MTN Mobile Money")
    public_name = _("MTN Mobile Money")

    @property
    def settings_form_fields(self):
        fields = [
            (
                "baseurl",
                forms.URLField(
                    label=_("Base URL"),
                ),
            ),
            (
                "environment",
                forms.ChoiceField(
                    label=_("Environment"),
                    choices=ENVIRONMENTS,
                ),
            ),
            (
                "api_user_id",
                forms.CharField(
                    label=_("API User ID"),
                ),
            ),
            (
                "api_secret",
                SecretKeySettingsField(
                    label=_("API secret"),
                ),
            ),
            (
                "subscription_key",
                SecretKeySettingsField(
                    label=_("Collection subscription key"),
                ),
            ),
            (
                "refund_subscription_key",
                SecretKeySettingsField(
                    label=_("Disbursement API Subscription key"),
                    required=False,
                ),
            ),
        ]
        d = OrderedDict(fields + list(super().settings_form_fields.items()))
        d.move_to_end("_enabled", last=False)
        return d

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "mtn_momo", event)

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return self.settings.refund_subscription_key

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return self.settings.refund_subscription_key

    def payment_can_retry(self, payment):
        return self._is_still_available(order=payment.order)

    def shred_payment_info(self, obj: OrderPayment):
        if not obj.info:
            return
        d = obj.info_data
        d["payer"] = {"_shredded": True}
        d["_shredded"] = True
        obj.info = json.dumps(d)
        obj.save(update_fields=["info"])

    def test_mode_message(self) -> str:
        if self.settings.environment == "sandbox":
            if self.settings.test_merchant_account and self.settings.test_api_key:
                return mark_safe(
                    _(
                        "The Mobile Money plugin is operating in test mode. You can use any phone number to test, or one "
                        "of <a {args}>a few test numbers</a> to create a failed transaction. No money will actually be "
                        "transferred."
                    ).format(
                        args='href="https://momodeveloper.mtn.com/api-documentation/testing/" '
                        'target="_blank"'
                    )
                )
            return _("Mobile Money is operating in test mode.")

    def payment_control_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template("pretix_mtn_momo/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment_info,
            "payment": payment,
            "provider": self,
        }
        return template.render(ctx)

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        return bool(request.session.get("payment_mtn_momo_msisdn"))

    def payment_form(self, request: HttpRequest):
        initial = {
            k.replace("payment_%s_" % self.identifier, ""): v
            for k, v in request.session.items()
            if k.startswith("payment_%s_" % self.identifier)
        }

        if not initial.get("msisdn"):
            cs = cart_session(request)
            if cs and cs.get("contact_form_data", {}).get("phone"):
                initial["msisdn"] = "+{}.".format(
                    cs.get("contact_form_data", {}).get("phone")
                )
            else:
                phone_prefix = guess_phone_prefix_from_request(request, self.event)
                if phone_prefix:
                    initial["msisdn"] = "+{}.".format(phone_prefix)

        form = self.payment_form_class(
            data=(
                request.POST
                if request.method == "POST"
                and request.POST.get("payment") == self.identifier
                else None
            ),
            prefix="payment_%s" % self.identifier,
            initial=initial,
        )
        form.fields = {
            "msisdn": PhoneNumberField(
                label=_("Phone number"),
                required=True,
                widget=WrappedPhoneNumberPrefixWidget(),
            )
        }

        for k, v in form.fields.items():
            v._required = v.required
            v.required = False
            v.widget.is_required = False

        return form

    def payment_prepare(self, request: HttpRequest, payment: OrderPayment):
        return self.checkout_prepare(request, None)

    def checkout_prepare(self, request, cart):
        form = self.payment_form(request)
        if form.is_valid():
            request.session["payment_mtn_momo_msisdn"] = phonenumbers.format_number(
                form.cleaned_data["msisdn"], phonenumbers.PhoneNumberFormat.E164
            )
            return True
        return False

    def payment_form_render(self, request) -> str:
        template = get_template("pretix_mtn_momo/checkout_payment_form.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "form": self.payment_form(request),
        }
        return template.render(ctx)

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_mtn_momo/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "msisdn": request.session["payment_mtn_momo_msisdn"],
        }
        return template.render(ctx)

    def _update_payment(self, payment: OrderPayment):
        reference = payment.info_data["reference"]
        try:
            access_token = get_token(
                self.settings.baseurl,
                self.settings.subscription_key,
                self.settings.api_user_id,
                self.settings.api_secret,
            )
            r = requests.get(
                urljoin(
                    self.settings.baseurl, f"/collection/v1_0/requesttopay/{reference}"
                ),
                headers={
                    "X-Target-Environment": self.settings.environment,
                    "Ocp-Apim-Subscription-Key": self.settings.subscription_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
            r.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception("Could not update payment state.")
        else:
            d = r.json()

            if d["status"] == "SUCCESSFUL":
                payment.info_data = {**payment.info_data, **d}
                payment.save(update_fields=["info"])
                payment.confirm()
            if d["status"] == "FAILED" and payment.state in (
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
            ):
                payment.fail(info={**payment.info_data, **d})
            else:
                payment.info_data = {**payment.info_data, **d}
                payment.save(update_fields=["info"])

    def _update_refund(self, refund: OrderRefund):
        reference = refund.info_data["reference"]
        try:
            access_token = get_token(
                self.settings.baseurl,
                self.settings.refund_subscription_key,
                self.settings.api_user_id,
                self.settings.api_secret,
                api="disbursement",
            )
            r = requests.get(
                urljoin(
                    self.settings.baseurl, f"/disbursement/v1_0/refund/{reference}"
                ),
                headers={
                    "X-Target-Environment": self.settings.environment,
                    "Ocp-Apim-Subscription-Key": self.settings.refund_subscription_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
            r.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception("Could not update payment state.")
        else:
            d = r.json()

            if d["status"] == "SUCCESSFUL":
                refund.info_data = {**refund.info_data, **d}
                refund.save(update_fields=["info"])
                refund.done()
            if d["status"] == "FAILED" and refund.state in (
                OrderRefund.REFUND_STATE_CREATED,
                OrderRefund.REFUND_STATE_TRANSIT,
            ):
                raise PaymentException(d["reason"])
            else:
                refund.info_data = {**refund.info_data, **d}
                refund.save(update_fields=["info"])

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str:
        refid = str(uuid.uuid4())
        payment.info_data = {"reference": refid}
        payment.save(update_fields=["info"])

        try:
            access_token = get_token(
                self.settings.baseurl,
                self.settings.subscription_key,
                self.settings.api_user_id,
                self.settings.api_secret,
            )
            r = requests.post(
                urljoin(self.settings.baseurl, "/collection/v1_0/requesttopay"),
                headers={
                    "X-Callback-Url": (
                        urljoin(
                            settings.SITE_URL,
                            reverse("plugins:pretix_mtn_momo:webhook"),
                        )
                        + "?payment="
                        + str(payment.pk)
                    ),
                    "X-Reference-Id": refid,
                    "X-Target-Environment": self.settings.environment,
                    "Content-Type": "application/json",
                    "Ocp-Apim-Subscription-Key": self.settings.subscription_key,
                    "Authorization": f"Bearer {access_token}",
                },
                json={
                    "amount": str(payment.amount),
                    "currency": self.event.currency,
                    "externalId": f"{self.event.slug.upper()}-{payment.full_id}",
                    "payer": {
                        "partyIdType": "MSISDN",
                        "partyId": request.session["payment_mtn_momo_msisdn"].lstrip(
                            "+"
                        ),
                    },
                    "payerMessage": f"{self.event.slug.upper()}-{payment.full_id}",
                    "payeeNote": str(self.event.name),
                },
            )
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            payment.fail(
                info={
                    "reference": refid,
                    "error": str(e),
                }
            )
            raise PaymentException(
                _(
                    "We had trouble communicating with the payment service. Please try again and"
                    "get in touch with us if this problem persists."
                )
            )
        else:
            del request.session["payment_mtn_momo_msisdn"]
            payment.state = OrderPayment.PAYMENT_STATE_PENDING
            payment.save(update_fields=["state"])
        self._update_payment(payment)

    def execute_refund(self, refund: OrderRefund):
        refid = str(uuid.uuid4())
        refund.info_data = {"reference": refid}
        refund.save(update_fields=["info"])

        try:
            access_token = get_token(
                self.settings.baseurl,
                self.settings.refund_subscription_key,
                self.settings.api_user_id,
                self.settings.api_secret,
                api="disbursement",
            )
            r = requests.post(
                urljoin(self.settings.baseurl, "/disbursement/v2_0/refund"),
                headers={
                    "X-Callback-Url": (
                        urljoin(
                            settings.SITE_URL,
                            reverse("plugins:pretix_mtn_momo:webhook"),
                        )
                        + "?refund="
                        + str(refund.pk)
                    ),
                    "X-Reference-Id": refid,
                    "X-Target-Environment": self.settings.environment,
                    "Content-Type": "application/json",
                    "Ocp-Apim-Subscription-Key": self.settings.refund_subscription_key,
                    "Authorization": f"Bearer {access_token}",
                },
                json={
                    "amount": str(refund.amount),
                    "currency": self.event.currency,
                    "externalId": f"{self.event.slug.upper()}-{refund.full_id}",
                    "referenceIdToRefund": refund.payment.info_data["reference"],
                    "payerMessage": f"{self.event.slug.upper()}-{refund.full_id}",
                    "payeeNote": str(self.event.name),
                },
            )
            r.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception("Could not execute refund.")
            raise PaymentException(
                _(
                    "We had trouble communicating with the payment service. Please try again and"
                    "get in touch with us if this problem persists."
                )
            )
        else:
            refund.state = OrderRefund.REFUND_STATE_TRANSIT
            refund.save(update_fields=["state"])
        self._update_refund(refund)

    def payment_pending_render(
        self, request: HttpRequest, payment: OrderPayment
    ) -> str:
        if payment.state == OrderPayment.PAYMENT_STATE_PENDING:
            check_payment.apply_async(args=(payment.pk,))

        if payment.info:
            payment_info = payment.info_data
        else:
            payment_info = None
        template = get_template("pretix_mtn_momo/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
            "order": payment.order,
            "payment": payment,
            "payment_info": payment_info,
        }
        return template.render(ctx)
