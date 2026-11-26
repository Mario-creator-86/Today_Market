# Corecții după auditul logicii

Aplicate și verificate la 29 septembrie 2026, aproximativ 20:46 Europe/Bucharest.

## Rezultat

1. Confirmarea FAVORED/PRESSURED cere o schimbare curentă validă, peste pragul configurat. O cotație expirată, contextul istoric sau o decizie salvată nu o înlocuiesc.
2. Regimul live și interpretarea direcției folosesc același filtru de cotații: FRESH, fără stub, clasă LIVE și fără sesiune explicit închisă. Cotațiile valide care au numai nivel sunt păstrate pentru indicatori precum VIX.
3. Reutilizarea textului AI verifică separat identitatea dovezilor și cifrele citate. Cifrele sunt legate de instrumentul și contextul lor; schimbarea lor activează rezumatul determinist actual. Cadența cererilor AI nu este accelerată. Textele numerice vechi, fără identitatea dovezilor, sunt înlocuite conservator.
4. Autoritatea sursei rămâne în tier_weight. Importanța neanalizată este null, cu baza not assessed, nu 10/10. Importanța evaluată continuă să folosească formula existentă tier × extractor.
5. WHY include blocajele, măsurătorile neutre și datele lipsă. Funcționează și pentru decizii deja salvate, fără rescrierea lor. Marcajele vizuale sunt neutre, deoarece lipsurile nu sunt confirmări pozitive.

## Fișiere

- Logică: `backend/regime.py`, `backend/event_risk.py`, `backend/interpretation.py`, `backend/service.py`, `backend/scoring.py`.
- Afișare: `frontend/app/globals.css`.
- Regresii noi: `tests/test_logic_audit_fixes.py` (12 cazuri).
- Contracte și fixture-uri actualizate: `tests/test_ai_volume.py`, `tests/test_dedupe.py`, `tests/test_ui_audit_be1.py`, `tests/test_scoring.py`, `tests/test_event_aware_cases.py`, `scripts/export_event_fixture.py`, `frontend/tests/fixtures/event-risk.real.json`.
- Verificare independentă: `artifacts/audit_market_logic.py`.
- Captură: `artifacts/logic-fixes-wait.png`.

## Verificare

- Backend complet: **1.023 teste trecute**; trei avertismente de depreciere preexistente.
- Frontend: **294 teste unitare trecute**.
- Browser: **33 teste trecute**, inclusiv desktop și mobil.
- Ruff, mypy pe cele cinci module backend și frontend lint: trecute.
- Scriptul de audit: trecut, fără acces la furnizori sau scrieri în baza de date.
- API real după repornire: 74 evenimente cu importanță necunoscută și zero evenimente neanalizate promovate automat la scor maxim în snapshotul verificat. Cardurile WAIT conțin blocajele și motivele din component_detail.
- Dashboard verificat în browser; fără erori sau avertismente în consolă.

Backendul a fost repornit pentru a încărca modificările. Nu au fost modificate credențiale, conexiuni, integrarea X sau bugetele AI. Lipsa unei confirmări continuă să păstreze WAIT; aceste corecții nu garantează profitabilitatea semnalelor.
