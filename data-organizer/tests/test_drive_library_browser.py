"""Optional browser regression check; uses an installed Chromium or Playwright browser."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


def test_search_refresh_errors_and_overlapping_requests():
    browser_api = pytest.importorskip('playwright.async_api')

    async def check():
        html = (Path(__file__).resolve().parents[1] / 'app/dashboard/static/drive-library.html').read_text(encoding='utf-8')
        async with browser_api.async_playwright() as p:
            chrome = Path(os.environ.get('PROGRAMFILES', '')) / 'Google/Chrome/Application/chrome.exe'
            options = {'executable_path':str(chrome)} if chrome.is_file() else {}
            try:
                browser = await p.chromium.launch(headless=True, **options)
            except browser_api.Error:
                pytest.skip('No installed Chromium browser')
            try:
                page = await browser.new_page()
                calls = []
                state = {'fail':False}

                async def respond(route):
                    params = parse_qs(urlsplit(route.request.url).query, keep_blank_values=True)
                    query = params['q'][0]
                    calls.append(params)
                    await asyncio.sleep(0.6 if query == 'slow' else 0.15)
                    if state['fail']:
                        await route.fulfill(status=502, content_type='text/html', body='Bad gateway')
                        return
                    data = {'total':0, 'items':[]} if query == 'missing' else {
                        'total':101, 'items':[{'company':'Alpha Medical', 'product':query or 'Initial pump',
                            'kind':'product', 'record':{'price':'120', 'source_name':'catalogue.csv'}}]}
                    try:
                        await route.fulfill(content_type='application/json', body=json.dumps(data))
                    except browser_api.Error:
                        pass  # An obsolete request can already have been cancelled.

                await page.route('https://dashboard.test/drive-library?view=catalogue',
                                 lambda route: route.fulfill(content_type='text/html', body=html))
                await page.route('https://dashboard.test/api/drive-index/**', respond)
                await page.add_init_script("sessionStorage.setItem('organizer_token','synthetic')")
                await page.goto('https://dashboard.test/drive-library?view=catalogue')
                await browser_api.expect(page.locator('#searchStatus')).to_contain_text('Search complete')
                await page.locator('#q').fill('Pump')
                # Typing searches without requiring another click.
                await browser_api.expect(page.locator('#results')).to_contain_text('Pump')
                await page.locator('#q').fill('slow')
                await page.locator('#searchButton').click()
                await browser_api.expect(page.locator('#searchStatus')).to_contain_text('Searching for')
                await page.wait_for_timeout(100)
                await page.locator('#q').fill('Latest product')
                await page.locator('#q').press('Enter')
                await browser_api.expect(page.locator('#results')).to_contain_text('Latest product')
                await page.wait_for_timeout(700)
                await browser_api.expect(page.locator('#results')).not_to_contain_text('slow')
                await browser_api.expect(page.locator('#searchStatus')).to_contain_text('Latest product')
                await page.locator('#next').click()
                await browser_api.expect(page.locator('#page')).to_contain_text('101–101')
                await page.locator('#catalogueRefresh').click()
                await browser_api.expect(page.locator('#searchStatus')).to_contain_text('Refreshing')
                await browser_api.expect(page.locator('#searchStatus')).to_contain_text('Catalogue refreshed')
                assert calls[-1]['q'] == ['Latest product'] and calls[-1]['offset'] == ['100']
                state['fail'] = True
                await page.locator('#prev').click()
                await browser_api.expect(page.locator('#searchError')).to_contain_text('request could not be completed')
                await browser_api.expect(page.locator('#page')).to_contain_text('101–101')
                await browser_api.expect(page.locator('#searchButton')).to_have_text('Search')
                await browser_api.expect(page.locator('#searchError')).to_be_in_viewport()
                state['fail'] = False
                await page.locator('#catalogueRefresh').click()
                await browser_api.expect(page.locator('#searchStatus')).to_contain_text('Catalogue refreshed')
                await browser_api.expect(page.locator('#searchError')).to_have_text('')
                await page.locator('#q').fill('missing')
                await page.locator('#searchButton').click()
                await browser_api.expect(page.locator('#results')).to_contain_text('No saved records match')
                await browser_api.expect(page.locator('#next')).to_be_disabled()
                assert all(call['kind'] == ['product'] for call in calls)
            finally:
                await browser.close()

    asyncio.run(check())
