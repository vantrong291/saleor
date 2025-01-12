import json
import logging

from django.contrib import auth, messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import pgettext_lazy
from django.views.decorators.csrf import csrf_exempt

from ..account.forms import LoginForm
from ..account.models import User
from ..core.utils import get_client_ip
from ..payment import ChargeStatus, TransactionKind, get_payment_gateway
from ..payment.utils import (
    create_payment,
    create_payment_information,
    gateway_process_payment,
)
from . import FulfillmentStatus
from .forms import CustomerNoteForm, PasswordForm, PaymentDeleteForm, PaymentsForm
from .models import Order
from .utils import attach_order_to_user, check_order_status

logger = logging.getLogger(__name__)

PAYMENT_TEMPLATE = "order/payment/%s.html"


def details(request, token):
    note_form = None
    orders = Order.objects.confirmed().prefetch_related(
        "lines__variant__images",
        "lines__variant__product__images",
        "fulfillments__lines__order_line",
    )
    orders = orders.select_related("billing_address", "shipping_address", "user")
    order = get_object_or_404(orders, token=token)
    if order.is_open() and not order.customer_note:
        note_form = CustomerNoteForm(request.POST or None, instance=order)
        if request.method == "POST":
            if note_form.is_valid():
                note_form.save(user=request.user)
                return redirect("order:details", token=order.token)
    fulfillments = order.fulfillments.exclude(status=FulfillmentStatus.CANCELED)
    ctx = {"order": order, "fulfillments": fulfillments, "note_form": note_form}
    return TemplateResponse(request, "order/details.html", ctx)


def payment(request, token):
    orders = Order.objects.confirmed().filter(billing_address__isnull=False)
    orders = orders.prefetch_related(
        "lines__variant__images", "lines__variant__product__images"
    )
    orders = orders.select_related("billing_address", "shipping_address", "user")
    order = get_object_or_404(orders, token=token)
    payments = order.payments.all()
    form_data = request.POST or None

    waiting_payment = payments.filter(
        is_active=True,
        charge_status=ChargeStatus.NOT_CHARGED,
        transactions__kind=TransactionKind.AUTH,
    ).first()
    if not waiting_payment:
        waiting_payment_form = None
    else:
        form_data = None
        waiting_payment_form = PaymentDeleteForm(
            None, order=order, initial={"payment_id": waiting_payment.id}
        )
    if order.is_fully_paid() or not order.billing_address:
        form_data = None
    payment_form = None
    if not order.is_pre_authorized():
        payment_form = PaymentsForm(form_data)
        # FIXME: redirect if there is only one payment
        if payment_form.is_valid():
            payment = payment_form.cleaned_data["gateway"]
            return redirect("order:payment", token=order.token, gateway=payment)
    ctx = {
        "order": order,
        "payment_form": payment_form,
        "payments": payments,
        "waiting_payment": waiting_payment,
        "waiting_payment_form": waiting_payment_form,
    }
    return TemplateResponse(request, "order/payment.html", ctx)


@check_order_status
def start_payment(request, order, gateway):
    payment_gateway, gateway_config = get_payment_gateway(gateway)
    connection_params = gateway_config.connection_params
    extra_data = {"customer_user_agent": request.META.get("HTTP_USER_AGENT")}
    with transaction.atomic():
        payment = create_payment(
            gateway=gateway,
            currency=order.total.gross.currency,
            email=order.user_email,
            billing_address=order.billing_address,
            customer_ip_address=get_client_ip(request),
            total=order.total.gross.amount,
            order=order,
            extra_data=extra_data,
        )

        if (
            order.is_fully_paid()
            or payment.charge_status == ChargeStatus.FULLY_REFUNDED
        ):
            return redirect(order.get_absolute_url())

        payment_info = create_payment_information(payment)
        form = payment_gateway.create_form(
            data=request.POST or None,
            payment_information=payment_info,
            connection_params=connection_params,
        )
        if form.is_valid():
            try:
                gateway_process_payment(
                    payment=payment, payment_token=form.get_payment_token()
                )
            except Exception as exc:
                form.add_error(None, str(exc))
            else:
                if order.is_fully_paid():
                    return redirect("order:payment-success", token=order.token)
                return redirect(order.get_absolute_url())

    client_token = payment_gateway.get_client_token(config=gateway_config)
    ctx = {
        "form": form,
        "payment": payment,
        "client_token": client_token,
        "order": order,
    }
    return TemplateResponse(request, gateway_config.template_path, ctx)


@check_order_status
def cancel_payment(request, order):
    form = PaymentDeleteForm(request.POST or None, order=order)
    if form.is_valid():
        with transaction.atomic():
            form.save()
        return redirect("order:payment", token=order.token)
    return HttpResponseForbidden()


@csrf_exempt
def payment_success(request, token):
    """Receive request from payment gateway after paying for an order.

    Redirects user to payment success.
    All post data and query strings are dropped.
    """
    url = reverse("order:checkout-success", kwargs={"token": token})
    return redirect(url)


def checkout_success(request, token):
    """Redirect user after placing an order.

    Anonymous users are redirected to the checkout success page.
    Registered users are redirected to order details page and the order
    is attached to their account.
    """
    order = get_object_or_404(Order, token=token)
    email = order.user_email
    ctx = {"email": email, "order": order}
    if request.user.is_authenticated:
        # //vantrong291 log
        # mioitlog
        # logger.info('CHECKOUT ORDER SUCCESSFULLY | PRODUCT | {} | USER | {}'.format(order.product_to_string(), str(
        #     order.user.first_name) + " " + str(order.user.last_name)))
        for product in order.product_to_list():
            product_info_log_dict = {
                "order_id": order.id,
                "event": "CHECKOUT ORDER SUCCESSFULLY - PRODUCT INFO",
                "product": product,
            }
            logger.info("CHECKOUT ORDER SUCCESSFULLY", extra=product_info_log_dict)

        for variant in order.variant_to_list():
            variant_info_log_dict = {
                "order_id": order.id,
                "event": "CHECKOUT ORDER SUCCESSFULLY - VARIANT INFO",
                "variant": variant['variant'],
                "quantity_allocated": variant['quantity_allocated'],
                "quantity": variant['quantity'],
            }
            logger.info("CHECKOUT ORDER SUCCESSFULLY", extra=variant_info_log_dict)

        for category in order.product_category_to_list():
            category_info_log_dict = {
                "order_id": order.id,
                "event": "CHECKOUT ORDER SUCCESSFULLY - CATEGORY INFO",
                "category": category,
            }
            logger.info("CHECKOUT ORDER SUCCESSFULLY", extra=category_info_log_dict)

        for type in order.product_type_to_list():
            type_info_log_dict = {
                "order_id": order.id,
                "event": "CHECKOUT ORDER SUCCESSFULLY - PRODUCT TYPE INFO",
                "type": type,
            }
            logger.info("CHECKOUT ORDER SUCCESSFULLY", extra=type_info_log_dict)

        extra_info_log_dict = {
            "order_id": order.id,
            "event": "CHECKOUT ORDER SUCCESSFULLY - EXTRA INFO",
            "product": order.product_to_list(),
            "product_category": order.product_category_to_list(),
            "user_id": order.user.id,
            "user_email": order.user_email,
            "user_address": order.user.addresses.first().parse_data(),
            "billing_address": order.billing_address.parse_data(),
            "shipping_address": order.shipping_address.parse_data(),
            "shipping_method": order.shipping_method_name,
            "total_gross_amount": float(order.total_gross_amount),
            "total_net_amount": float(order.total_net_amount),
            "discount_amount": float(order.discount_amount),
            "shipping_price_gross_amount": float(order.shipping_price_gross_amount),
            "shipping_price_net_amount": float(order.shipping_price_net_amount),
            "currency": order.currency,
            "display_gross_prices": order.display_gross_prices
        }
        # logger.info(json.dumps(log_dict))
        logger.info("CHECKOUT ORDER SUCCESSFULLY", extra=extra_info_log_dict)

        return TemplateResponse(request, "order/checkout_success.html", ctx)
    form_data = request.POST.copy()
    if form_data:
        form_data.update({"email": email})
    register_form = PasswordForm(form_data or None)
    if register_form.is_valid():
        register_form.save()
        password = register_form.cleaned_data.get("password")
        user = auth.authenticate(request=request, email=email, password=password)
        auth.login(request, user)
        attach_order_to_user(order, user)
        return redirect("order:details", token=token)
    user_exists = User.objects.filter(email=email).exists()
    login_form = LoginForm(initial={"username": email}) if user_exists else None
    ctx.update({"form": register_form, "login_form": login_form})
    return TemplateResponse(request, "order/checkout_success_anonymous.html", ctx)


@login_required
def connect_order_with_user(request, token):
    """Connect newly created order to an authenticated user."""
    try:
        order = Order.objects.get(user_email=request.user.email, token=token)
    except Order.DoesNotExist:
        order = None
    if not order:
        msg = pgettext_lazy(
            "Connect order with user warning message",
            "We couldn't assign the order to your account as the email"
            " addresses don't match",
        )
        messages.warning(request, msg)
        return redirect("account:details")
    attach_order_to_user(order, request.user)
    msg = pgettext_lazy(
        "storefront message", "The order is now assigned to your account"
    )
    messages.success(request, msg)
    return redirect("order:details", token=order.token)
