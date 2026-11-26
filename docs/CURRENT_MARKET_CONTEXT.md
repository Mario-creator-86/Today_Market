# Corectarea interpretării datelor existente

Verificat la 29 septembrie 2026, aproximativ 20:19 Europe/Bucharest.

## Comportament

- Observațiile curente valide au prioritate în confirmarea direcției. Proxy-urile aprobate furnizează direcție, nu preț executabil pentru instrumentul urmărit.
- Regimul calculat din închideri istorice este etichetat explicit. Divergențele față de observațiile curente sunt expuse separat.
- Datele istorice singure nu confirmă un instrument în sesiunea curentă. Motivele complete sunt vizibile în Market view.
- Rezumatul determinist se recalculează din snapshotul curent dacă interpretarea salvată este veche sau incompatibilă. Citirea dashboardului nu scrie o analiză nouă în baza de date și nu lansează cereri AI.
- Invalidațiile sunt derivate numai din condițiile care susțin regimul activ.
- Calendarul reutilizează scenariile condiționale din motorul de evenimente pentru evenimentele din fereastra de analiză. Nu inventează consens, rezultate sau surprize numerice.

## Fișiere modificate

Backend: `backend/regime.py`, `backend/event_risk.py`, `backend/interpretation.py`, `backend/service.py`, `backend/api.py`.

Frontend: `frontend/lib/api.ts`, `frontend/lib/event-risk.ts`, `frontend/components/dashboard/regime-banner.tsx`, `frontend/components/dashboard/market-view.tsx`.

Teste: `tests/test_current_market_context.py`, `tests/test_ui_audit_be3.py`, `tests/test_event_risk_contract.py`, `frontend/lib/event-risk.test.ts`, `frontend/tests/event-aware.spec.ts`, `frontend/tests/fixtures/event-risk.real.json`.

## Verificare

- Rulare completă backend: 1.011 teste trecute; trei avertismente de depreciere existente.
- Frontend, rularea din etapa implementării: 287 teste unitare și 33 teste de browser trecute; typecheck și lint trecute.
- Ruff pe fișierele Python modificate: trecut. Mypy pe cele cinci module backend: trecut în etapa implementării.
- Aplicația locală: health HTTP 200, dashboard și calendar verificate în browser, fără erori sau avertismente în consola dashboardului.
- Captură: `artifacts/current-market-context.png`.

## Furnizori la verificare

51 online, unul cu eroare de autorizare (Massive, HTTP 403), doi încă neverificați și șapte neconfigurați. Ollama înregistrează din nou cereri reușite; bugetul local zilnic avea 378 cereri rămase din 400 la verificarea inițială. Aceste valori sunt dinamice și nu sunt cote garantate de furnizor.

Conexiunile, credențialele, integrarea X și limitele configurate nu au fost modificate. Lipsa datelor necesare păstrează deciziile WAIT; direcția unui proxy nu devine automat semnal de tranzacționare.
