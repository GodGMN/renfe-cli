"""Non-interactive purchases on Renfe's legacy website."""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
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
PAYMENT_WAIT = 60000
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
            page.wait_for_url(lambda url: checkout._page_url(url) == checkout.RECEIPT, timeout=PAYMENT_WAIT)
        except BrowserTimeout:
            return "payment_unknown"
        return None
    if url.netloc not in PAYPAL_HOSTS:
        return "payment_unknown"
    if url.path.rstrip("/") not in {"/checkoutnow", "/webapps/hermes", "/cgi-bin/webscr"}:
        return "requires_action"
    verified_merchant = page.locator("#merchantName").filter(visible=True)
    displayed_total = page.locator("#cart-total, [data-testid=total-amount]").filter(visible=True)
    approve = page.get_by_role("button", name=re.compile(r"^(Pagar ahora|Pay Now|Aceptar y pagar|Agree and Pay)$", re.I))
    approve = approve.filter(visible=True)
    login = page.get_by_text(re.compile(r"^(Sign in|Log in|Iniciar sesión)$", re.I)).filter(visible=True)
    try:
        approve.or_(login).first.wait_for(state="visible", timeout=WAIT)
        if approve.count() == 0:
            return "requires_action"
        verified_merchant.first.wait_for(state="visible", timeout=WAIT)
        displayed_total.first.wait_for(state="visible", timeout=WAIT)
    except BrowserTimeout:
        return "requires_action"
    if (verified_merchant.count() != 1 or displayed_total.count() != 1 or approve.count() != 1
            or not re.fullmatch(r"Renfe(?: Viajeros(?: S\.?M\.?E\.?)?(?:,? S\.?A\.?)?)?",
                                " ".join(verified_merchant.inner_text().split()), re.I)):
        return "requires_action"
    amount = displayed_total.inner_text()
    if not re.search(r"(?:€|\bEUR\b)", amount, re.I) or cents(amount) != total:
        return "payment_unknown"
    approve.click(timeout=WAIT)
    page.wait_for_url(lambda url: checkout._page_url(url) == checkout.RECEIPT, timeout=PAYMENT_WAIT)
    return None


def _record(directory: Path, state: dict, intent: str) -> dict:
    pending = directory / "result.tmp"
    with open(pending, "x", opener=_private_file) as output:
        json.dump({**state, "intent": intent}, output)
        output.flush()
        os.fsync(output.fileno())
    pending.replace(directory / "result.json")
    return state


def _private_file(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _existing(directory: Path, intent: str) -> dict:
    try:
        result = json.loads((directory / "result.json").read_text())
    except (OSError, ValueError):
        return {"status": "payment_unknown", "purchase_id": directory.name}
    if result.pop("intent", None) != intent:
        raise UsageError("This purchase ID is already assigned to different or unverified purchase details.")
    if result.get("status") == "confirmed":
        private = directory / "ticket.pdf"
        if private.is_file():
            export = Path(result["ticket"]) if result.get("ticket") else None
            try:
                matches = export and export.is_file() and export.read_bytes() == private.read_bytes()
            except OSError:
                matches = False
            if not matches:
                result.update(ticket=str(private), ticket_status="saved_private")
        elif not result.get("unverified_ticket") or not Path(result["unverified_ticket"]).is_file():
            result.pop("ticket", None)
            result.pop("unverified_ticket", None)
            result["ticket_status"] = "unavailable"
    return result


def _export_ticket(source: Path, destination: Path) -> None:
    # Keep the verified original in private state if exporting fails. Never overwrite another file.
    with tempfile.NamedTemporaryFile(dir=destination.parent) as output, source.open("rb") as ticket:
        shutil.copyfileobj(ticket, output)
        output.flush()
        os.link(output.name, destination)


def _payment_controls(page: "Page") -> tuple["Locator", "Locator"]:
    pay = page.get_by_role("button", name=re.compile(r"^(Pagar|Comprar)$", re.I)).filter(visible=True)
    terms = page.get_by_role("checkbox", name=re.compile(r"acepto.*(?:condiciones|términos)", re.I)).filter(visible=True)
    if pay.count() != 1 or terms.count() != 1:
        raise ApiShapeError("Renfe's payment controls are not recognizable.")
    return pay, terms


def _submit(page: "Page", order: Order, total: int, directory: Path, ticket_dir: Path, intent: str) -> dict:
    from playwright.sync_api import Error as BrowserError
    from pypdf import PdfReader

    pay, terms = _payment_controls(page)
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
        return _existing(directory, intent)

    state = {"status": "payment_unknown", "purchase_id": directory.name,
             "method": order.payment.value, "total_cents": total, "currency": "EUR"}
    # Directory creation is the claim. A crashed or overlapping process must not send the charge again.
    _record(directory, state, intent)
    try:
        pay.click(timeout=WAIT)
        hosts = PAYPAL_HOSTS if order.payment == Payment.paypal else REDSYS_HOSTS
        page.wait_for_url(lambda url: checkout._page_url(url) == checkout.RECEIPT
                          or urlsplit(url).netloc in hosts, timeout=PAYMENT_WAIT)
        if checkout._page_url(page.url) != checkout.RECEIPT:
            status = _provider(page, total, order)
            if status:
                return _record(directory, {**state, "status": status}, intent)
        locator = page.locator(".localizator span").filter(visible=True)
        print_ticket = page.locator("#imprimirPDF").filter(visible=True)
        locator.first.wait_for(state="visible", timeout=WAIT)
        print_ticket.first.wait_for(state="visible", timeout=WAIT)
        if locator.count() != 1 or print_ticket.count() != 1:
            return _record(directory, state, intent)
        reference = locator.inner_text().strip()
        if not re.fullmatch(r"[A-Z0-9]{5,12}", reference):
            return _record(directory, state, intent)
        state.update(status="confirmed", locator=reference, ticket_status="unavailable")
        _record(directory, state, intent)
        with page.expect_download(timeout=WAIT) as expected:
            print_ticket.click()
        ticket = directory / "ticket.pdf"
        partial = directory / "ticket.part"
        try:
            expected.value.save_as(partial)
            partial.chmod(0o600)
            with partial.open("rb") as saved:
                resembles_pdf = b"%PDF-" in saved.read(1024)
                saved.seek(0)
                try:
                    valid_pdf = resembles_pdf and len(PdfReader(saved).pages) > 0
                except Exception:
                    # A reader failure must not discard the only candidate from a paid booking.
                    valid_pdf = False
            if valid_pdf:
                partial.replace(ticket)
                state.update(ticket=str(ticket), ticket_status="saved_private")
                destination = ticket_dir / f"renfe-{directory.name}.pdf"
                _export_ticket(ticket, destination)
                state.update(ticket=str(destination), ticket_status="saved")
            elif resembles_pdf:
                candidate = directory / "ticket-unverified.pdf"
                partial.replace(candidate)
                state.update(ticket_status="unverified", unverified_ticket=str(candidate))
        finally:
            partial.unlink(missing_ok=True)
    except (BrowserError, ApiShapeError, OSError):
        pass
    return _record(directory, state, intent)


def run(page: "Page", results: str, traveler: Traveler, order: Order,
        state_dir: Path, ticket_dir: Path, intent: str) -> dict:
    directory = state_dir / order.purchase_id if order.purchase_id else None
    if directory is not None and directory.exists():
        return _existing(directory, intent)
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
        _payment_controls(page)
        return {"status": "quoted", "total_cents": total, "currency": "EUR", "method": order.payment.value}
    return _submit(page, order, total, directory, ticket_dir, intent)


def buy(origin: Station, destination: Station, day: date, traveler: Traveler, order: Order,
        profile: Path | None, state_dir: Path, ticket_dir: Path, browser: Path | None = None) -> dict:
    if profile is None and (order.purchase_id or order.payment == Payment.saved_card):
        raise UsageError("--browser-profile is required to submit a purchase or use a saved card.")
    try:
        from playwright.sync_api import Error as BrowserError, sync_playwright
    except ImportError:
        raise UsageError("Install the checkout extra with `uv sync --extra checkout`.") from None
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (state_dir,) if profile is None else (profile, state_dir):
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UsageError("Browser profile and purchase state directories must be private (chmod 700).")
    details = json.dumps([origin.code, destination.code, day.isoformat(), asdict(traveler), asdict(order),
                          str(profile)], sort_keys=True)
    intent = hashlib.sha256(details.encode()).hexdigest()
    if order.purchase_id and (state_dir / order.purchase_id).exists():
        return _existing(state_dir / order.purchase_id, intent)
    try:
        with sync_playwright() as playwright:
            executable = str(browser) if browser else shutil.which("chromium") if profile is None else None
            context = playwright.chromium.launch_persistent_context(
                str(profile) if profile else "", channel=None if executable else "chrome", executable_path=executable,
                headless=True, chromium_sandbox=True, accept_downloads=True,
                locale="es-ES", service_workers="block",
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(WAIT)
                if order.purchase_id is None:
                    context.route("**/*", checkout._review_guard())
                results = checkout._search_results(context, origin, destination, day)
                return run(page, results, traveler, order, state_dir, ticket_dir, intent)
            finally:
                context.close()
    except BrowserError:
        raise UpstreamError("Chrome or Renfe interrupted checkout before a payment result could be verified.") from None
