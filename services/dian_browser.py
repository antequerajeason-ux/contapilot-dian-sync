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

    async def _complete_dian_role_selection(self, page: Page) -> Dict[str, Any]:
        """Después de AuthToken, DIAN puede mostrar una pantalla para seleccionar rol.
        En el HTML observado, el enlace de Administrador apunta a /User/Com.
        Esta versión intenta navegar explícitamente a /User/Com antes de entrar a documentos.
        """
        actions = []
        await page.wait_for_timeout(1200)
        html = await page.content()
        current = page.url

        # Si vemos la pantalla de rol/administrador, ir directo al endpoint que activa el rol.
        if 'Administrador' in html or 'login-wrapper' in html or '/User/Com' in html:
            try:
                await page.goto(f"{DIAN_BASE}/User/Com", wait_until="domcontentloaded", timeout=45_000)
                actions.append({"goto": f"{DIAN_BASE}/User/Com", "from": current})
                await page.wait_for_timeout(2500)
            except Exception as exc:
                actions.append({"goto_error": str(exc), "from": current})

        # Si aún queda en pantalla intermedia, intentar clics conocidos.
        for _ in range(3):
            html = await page.content()
            current = page.url
            if "/Document/" in current or "Documentos recibidos" in html or "Documentos enviados" in html:
                break
            if 'Administrador' not in html and 'login-wrapper' not in html:
                break
            clicked = False
            for selector in ['a[href="/User/Com"]', 'a[href*="/User/Com"]', 'a:has-text("Administrador")', 'button:has-text("Administrador")', 'button:has-text("Continuar")']:
                try:
                    loc = page.locator(selector).first
                    if await loc.count():
                        await loc.click(timeout=5000)
                        actions.append({"clicked": selector, "from": current})
                        clicked = True
                        await page.wait_for_timeout(2500)
                        break
                except Exception:
                    pass
            if not clicked:
                break
        return {"actions": actions, "url": page.url}

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
        await self._complete_dian_role_selection(page)
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
        await self._complete_dian_role_selection(page)
        await page.goto(f"{DIAN_BASE}/Document/Received", wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(2000)
        # Si DIAN nos devolvió a una pantalla de selección, intentamos completar y regresar.
        html = await page.content()
        if "login-wrapper" in html or ("Administrador" in html and "/Document/Received" not in page.url):
            await self._complete_dian_role_selection(page)
            await page.goto(f"{DIAN_BASE}/Document/Received", wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(2500)
        return await page.content()

    async def _query_documents(self, page: Page, start_date: str, end_date: Optional[str], max_documents: int) -> Dict[str, Any]:
        """Consulta GetDocumentsPageToken desde el contexto real del navegador.

        Hacerlo con page.evaluate(fetch) ayuda a que DIAN use las mismas cookies,
        tokens antifalsificación y contexto JS de la página real.
        """
        end = end_date or date.today().isoformat()
        payload = {
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
        result = await page.evaluate(
            """async ({payload}) => {
                const tokenEl = document.querySelector('input[name="__RequestVerificationToken"]');
                const token = tokenEl ? tokenEl.value : '';
                if (token) payload.__RequestVerificationToken = token;
                const body = new URLSearchParams(payload).toString();
                const res = await fetch('/Document/GetDocumentsPageToken', {
                    method: 'POST',
                    headers: {
                        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'x-requested-with': 'XMLHttpRequest'
                    },
                    body
                });
                const text = await res.text();
                return {status: res.status, text, tokenFound: !!token, contentType: res.headers.get('content-type') || ''};
            }""",
            {"payload": payload},
        )
        raw = result.get("text") or ""
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        return {
            "status": result.get("status"),
            "data": data,
            "raw": raw,
            "request_verification_token_found": bool(result.get("tokenFound")),
            "content_type": result.get("contentType"),
        }

    async def _dom_search_download_links(self, page: Page, start_date: str, end_date: Optional[str], max_documents: int) -> List[str]:
        """Fallback: usa la interfaz real para hacer Buscar y luego lee enlaces DownloadZipFiles del DOM."""
        end = end_date or date.today().isoformat()
        for selector, value in [
            ('input[name="StartDate"]', start_date), ('#StartDate', start_date),
            ('input[name="EndDate"]', end), ('#EndDate', end),
        ]:
            try:
                loc = page.locator(selector).first
                if await loc.count():
                    await loc.fill(value)
            except Exception:
                pass
        clicked = False
        for selector in ['text=Buscar', 'button:has-text("Buscar")', 'input[value="Buscar"]']:
            try:
                await page.locator(selector).first.click(timeout=5000)
                clicked = True
                break
            except Exception:
                pass
        if clicked:
            await page.wait_for_timeout(5000)
        links = await page.evaluate("""() => Array.from(document.querySelectorAll('a,button')).map(el => {
            const href = el.href || el.getAttribute('href') || el.getAttribute('data-url') || el.getAttribute('onclick') || '';
            return href;
        }).filter(x => x && x.includes('DownloadZipFiles'))""")
        normalized = []
        for link in links:
            link = str(link).replace('\\u0026','&').replace('&amp;','&')
            m = re.search(r'/Document/DownloadZipFiles\?[^"\'<>\)]+', link)
            if m:
                link = DIAN_BASE + m.group(0)
            if link.startswith('/Document/DownloadZipFiles'):
                link = DIAN_BASE + link
            if link.startswith('http') and link not in normalized:
                normalized.append(link)
        return normalized[:max_documents]

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
            query = await self._query_documents(page, start_date, end_date, max_documents)
            candidates = extract_download_candidates(query["data"])

            if not candidates:
                candidates = extract_download_candidates(query["raw"])

            if not candidates:
                dom_links = await self._dom_search_download_links(page, start_date, end_date, max_documents)
                candidates = [{"url": u, "source": "dom"} for u in dom_links]

            downloads: List[DownloadedFile] = []
            errors = []
            for c in candidates[:max_documents]:
                if not c.get("url"):
                    errors.append({"candidate": c, "error": "No hay URL de descarga completa; falta captcha o endpoint"})
                    continue
                try:
                    f = await self._download_candidate(context, c["url"])
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
                "page_url_after_navigation": page.url,
                "download_candidates": candidates,
                "downloaded": [{"name": f.name, "content_type": f.content_type, "size": len(f.content), "source_url": f.source_url} for f in downloads],
                "errors": errors,
                "upload_result": upload_result,
                "query_preview": query["raw"][:3000],
                "query_data_keys": list(query["data"].keys()) if isinstance(query["data"], dict) else [],
                "session_or_role_page_detected": ("login-wrapper" in query["raw"] or "Administrador" in query["raw"]),
                "note": "Si session_or_role_page_detected es true, DIAN devolvió pantalla de selección de rol/sesión en vez de JSON. El servicio intenta seleccionar Administrador automáticamente.",
            }
        finally:
            if browser:
                await browser.close()
