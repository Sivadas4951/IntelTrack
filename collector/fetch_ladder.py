import asyncio
import json
import uuid
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from collector.compliance import compliance_guard, USER_AGENT
from core.ledger import record_snapshot

class FetchLadder:
    def __init__(self, source_id: str, url: str):
        self.source_id = source_id
        self.url = url

    async def execute_ladder((self) -> dict:
        if not compliance_guard.is_allowed_by_robots(self.url):
            raise PermissionError(f"Scrape blocked by robots.txt for URL: {self.url}")

        compliance_guard.enforce_rate_limit(self.url)

        # Rung 1: Fast HTTP (httpx)
        rung1_result = await self._rung_1_fast_http()
        if rung1_result and len(rung1_result.get("text", "").strip()) > 300:
            return await self._save_capture(rung1_result["text"], None, rung="rung_1_http")

        # Rung 2 & 3: Playwright (XHR Interception + Rendered DOM + Screenshot)
        rung2_3_result = await self._rung_2_3_playwright()
        
        # Rung 4: Vision Fallback check
        raw_text = rung2_3_result["text"]
        requires_vision = len(raw_text.strip()) < 150

        snapshot_meta = await self._save_capture(
            raw_text=raw_text,
            screenshot_bytes=rung2_3_result["screenshot_bytes"],
            rung=rung2_3_result["rung"]
        )
        snapshot_meta["requires_vision"] = requires_vision
        snapshot_meta["xhr_payloads"] = rung2_3_result["xhr_payloads"]
        return snapshot_meta

    async def _rung_1_fast_http() -> dict | None:
        headers = {"User-Agent": USER_AGENT}
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                resp = await client.get(self.url, headers=headers)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for tag in soup(["script", "style", "nav", "footer"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)
                    return {"text": text, "rung": "rung_1_http"}
        except Exception:
            pass
        return None

    async def _rung_2_3_playwright() -> dict:
        xhr_payloads = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            # Rung 2: XHR/JSON Interception listener
            async def handle_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if "application/json" in ct and response.status == 200:
                        json_data = await response.json()
                        xhr_payloads.append({"url": response.url, "data": json_data})
                except Exception:
                    pass

            page.on("response", handle_response)

            # Navigate for Rung 3 Rendered DOM
            await page.goto(self.url, wait_until="networkidle", timeout=15000)
            
            # Clean up scripts/styles via JS evaluation
            await page.evaluate("""
                () => {
                    const elements = document.querySelectorAll('script, style, iframe');
                    elements.forEach(el => el.remove());
                }
            """)

            dom_text = await page.inner_text("body")
            
            # Combine XHR Payload data with text if available
            if xhr_payloads:
                dom_text += "\n[INTERCEPTED_XHR_JSON]\n" + json.dumps(xhr_payloads)

            snapshot_id = f"snap_{uuid.uuid4().hex[:10]}"
            screenshot_path = f"data/screenshots/{snapshot_id}.png"
            screenshot_bytes = await page.screenshot(full_page=True)

            await browser.close()

            used_rung = "rung_2_xhr" if xhr_payloads else "rung_3_playwright_dom"
            return {
                "text": dom_text,
                "screenshot_bytes": screenshot_bytes,
                "screenshot_path": screenshot_path,
                "xhr_payloads": xhr_payloads,
                "rung": used_rung
            }

    async def _save_capture(self, raw_text: str, screenshot_bytes: bytes | None, rung: str) -> dict:
        snapshot_id = f"snap_{uuid.uuid4().hex[:10]}"
        screenshot_path = f"data/screenshots/{snapshot_id}.png" if screenshot_bytes else None

        meta = record_snapshot(
            snapshot_id=snapshot_id,
            source_id=self.source_id,
            raw_text=raw_text,
            screenshot_bytes=screenshot_bytes,
            screenshot_path=screenshot_path,
            origin="live"
        )
        meta["rung"] = rung
        return meta