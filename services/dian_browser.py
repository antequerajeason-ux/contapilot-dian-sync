from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urljoin

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

DIAN_BASE = "https://catalogo-vpfe.dian.gov.co"


def validate_auth_url(token_url: str) -> Dict[str, str]:
    from urllib.parse import urlparse, parse_qs
    u = urlparse(token_url)
    if "catalogo-vpfe.dian.gov.co" not in u.netloc:
        raise ValueError("La URL no pertenece a catalogo-vpfe.dian.gov.co")
    if "/User/AuthToken" not in u.path:
        raise ValueError("La URL no es de tipo /User/AuthToken")
    qs = parse_qs(u.query)
    pk = qs.get("pk", [None])[0]
    rk = qs.get("rk", [None])[0]
    token = qs.get("token", [None])[0]
    if not pk or not rk or not token:
        raise ValueError("La URL debe contener pk, rk y token")
    return {"pk": pk, "rk": rk, "token_last4": token[-4:]}


def find_request_verification_token(html: str) -> str:
    # Token puede estar en input hidden o en scripts.
    patterns = [
        r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"',
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        r'__RequestVerificationToken["\']?\s*[:=]\s*["\']([^"\']+)',
    ]
    for p in patterns:
        m = re.search(p, html, re.I)
        if m:
            return m.group(1)
    return ""


def extract_download_candidates(obj: Any) -> List[Dict[str, str]]:
    """Busca URLs DownloadZipFiles, trackId y captcha en cualquier JSON/HTML devuelto por DIAN."""
    text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    candidates: List[Dict[str, str]] = []

    # URL completa o parcial de descarga.
    for m in re.finditer(r'(?:https://catalogo-vpfe\.dian\.gov\.co)?/Document/DownloadZipFiles\?[^"\'<>\\]+', text, re.I):
        url = m.group(0).replace("\\u0026", "&").replace("&amp;", "&")
        if url.startswith("/"):
            url = DIAN_BASE + url
        candidates.append({"url": url})

    # trackId + captcha en textos separados.
    track_ids = re.findall(r'trackId[=:\"\']+([a-f0-9]{32,})', text, re.I)
    captchas = re.findall(r'captcha[=:\"\']+([^\"\'&<>\s]+)', text, re.I)
    if track_ids:
        for i, tid in enumerate(track_ids):
            cap = captchas[i] if i < len(captchas) else (captchas[0] if captchas else "")
            if cap:
                candidates.append({"url": f"{DIAN_BASE}/Document/DownloadZipFiles?trackId={tid}&captcha={cap}", "trackId": tid, "captcha": cap})
            else:
                candidates.append({"trackId": tid})

    # Quitar duplicados
    seen = set()
    unique = []
    for c in candidates:
        key = c.get("url") or c.get("trackId")
        if key and key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


@dataclass
class DownloadedFile:
    name: str
    content: bytes
    content_type: str
    source_url: str


class DianSyncService:
    def __init__(self, headless: bool = True):
        self.headless = headless

    async def _open_session(self, token_url: str) -> tuple[Browser, BrowserContext, Page]:
        validate_auth_url(token_url)
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=self.headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari/537.36",
            locale="es-CO",
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
        )
        page = await context.new_page()
        await page.goto(token_url, wait_until="domcontentloaded", timeout=90_000)
        # Dar tiempo al challenge/WAF si aparece y a redirecciones.
        await page.wait_for_timeout(3500)
        return browser, context, page

    async def test_token(self, token_url: str) -> Dict[str, Any]:
        browser = None
        try:
            parsed = validate_auth_url(token_url)
            browser, context, page = await self._open_session(token_url)
            title = await page.title()
            url = page.url
            body_text = (await page.locator("body").inner_text(timeout=10_000))[:1000]
            ok = "Sistema de factura" in body_text or "Documentos" in body_text or "Inicio" in body_text or "LoginConfirmed" in url
            return {"ok": ok, "url": url, "title": title, "token": parsed, "preview": body_text[:500]}
        finally:
            if browser:
                await browser.close()

    async def _get_received_page(self, page: Page) -> str:
        await page.goto(f"{DIAN_BASE}/Document/Received", wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(2000)
        return await page.content()

    async def _query_documents(self, context: BrowserContext, html: str, start_date: str, end_date: Optional[str], max_documents: int) -> Dict[str, Any]:
        token = find_request_verification_token(html)
        if not token:
            # A veces el token de formulario no aparece hasta evaluar la página.
            token = ""
        end = end_date or date.today().isoformat()
        form = {
            "draw": "1",
            "start": "0",
            "length": str(max_documents),
            "DocumentKey": "",
            "SerieAndNumber": "",
            "SenderCode": "",
            "ReceiverCode": "",
            "StartDate": start_date,
            "EndDate": end,
            "DocumentTypeId": "00",
            "Status": "0",
            "IsNextPage": "false",
            "FilterType": "3",
            "blockIndex": "0",
            "RadianStatus": "0",
        }
        if token:
            form["__RequestVerificationToken"] = token
        response = await context.request.post(
            f"{DIAN_BASE}/Document/GetDocumentsPageToken",
            form=form,
            headers={
                "x-requested-with": "XMLHttpRequest",
                "origin": DIAN_BASE,
                "referer": f"{DIAN_BASE}/Document/Received",
            },
            timeout=90_000,
        )
        text = await response.text()
        try:
            data = json.loads(text)
        except Exception:
            data = {"raw": text}
        return {"status": response.status, "data": data, "raw": text, "request_verification_token_found": bool(token)}

    async def _download_candidate(self, context: BrowserContext, url: str) -> DownloadedFile:
        res = await context.request.get(url, headers={"referer": f"{DIAN_BASE}/Document/Received"}, timeout=90_000)
        content = await res.body()
        ctype = res.headers.get("content-type", "application/octet-stream")
        disp = res.headers.get("content-disposition", "")
        name = "dian_document.zip"
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp, re.I)
        if m:
            name = m.group(1).strip().strip('"')
        elif "pdf" in ctype:
            name = "dian_document.pdf"
        return DownloadedFile(name=name, content=content, content_type=ctype, source_url=url)

    async def _upload_to_contapilot(self, upload_url: str, bearer_token: str, files: List[DownloadedFile]) -> Dict[str, Any]:
        results = []
        async with httpx.AsyncClient(timeout=120) as client:
            for f in files:
                resp = await client.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {bearer_token}"},
                    files={"file": (f.name, f.content, f.content_type)},
                )
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"text": resp.text}
                results.append({"file": f.name, "status_code": resp.status_code, "response": payload})
        return {"uploads": results}

    async def sync_received_documents(
        self,
        token_url: str,
        company_nit: str,
        start_date: str,
        end_date: Optional[str] = None,
        max_documents: int = 25,
        upload_url: Optional[str] = None,
        bearer_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        browser = None
        try:
            browser, context, page = await self._open_session(token_url)
            html = await self._get_received_page(page)
            query = await self._query_documents(context, html, start_date, end_date, max_documents)
            candidates = extract_download_candidates(query["data"])

            # Si el JSON no trae URLs, intentamos extraer links del DOM luego de ejecutar búsqueda manualmente desde la página.
            if not candidates:
                candidates = extract_download_candidates(query["raw"])

            downloads: List[DownloadedFile] = []
            errors = []
            for c in candidates[:max_documents]:
                if not c.get("url"):
                    errors.append({"candidate": c, "error": "No hay URL de descarga completa; falta captcha o endpoint"})
                    continue
                try:
                    f = await self._download_candidate(context, c["url"])
                    # Ignorar HTML de error si no es zip/xml/pdf.
                    downloads.append(f)
                except Exception as exc:
                    errors.append({"candidate": c, "error": str(exc)})

            upload_result = None
            if upload_url and bearer_token and downloads:
                upload_result = await self._upload_to_contapilot(upload_url, bearer_token, downloads)

            return {
                "ok": True,
                "company_nit": company_nit,
                "query_status": query["status"],
                "token_found": query["request_verification_token_found"],
                "download_candidates": candidates,
                "downloaded": [{"name": f.name, "content_type": f.content_type, "size": len(f.content), "source_url": f.source_url} for f in downloads],
                "errors": errors,
                "upload_result": upload_result,
                "note": "Si download_candidates viene vacío, necesitamos la respuesta JSON de GetDocumentsPageToken porque la DIAN puede no incluir la URL de descarga directamente.",
            }
        finally:
            if browser:
                await browser.close()
