# -*- coding: utf-8 -*-
"""test_computer_use_e2e.py — E2E tests for Computer Use with REAL Chromium.

These tests launch a real Chromium browser against a local ERP fixture.
No mocks. No Playwright fakes. Real browser, real HTTP, real DOM.
"""
import pytest


pytestmark = pytest.mark.computer_use_e2e


@pytest.fixture(scope='module')
def erp_fixture():
    """Start local ERP fixture server."""
    from tests.fixtures.erp_simulator import start_erp_server, ERPHandler
    ERPHandler.INVOICES = []
    ERPHandler.SESSIONS = {}
    server = start_erp_server(port=18765)
    yield 'http://127.0.0.1:18765'
    server.shutdown()
    server.server_close()


@pytest.fixture()
def browser_page(erp_fixture):
    """A real page whose browser is always closed, including on assertion failure."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(erp_fixture)
        try:
            yield page
        finally:
            browser.close()


class TestComputerUseE2E:
    """Tests that open real Chromium against the ERP fixture."""

    def test_import_playwright(self):
        from playwright.async_api import async_playwright
        assert async_playwright is not None

    def test_launch_and_close_browser(self, browser_page):
        assert 'Login' in browser_page.title() or 'ERP' in browser_page.title()

    def test_login_wrong_password_shows_error(self, browser_page):
        page = browser_page
        page.fill('#username', 'admin')
        page.fill('#password', 'wrong')
        page.click('#login-submit')
        page.wait_for_load_state('networkidle')
        error = page.query_selector('#error')
        assert error is not None, 'Wrong password must show error element'
        assert 'incorrecta' in error.text_content().lower() or 'error' in error.text_content().lower()

    def test_login_correct_navigates_to_dashboard(self, browser_page):
        page = browser_page
        page.fill('#username', 'admin')
        page.fill('#password', 'correct')
        page.click('#login-submit')
        page.wait_for_load_state('networkidle')
        assert '/dashboard' in page.url
        title = page.query_selector('#dashboard-title')
        assert title is not None, 'Dashboard must have title element'
        assert 'Dashboard' in title.text_content()

    def test_navigate_to_facturas(self, browser_page):
        page = browser_page
        page.fill('#username', 'admin')
        page.fill('#password', 'correct')
        page.click('#login-submit')
        page.wait_for_load_state('networkidle')
        page.click('a[href="/facturas"]')
        page.wait_for_load_state('networkidle')
        title = page.query_selector('#facturas-title')
        assert title is not None, 'Facturas page must have title'
        assert 'Factura' in title.text_content()

    def test_register_invoice_appears_in_grid(self, browser_page):
        page = browser_page
        page.fill('#username', 'admin')
        page.fill('#password', 'correct')
        page.click('#login-submit')
        page.wait_for_load_state('networkidle')
        page.click('a[href="/facturas"]')
        page.wait_for_load_state('networkidle')
        page.fill('#folio-input', 'TEST-001')
        page.fill('#emisor-input', 'Empresa Test SA')
        page.fill('#total-input', '15000.00')
        page.click('#register-submit')
        page.wait_for_load_state('networkidle')
        grid = page.query_selector('#invoice-grid')
        assert grid is not None
        content = grid.text_content()
        assert 'TEST-001' in content, 'Registered invoice must appear in grid'
        assert 'Empresa Test' in content

    def test_unauthenticated_redirects_to_login(self, browser_page, erp_fixture):
        page = browser_page
        page.goto(erp_fixture + '/dashboard')
        page.wait_for_load_state('networkidle')
        # Should redirect to login page
        content = page.content().lower()
        assert 'login' in content or 'iniciar' in content or page.url.endswith('/')

    def test_unauthenticated_facturas_redirects(self, browser_page, erp_fixture):
        page = browser_page
        page.goto(erp_fixture + '/facturas')
        page.wait_for_load_state('networkidle')
        content = page.content().lower()
        assert 'login' in content or 'iniciar' in content or page.url.endswith('/')

    def test_contpaqi_real_driver_fills_and_verifies_form(self, erp_fixture):
        """Exercise the project driver itself, not just Playwright primitives."""
        from b2b_ai.computer_use.contpaqi_real_driver import CONTPAQiRealDriver

        driver = CONTPAQiRealDriver(erp_url=erp_fixture, headless=True)
        try:
            connected = driver.connect()
            assert connected.ok is True, connected
            login = driver.login({"usuario": "admin", "password": "correct"})
            assert login.ok is True, login
            result = driver.register_invoice({
                "folio_fiscal": "DRIVER-001",
                "emisor_rfc": "XAXX010101000",
                "total": 1234.56,
            })
            assert result.ok is True, result
            assert result.data["registro"]["saved"] is True
            assert result.data["registro"]["grid_verified"] is True
        finally:
            driver.close()
