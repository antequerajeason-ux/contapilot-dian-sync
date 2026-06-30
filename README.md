# ContaPilot DIAN Sync Service

Microservicio para sincronizar documentos recibidos desde el portal DIAN usando el enlace temporal `AuthToken`.

## Por qué existe este microservicio

El portal DIAN no se comporta como una API pública simple. Usa sesión, cookies, `__RequestVerificationToken`, WAF/challenge y URLs de descarga con `captcha`.

Por eso la app principal en Cloudflare Worker no debe hacer esta parte directamente. Este servicio usa Playwright para abrir una sesión de navegador real.

## Endpoints

- `GET /health`
- `POST /test-token`
- `POST /sync`

## Instalar localmente

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Mac/Linux:
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --port 8080
```

## Probar token

```bash
curl -X POST http://localhost:8080/test-token \
  -H "Content-Type: application/json" \
  -d '{"token_url":"https://catalogo-vpfe.dian.gov.co/User/AuthToken?pk=...&rk=...&token=..."}'
```

## Sincronizar

```bash
curl -X POST http://localhost:8080/sync \
  -H "Content-Type: application/json" \
  -d '{
    "token_url":"https://catalogo-vpfe.dian.gov.co/User/AuthToken?pk=...&rk=...&token=...",
    "company_nit":"901430007",
    "start_date":"2026-05-25",
    "end_date":"2026-06-23",
    "max_documents":10,
    "headless":true
  }'
```

## Enviar a ContaPilot automáticamente

Incluye:

```json
{
  "contapilot_upload_url": "https://TU_WORKER/api/companies/COMPANY_ID/upload",
  "contapilot_bearer_token": "TOKEN_DE_USUARIO"
}
```

## Despliegue recomendado

Render/Railway/Fly.io/VPS con Playwright.

En Render el build command puede ser:

```bash
pip install -r requirements.txt && playwright install chromium
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Estado

Este servicio ya implementa:

1. Abrir AuthToken.
2. Entrar a `/Document/Received`.
3. Enviar POST a `/Document/GetDocumentsPageToken`.
4. Extraer candidatos de descarga `/Document/DownloadZipFiles` si vienen en la respuesta.
5. Descargar ZIPs encontrados.
6. Enviar ZIPs a ContaPilot si se configura callback.

Si la respuesta de `GetDocumentsPageToken` no trae la URL de descarga, necesitamos copiar la pestaña `Response` de esa petición para mapear el campo exacto que contiene `trackId/captcha`.
