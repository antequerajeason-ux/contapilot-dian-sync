from __future__ import annotations

import base64
import os
import uuid
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.dian_browser import DianSyncService

app = FastAPI(title="ContaPilot DIAN Sync Service", version="0.1.0")

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SESSIONS: Dict[str, Dict[str, Any]] = {}


class TestTokenIn(BaseModel):
    token_url: str = Field(..., description="URL AuthToken enviada por DIAN")


class SyncIn(BaseModel):
    token_url: str
    company_nit: str
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="YYYY-MM-DD. Si no se envía, usa hoy")
    max_documents: int = 25
    headless: bool = True
    # Si se envían, el microservicio sube automáticamente los ZIP/XML a ContaPilot.
    contapilot_upload_url: Optional[str] = None
    contapilot_bearer_token: Optional[str] = None


class RemoteStartIn(BaseModel):
    token_url: str
    company_nit: str
    start_date: str
    end_date: Optional[str] = None
    max_documents: int = 25


class RemoteSyncIn(BaseModel):
    session_id: str
    company_nit: str
    start_date: str
    end_date: Optional[str] = None
    max_documents: int = 25
    contapilot_upload_url: Optional[str] = None
    contapilot_bearer_token: Optional[str] = None


@app.get("/health")
def health():
    return {"ok": True, "service": "contapilot-dian-sync"}


@app.post("/test-token")
async def test_token(data: TestTokenIn):
    try:
        service = DianSyncService(headless=True)
        result = await service.test_token(data.token_url)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/sessions/start")
async def start_remote_session(data: RemoteStartIn):
    try:
        service = DianSyncService(headless=False)
        browser, context, page = await service._open_session(data.token_url)
        session_id = str(uuid.uuid4())
        live_url = None
        live_url_id = None
        try:
            cdp = await context.new_cdp_session(page)
            live = await cdp.send("Browserless.liveURL", {"timeout": 900000})
            live_url = live.get("liveURL")
            live_url_id = live.get("liveURLId")
        except Exception as live_exc:
            # Si Browserless/liveURL no está disponible, la sesión queda creada pero sin link visual.
            live_url = None
        SESSIONS[session_id] = {
            "service": service, "browser": browser, "context": context, "page": page,
            "company_nit": data.company_nit, "start_date": data.start_date, "end_date": data.end_date,
            "max_documents": data.max_documents, "live_url": live_url, "live_url_id": live_url_id
        }
        return {
            "ok": True,
            "session_id": session_id,
            "current_url": page.url,
            "live_url": live_url,
            "note": "Abre live_url para resolver captcha/entrar a DIAN. Luego vuelve a ContaPilot y presiona Continuar sincronización."
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sessions/{session_id}")
async def get_remote_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "Sesión no encontrada o expirada")
    page = s["page"]
    return {"ok": True, "session_id": session_id, "url": page.url, "live_url": s.get("live_url")}


@app.post("/sessions/sync")
async def sync_remote_session(data: RemoteSyncIn):
    s = SESSIONS.get(data.session_id)
    if not s:
        raise HTTPException(404, "Sesión no encontrada o expirada")
    try:
        service: DianSyncService = s["service"]
        return await service.sync_current_page(
            page=s["page"], context=s["context"], company_nit=data.company_nit,
            start_date=data.start_date, end_date=data.end_date, max_documents=data.max_documents,
            upload_url=data.contapilot_upload_url, bearer_token=data.contapilot_bearer_token
        )
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))


@app.post("/sessions/{session_id}/close")
async def close_remote_session(session_id: str):
    s = SESSIONS.pop(session_id, None)
    if not s:
        return {"ok": True, "closed": False}
    try:
        await s["browser"].close()
    except Exception:
        pass
    return {"ok": True, "closed": True}


@app.post("/sync")
async def sync(data: SyncIn):
    """
    Sincroniza facturas recibidas desde el portal DIAN usando el enlace AuthToken.

    Nota: DIAN no expone una API pública simple aquí; este servicio usa Playwright para crear
    una sesión de navegador y luego llama los endpoints internos descubiertos:
    - /Document/GetDocumentsPageToken
    - /Document/DownloadZipFiles?trackId=...&captcha=...
    """
    try:
        service = DianSyncService(headless=data.headless)
        result = await service.sync_received_documents(
            token_url=data.token_url,
            company_nit=data.company_nit,
            start_date=data.start_date,
            end_date=data.end_date,
            max_documents=data.max_documents,
            upload_url=data.contapilot_upload_url,
            bearer_token=data.contapilot_bearer_token,
        )
        # Para no devolver archivos enormes, los ZIP se devuelven en base64 solo si no hay callback.
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
