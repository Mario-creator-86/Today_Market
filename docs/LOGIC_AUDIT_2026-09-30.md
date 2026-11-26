# Verificarea logicii îndrumărilor — 30 septembrie 2026

Am identificat și corectat defectele de mai jos în traseul observații → scor → audit → publicare → afișare. Cele 1.023 de teste backend existente treceau înaintea intervenției; reproducerile suplimentare au demonstrat combinații pe care acestea nu le acopereau.

Verificarea acoperă comportamentul implementat și cazurile de regresie enumerate. Nu reprezintă o demonstrație a profitabilității regulilor economice sau a corectitudinii fiecărei funcții din proiect.

## Defecte corectate

| Prioritate | Comportamentul anterior și consecința | Corecția |
|---|---|---|
| P1 | Suportul și rezistența erau selectate în jurul prețului live care urma să fie verificat. Prețul rămânea astfel între limite sau elimina una dintre ele, împiedicând detectarea străpungerii în fluxul real. | Limitele sunt ancorate la ultima închidere de minut completă, respectiv la ultima închidere zilnică completă când nu există minute. Prețul live este comparat ulterior cu ele. Referința este păstrată în `levels.reference`. |
| P1 | Reacția cu orizontul cel mai lung putea proveni de la un eveniment mai vechi și putea fi combinată cu un coș al altui eveniment. | Reacția proprie și coșul trebuie să aparțină aceluiași eveniment. Fără coș, se selectează mai întâi evenimentul cel mai recent și apoi orizontul său. |
| P1 | Zero confirmări în coș produceau un scor opus direcției așteptate, inclusiv când toate instrumentele erau nemișcate. | Confirmarea contribuie cu `expected_direction × confirmation_pct`. Lipsa reacției contribuie cu zero; reacțiile efectiv divergente păstrează penalizarea proprie. |
| P1 | Unele interogări excludeau doar furnizorii cu sufix `-stub`. Marcajele stub, daily close și proxy puteau ajunge în reacții intraday, anomalii și evaluarea rezultatelor. | Același filtru SQL admite numai observațiile live eligibile, cu compatibilitate pentru înregistrările vechi fără `data_class`. Datele zilnice sunt excluse și din barele de minut folosite la decizie. |
| P1 | Un lean cu `audit_status=NOT_REQUIRED` putea rămâne direcțional după expirarea analizei, deși cotația era încă proaspătă. Motivele invalidării nu ajungeau întotdeauna în `blocks`. | Expirarea, schimbarea dovezilor, indisponibilitatea furnizorului și pragul de invalidare sunt verificate pentru toate deciziile direcționale. Motivele sunt furnizate în `reasons` și `blocks`; înregistrarea istorică rămâne intactă. |
| P1 | Explicația și verdictul AI puteau fi reutilizate când aceleași numere apăreau în alte câmpuri, cu alt regim sau alt sens al evenimentului. | Reutilizarea cere aceeași configurație și un hash al câmpurilor, valorilor și contextului analizat. Numai timpul și vârsta cotației sunt excluse din această comparație. |
| P1 | Prețul sau dovezile se puteau schimba în timpul ultimului apel asincron către auditor, după ultima verificare efectivă. | După răspunsul auditorului sunt verificate din nou cotația, confirmarea tehnică, expirarea, dovezile și invalidarea. Auditul determinist este repetat înainte de salvare; un context respins nu intră în cache. |
| P1 | Un card putea păstra `CONDITIONAL LONG` după ce decizia de bază devenea WAIT, BLOCK sau avea sens opus. La expirarea cotației, alte afișări păstrau categoria veche și auditul PASS. | Direcția condițională cere un setup sau lean neblocat pe aceeași parte. Expirarea locală schimbă coerent direcția, categoria, auditul și motivele în WAIT / DATA INSUFFICIENT. |

În calculul nivelurilor am corectat și eliminarea necondiționată a ultimei bare: se elimină numai minutele încă în formare. O bară deja încheiată este păstrată. Un nivel atins exact de ultima închidere rămâne o limită utilizabilă dacă nu există una dincolo de el.

În verificarea completă a interfeței am corectat separat cererea administrativă duplicată: cele două componente care observă configurarea furnizorilor nu mai retrimit automat același token refuzat. Reîncercarea explicită și introducerea unui token nou rămân funcționale.

## Reproduceri și verificare

Noua suită `tests/test_guidance_logic.py` conține 30 de cazuri, inclusiv:

- străpungere în sus și în jos prin funcția reală care calculează nivelurile, egalitatea cu limita, bare în formare și istoric exclusiv zilnic;
- evenimente vechi/noi cu direcții opuse și orizonturi diferite; coș și reacție din evenimente diferite; lipsa reacției pentru ambele semne;
- expirarea ambelor tipuri de lean cu o cotație încă proaspătă și păstrarea nemodificată a deciziei istorice;
- excluderea observațiilor stub, daily close, derived daily și proxy, plus păstrarea observațiilor live valide;
- respingerea reutilizării AI după schimbarea rolului unui nivel, a regimului sau a evenimentului;
- audit asincron cu preț stabil, cotație expirată, prag depășit și dovezi schimbate.

Testele frontend acoperă suplimentar contradicțiile dintre card și decizia curentă, expirarea coerentă a categoriei și auditului, precum și funcționarea reîncercării administrative explicite.

| Verificare | Rezultat |
|---|---|
| Backend, întreaga suită pytest | **1.053 trecute**, trei avertismente de depreciere existente |
| Frontend, Vitest | **299 trecute**, 13 fișiere |
| Browser, Playwright | **168 trecute**, rulare completă cu patru workers, 53,8 secunde |
| TypeScript | `tsc --noEmit --incremental false` trecut |
| ESLint | Fișierele frontend modificate: trecut |
| Ruff și mypy | Fișierele backend modificate: trecut |

Comenzi de reproducere, din rădăcina proiectului pentru Python și din `frontend` pentru Node:

```text
.venv\Scripts\python.exe -X utf8 -m pytest -q --tb=short --basetemp=E:/Today_Market/artifacts/pytest-logic-verified-0930 -o cache_dir=E:/Today_Market/artifacts/pytest-logic-cache-0930
node node_modules/vitest/vitest.mjs run
node node_modules/@playwright/test/cli.js test --workers=4 --reporter=line
node node_modules/typescript/bin/tsc --noEmit --incremental false
```

Două rulări paralele Playwright au încheiat fiecare cu 167 de teste trecute și câte un timeout de încărcare, în teste diferite. Rularea secvențială a identificat apoi un defect de izolare: testul de indisponibilitate intercepta HTTP, dar primea date reale prin WebSocket. `workspace.spec.ts` izolează acum streamul implicit la nivel de context; testele streamului își pot furniza propria conexiune simulată. Cele 29 de teste din fișier, apoi întreaga suită de 168 de teste, au trecut după această corecție. Timeouturile aserțiunilor au rămas neschimbate; această rulare reușită nu demonstrează că timeouturile intermitente nu pot reapărea.

La 15:15–15:16 Europe/Bucharest, backendul local a devenit disponibil și am verificat numai prin citire `/api/dashboard` și `/api/assets/BTC`: ambele au răspuns HTTP 200. Snapshotul avea 31 de active, analiză recentă și niciun simbol raportat ca eșuat. Toate deciziile erau WAIT; acest snapshot nu probează publicarea unui setup live. Răspunsul BTC conținea noua referință a nivelurilor la ultima închidere de minut completă. Rezumatul observațiilor este în `artifacts/logic-audit-2026-09-30/runtime-check.json`.

## Fișiere create sau modificate

- Backend: `backend/technicals.py`, `backend/scoring.py`, `backend/analytics.py`, `backend/evaluation.py`, `backend/decision.py`, `backend/service.py`.
- Frontend: `frontend/lib/api.ts`, `frontend/lib/freshness.ts`, `frontend/lib/trade-watch.ts`, `frontend/components/pages/provider-settings.tsx`.
- Teste: noul `tests/test_guidance_logic.py`; actualizări în `tests/test_scoring.py`, `tests/test_ai_volume.py`, `tests/test_ui_audit_be1.py`, `frontend/lib/trade-watch.test.ts`, `frontend/tests/detail.spec.ts`, `frontend/tests/workspace.spec.ts`, `frontend/tests/event-aware.spec.ts`, `frontend/tests/provider-settings.spec.ts`.
- Documentație: acest raport, `docs/DECISION_ENGINE.md`, `docs/AI_AGENTS.md` și nota de progres din `PROGRESS.md`.

`artifacts/logic-audit-2026-09-30/core-changes.patch` păstrează diferențele pentru fișierele salvate înainte de modificare și noua suită Python. Este un extras pentru revizuire, nu lista completă de modificări; lista completă este cea de mai sus.

## Limite și impact asupra folosirii

- Testarea automată folosește baze de date izolate și răspunsuri controlate. Citirea suplimentară a API-ului real confirmă starea acelui snapshot, fără să valideze fiecare furnizor extern sau generarea AI reală.
- Baza de date de lucru nu a fost rescrisă. Reacțiile și rezultatele istorice deja salvate nu sunt recalculate retroactiv de aceste modificări; statisticile existente nu sunt astfel certificate de acest audit.
- Validarea mai strictă a cache-ului poate reduce reutilizarea explicațiilor AI. Bugetele și cadențele rămân aplicabile; lipsa unui răspuns valid continuă să ducă la WAIT.
- Testele confirmă protecțiile implementate. Acuratețea predictivă și utilitatea pentru tranzacționare necesită separat evaluare cronologică pe date reale, în afara eșantionului, cu costuri și slippage.
