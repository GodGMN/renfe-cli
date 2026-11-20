"""Non-interactive purchases on Renfe's legacy website."""

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import checkout, legacy
from .errors import ApiShapeError, UpstreamError, UsageError
from .stations import Station

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

WAIT = 15000
PAYPAL_HOSTS = {"www.paypal.com", "www.paypal.es"}
REDSYS_HOSTS = {"sis.redsys.es"}


class Payment(StrEnum):
    paypal = "paypal"
    saved_card = "saved-card"


@dataclass(frozen=True)
class Traveler:
    name: str
    surname: str
    document_type: str
    document: str
    email: str
    phone_prefix: str
    phone: str
    second_surname: str = ""

    @classmethod
    def read(cls, path: Path) -> "Traveler":
        try:
            with path.open() as file:
                mode = os.fstat(file.fileno())
                if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
                    raise UsageError("Traveler file must be owned by the current user and private (chmod 600).")
                data = json.load(file)
        except (OSError, ValueError):
            raise UsageError("Could not read the traveler JSON file.") from None
        fields = set(cls.__dataclass_fields__)
        if (not isinstance(data, dict) or set(data) - fields
                or not fields - {"second_surname"} <= data.keys()
                or any(not isinstance(value, str) or len(value) > 240 for value in data.values())):
            raise UsageError("Traveler JSON is missing required fields or contains invalid values.")
        traveler = cls(**{name: value.strip() for name, value in data.items()})
        if (not all((traveler.name, traveler.surname, traveler.document))
                or traveler.document_type not in {"DNI", "NIE", "Pasaporte/Otro"}
                or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", traveler.email)
                or not re.fullmatch(r"\+[0-9]{1,4}", traveler.phone_prefix)
                or not re.fullmatch(r"[0-9]{5,18}", traveler.phone)):
            raise UsageError("Traveler JSON has an invalid name, document, email or phone.")
        return traveler


@dataclass(frozen=True)
class Order:
    train: list[str]
    fare: str
    payment: Payment
    max_cents: int
    last4: str | None
    purchase_id: str | None


def cents(text: str) -> int:
    """Parse a displayed euro amount, accepting Spanish and English grouping."""
    match = re.fullmatch(
        r"(?:(?:EUR|€)\s*)?([0-9][0-9.,]*)(?:\s*(?:EUR|€))?",
        text.strip(), re.IGNORECASE,
    )
    if match:
        amount = match[1]
        for thousands, decimal in ((".", ","), (",", ".")):
            groups = re.escape(thousands)
            point = re.escape(decimal)
            parts = re.fullmatch(rf"([0-9]+|[1-9][0-9]{{0,2}}(?:{groups}[0-9]{{3}})+)(?:{point}([0-9]{{1,2}}))?",
                                 amount)
            if parts:
                return int(parts[1].replace(thousands, "")) * 100 + int((parts[2] or "").ljust(2, "0"))
    raise ApiShapeError("Checkout did not show a recognizable euro amount.")


def _traveler(page: "Page", traveler: Traveler) -> None:
    if checkout._page_url(page.url) != checkout.TRAVELERS:
        raise ApiShapeError("Renfe did not open the expected traveler form.")
    form = page.locator("form#formBean")
    form.locator("#tipoDocumento0").select_option(label=traveler.document_type)
    form.locator("#prefijo0").select_option(value=traveler.phone_prefix)
    for field, value in {
        "nombre0": traveler.name, "apellido10": traveler.surname, "apellido20": traveler.second_surname,
        "documento0": traveler.document, "email0": traveler.email, "telefono0": traveler.phone,
    }.items():
        form.locator(f"#{field}").fill(value)
    form.locator("#submitpersonaliza").click()

    # Some fares go through an optional extras page. Use only Renfe's own checkout and skip extras.
    page.wait_for_url(lambda url: checkout._page_url(url) in {checkout.PAYMENT, f"{legacy.BASE}/personalizaViajeEnlaces.do"},
                      timeout=WAIT)
    if checkout._page_url(page.url) != checkout.PAYMENT:
        page.get_by_role("button", name=re.compile(r"^(Continuar|Ir a pagar|Pasar al pago)$", re.I)).click()
        page.wait_for_url(lambda url: checkout._page_url(url) == checkout.PAYMENT, timeout=WAIT)


def _saved_card(page: "Page", last4: str) -> "Locator":
    saved = page.get_by_role("radio", name=re.compile(rf"(?:[*•·xX]\s*){{2,}}{last4}(?!\d)")).filter(visible=True)
    if saved.count() != 1:
        raise UsageError("The requested saved card isn't uniquely visible in Renfe checkout.")
    return saved


def _total(page: "Page", order: Order) -> int:
    amount = page.locator("#totalTrayecto").inner_text(timeout=5000)
    if not re.search(r"(?:€|\bEUR\b)", amount, re.I):
        raise ApiShapeError("Renfe did not show a euro total.")
    total = cents(amount)
    if not 0 < total <= order.max_cents:
        raise UsageError("Renfe's final price exceeds the authorized limit or is zero.")
    return total


def _provider(page: "Page", total: int, order: Order) -> str | None:
    from playwright.sync_api import TimeoutError as BrowserTimeout

    url = urlsplit(page.url)
    if url.scheme != "https":
        return "payment_unknown"
    if order.payment == Payment.saved_card:
        # Frictionless card authorization can return directly to Renfe from the gateway.
        if url.netloc not in REDSYS_HOSTS:
            return "payment_unknown"
        try:
            page.wait_for_url(lambda url: checkout._page_url(url) == checkout.RECEIPT, timeout=WAIT)
        except BrowserTimeout:
            return "requires_action"
        return None
    if url.netloc not in PAYPAL_HOSTS:
        return "payment_unknown"
    if url.path.rstrip("/") not in {"/checkoutnow", "/webapps/hermes", "/cgi-bin/webscr"}:
        return "requires_action"
    verified_merchant = page.locator("#merchantName").filter(visible=True)
    displayed_total = page.locator("#cart-total, [data-testid=total-amount]").filter(visible=True)
    approve = page.get_by_role("button", name=re.compile(r"^(Pagar ahora|Pay Now|Aceptar y pagar|Agree and Pay)$", re.I))
    if (verified_merchant.count() != 1 or displayed_total.count() != 1 or approve.count() != 1
            or not re.fullmatch(r"Renfe(?: Viajeros(?:,? S\.?A\.?)?)?", verified_merchant.inner_text().strip(), re.I)):
        return "requires_action"
    amount = displayed_total.inner_text()
    if not re.search(r"(?:€|\bEUR\b)", amount, re.I) or cents(amount) != total:
        return "payment_unknown"
    approve.click(timeout=WAIT)
    page.wait_for_url(lambda url: checkout._page_url(url) == checkout.RECEIPT, timeout=WAIT)
    return None


def _record(directory: Path, state: dict) -> dict:
    pending = directory / "result.tmp"
    with open(pending, "x", opener=_private_file) as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    pending.replace(directory / "result.json")
    return state


def _private_file(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _existing(directory: Path) -> dict:
    try:
        return json.loads((directory / "result.json").read_text())
    except (OSError, ValueError):
        return {"status": "payment_unknown", "purchase_id": directory.name}


def _submit(page: "Page", order: Order, total: int, directory: Path) -> dict:
    from playwright.sync_api import Error as BrowserError

    pay = page.get_by_role("button", name=re.compile(r"^(Pagar|Comprar)$", re.I)).filter(visible=True)
    terms = page.get_by_role("checkbox", name=re.compile(r"acepto.*(?:condiciones|términos)", re.I)).filter(visible=True)
    if pay.count() != 1 or terms.count() != 1:
        raise ApiShapeError("Renfe's payment controls are not recognizable.")
    terms.check()
    instrument = (_saved_card(page, order.last4) if order.last4 else
                  page.locator("#datosPago_cdgoFormaPago_paypal"))
    if instrument.count() != 1 or not instrument.is_checked():
        raise UpstreamError("Renfe changed the selected payment instrument.")
    if checkout._page_url(page.url) != checkout.PAYMENT or _total(page, order) != total:
        raise UpstreamError("Renfe changed the purchase before submission.")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        return _existing(directory)

    state = {"status": "payment_unknown", "purchase_id": directory.name,
             "method": order.payment.value, "total_cents": total, "currency": "EUR"}
    # Directory creation is the claim. A crashed or overlapping process must not send the charge again.
    try:
        pay.click(timeout=WAIT)
        hosts = PAYPAL_HOSTS if order.payment == Payment.paypal else REDSYS_HOSTS
        page.wait_for_url(lambda url: checkout._page_url(url) == checkout.RECEIPT
                          or urlsplit(url).netloc in hosts, timeout=WAIT)
        if checkout._page_url(page.url) != checkout.RECEIPT:
            status = _provider(page, total, order)
            if status:
                return _record(directory, {**state, "status": status})
        locator = page.locator(".localizator span").filter(visible=True)
        print_ticket = page.locator("#imprimirPDF").filter(visible=True)
        if locator.count() != 1 or print_ticket.count() != 1:
            return _record(directory, state)
        reference = locator.inner_text().strip()
        if not re.fullmatch(r"[A-Z0-9]{5,12}", reference):
            return _record(directory, state)
        state.update(status="confirmed", locator=reference)
        with page.expect_download(timeout=WAIT) as expected:
            print_ticket.click()
        ticket = directory / "ticket.pdf"
        expected.value.save_as(ticket)
        ticket.chmod(0o600)
        with ticket.open("rb") as saved:
            if saved.read(5) != b"%PDF-":
                ticket.unlink()
            else:
                state["ticket"] = str(ticket)
    except (BrowserError, ApiShapeError, OSError):
        pass
    return _record(directory, state)


def run(page: "Page", results: str, traveler: Traveler, order: Order, state_dir: Path) -> dict:
    directory = state_dir / order.purchase_id if order.purchase_id else None
    if directory is not None and directory.exists():
        return _existing(directory)
    response = page.goto(results, referer=legacy.WWW + "/", wait_until="domcontentloaded", timeout=WAIT)
    if not response or not response.ok or checkout._page_url(page.url) != legacy.RESULTS_URL:
        raise UpstreamError("Renfe did not load the booking results.")
    checkout._select_fare(page, checkout._expand_train(page, order.train), order.fare)
    _traveler(page, traveler)
    method = checkout.Method.paypal if order.payment == Payment.paypal else checkout.Method.card
    checkout._select_method(page, method)
    if order.last4:
        _saved_card(page, order.last4).check()
    total = _total(page, order)
    if directory is None:
        return {"status": "quoted", "total_cents": total, "currency": "EUR", "method": order.payment.value}
    return _submit(page, order, total, directory)


def buy(origin: Station, destination: Station, day: date, traveler: Traveler, order: Order,
        profile: Path, state_dir: Path, browser: Path | None = None) -> dict:
    try:
        from playwright.sync_api import Error as BrowserError, sync_playwright
    except ImportError:
        raise UsageError("Install the checkout extra with `uv sync --extra checkout`.") from None
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (profile, state_dir):
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UsageError("Browser profile and purchase state directories must be private (chmod 700).")
    if order.purchase_id and (state_dir / order.purchase_id).exists():
        return _existing(state_dir / order.purchase_id)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel=None if browser else "chrome", executable_path=str(browser) if browser else None,
                headless=True, chromium_sandbox=True, accept_downloads=True,
                locale="es-ES", service_workers="block",
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(WAIT)
                if order.purchase_id is None:
                    context.route("**/*", checkout._review_guard())
                results = checkout._search_results(context, origin, destination, day)
                return run(page, results, traveler, order, state_dir)
            finally:
                context.close()
    except BrowserError:
        raise UpstreamError("Chrome or Renfe interrupted checkout before a payment result could be verified.") from None
