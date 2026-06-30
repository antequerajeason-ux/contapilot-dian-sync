from __future__ import annotations

import base64
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.dian_browser import DianSyncService

app = FastAPI(title="ContaPilot DIAN Sync Service", version="0.1.0")


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
