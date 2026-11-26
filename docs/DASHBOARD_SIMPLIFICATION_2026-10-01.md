# Dashboard simplificat — 1 octombrie 2026

## Cerința urmărită

Au fost citite integral `masterprompt.md` și `imbunatatire.md`. Cerința comună este ca ecranul de deschidere
să explice în 5–10 secunde ce se întâmplă, de ce, ce active sunt favorizate sau sub presiune și ce eveniment
poate schimba situația. Complexitatea analizei poate rămâne în backend; lectura principală trebuie să fie simplă.
Această modificare reorganizează funcțiile existente, fără servicii, integrări, semnale sau predicții suplimentare.

## Ce funcționa și ce împiedica înțelegerea

| Zonă | Constatare | Rezultat |
|---|---|---|
| Analiză deterministă | Regimul, motivele, expunerile, scenariile și riscul de eveniment existau deja. | Sunt păstrate și aduse într-o ordine de lectură clară. |
| Ecranul principal | Numeroase secțiuni deschise repetau interpretarea și evenimentele. | Rezumatul și riscurile sunt prioritare; detaliile existente se deschid la cerere. |
| „What matters now” | Pagina reală prioritiza știri fără impact stabilit asupra activelor urmărite, doar după importanța editorială. | Zona principală cere fapte confirmate cu impact identificat; știrile complete rămân accesibile. Panoul gol este omis. |
| Acțiune sau așteptare | Cardurile aveau motive, confirmare și invalidare, dar multe metrici concurau cu ele. | Maximum cinci carduri principale; metricile și dovada publicării sunt detalii. |
| Date istorice | Un context de la ultima închidere putea fi confundat cu direcția curentă. | Lipsa confirmării curente și avertismentele istorice rămân vizibile. |
| Sesiuni de piață | O sesiune deschisă era descrisă drept date interpretate live. | Textul descrie doar sesiunea; observațiile curente stabilesc disponibilitatea datelor. |
| Interpretare în cache | Booleanul `stale` nu îmbătrânea pe ecran când actualizările se opreau. | Backendul furnizează termenul configurat; browserul marchează expirarea local. |
| Detaliul unui activ | O decizie blocată în dashboard putea rămâne acționabilă în detaliu dacă reîncărcarea eșua. | Decizia și riscul publicate se sincronizează imediat, inclusiv blocările cu același ID. |
| Publicarea deciziilor | Verificarea finală omitea leans fără AI; un preț modificat putea păstra proza AI veche. | Toate direcțiile sunt reverificate; proza/auditul rămân legate de intrările analizate. |

## Lectura principală

1. **Market now / Market view:** concluzie, motive și incertitudine.
2. **Next market-moving event:** evenimentul următor, ora București, impactul, expunerile și explicația disponibilă.
3. **Favored / Under pressure / Not confirming**, urmate de **What can change this view**.
4. Maximum cinci carduri de urmărit, cu status, motiv, confirmare și invalidare.
5. „What matters now” afișează cel mult cinci fapte confirmate cu impact identificat. Contextul extins, calendarul,
   reacțiile, toate știrile, furnizorii și toate piețele se deschid din secțiunile pliate.

Avertismentele importante — date lipsă/expirate, contradicții și MARKET DIVERGENCE — rămân vizibile.
Etichetele surselor AI/deterministe și proveniența prețurilor sunt păstrate. Nu se modifică scorurile ori
regulile de tranzacționare pentru a produce o concluzie mai categorică.

## Verificarea datelor existente

Pe 1 octombrie, la 13:07 București, un test API pe o copie temporară a bazei existente, cu ingestia dezactivată,
a returnat HTTP 200 pentru 41 de cereri, inclusiv cele 31 de detalii API de active. Copia a fost ștearsă după test;
baza proprietarului și credențialele nu au fost rescrise. Rezultatele sunt în
`artifacts/continuation-2026-10-01/offline-smoke.json` și `offline-dashboard.json`.

Acest snapshot arăta `MIXED` pe baza ultimei închideri, fără observații curente pentru Rates/USD/Oil/Risk appetite,
fără active favorizate/presate confirmate. Evenimentul următor era US initial jobless claims, iar evenimentul
dominant era NFP. Consensul lipsea (`NO CONSENSUS`), iar pricingul pieței era `UNKNOWN`.
Acestea sunt limite ale datelor acelui snapshot, nu o previziune și nici o certificare a furnizorilor live.

API-ul local deja pornit folosea încă vechiul cod. A fost identificat lansatorul proiectului și repornit;
interfața existentă de pe portul 3000 a rămas disponibilă. La 13:27 București, `/health/ready` a răspuns `ready`,
iar `/api/dashboard` a returnat cele 31 de active, termenul configurat `stale_after_seconds=3600` și formularea
corectă despre sesiuni. Rezultatul citirii este în `artifacts/continuation-2026-10-01/live-dashboard-after-restart.json`.
Pagina reală a fost verificată și în browserul Codex, fără date de test injectate: contextul istoric și lipsa
confirmării curente sunt vizibile. Această citire locală nu verifică separat fiecare abonament/furnizor extern.
La 13:46, după ultimele modificări, zona principală de știri conținea un fapt `CONFIRMED` cu impact BRENT/WTI;
știrile `PARTIAL` erau accesibile în secțiunea pliată. Nu existau setupuri acționabile. În alertele existente,
Massive raporta acces respins (HTTP 403), iar multe niveluri tradiționale erau închideri zilnice ori proxy-uri.
Aceste limite rămân explicite; disponibilitatea unei sesiuni nu le transformă în cotații live.

Aplicația a rămas deschisă la <http://127.0.0.1:3000/>. Captură a paginii reale la 13:46 București:

![Dashboard real, cu contextul istoric și lipsa confirmării curente vizibile](../artifacts/dashboard-simplification-2026-10-01/dashboard-live-viewport.jpg)

## Verificare software

Backend: **1.090 teste trecute**, Ruff curat, mypy curat pentru 58 de fișiere. Frontend: **348 teste unitare
trecute** în 14 fișiere și **180 teste de browser trecute** în 55,9 secunde, cu două procese paralele.
TypeScript și ESLint curate, build Next.js de producție reușit. O revizuire independentă finală nu a găsit
probleme blocante în componentele și selectorii modificați.

Comenzi, din rădăcină:

```text
.venv/Scripts/python.exe -X utf8 -m pytest -q --tb=short --basetemp=artifacts/pytest-final-fixed-1001 -o cache_dir=artifacts/pytest-final-fixed-cache-1001
.venv/Scripts/python.exe -X utf8 -m ruff check backend tests scripts main.py launch.py
.venv/Scripts/python.exe -X utf8 -m mypy backend
```

Comenzi frontend, din `frontend/`:

```text
node node_modules/vitest/vitest.mjs run
node node_modules/typescript/bin/tsc --noEmit --incremental false
node node_modules/eslint/bin/eslint.js .
node node_modules/next/dist/bin/next build
```

Suita de browser a folosit buildul de producție pornit temporar la `http://127.0.0.1:4178`, prin
`PLAYWRIGHT_BASE_URL`, și comanda `node node_modules/@playwright/test/cli.js test --workers=2`.
Serverul temporar este oprit după verificare; API-ul și interfața proprietarului rămân pornite.

Verificările au găsit și corectat fixture-ul API rămas în urmă, așteptările vechi despre textul „live” și vizibilitatea
secțiunilor acum pliate. Un test de timp al limitatorului AI începea temporizatorul înainte de construirea clientului;
temporizatorul începe acum la intrarea efectivă în coadă, păstrând aceleași limite verificate. Un timeout intermitent
de încărcare în browser a trecut izolat și apoi în suita completă cu două procese paralele; nu s-au relaxat aserțiunile.

Rămân avertismente existente de depreciere Starlette/httpx, Alembic și configurație viitoare Vite.
Testele de browser folosesc date controlate și conexiuni izolate; capturile lor demonstrează afișarea,
nu direcția reală a pieței. Testele de logică nu certifică profitabilitatea istorică.

## Fișiere create sau modificate

- Backend: `backend/decision.py`, `backend/interpretation.py`.
- Interfață: `frontend/components/asset/asset-view.tsx`, `frontend/app/globals.css`;
  componentele dashboard `dashboard-view.tsx`, `regime-banner.tsx`, `market-view.tsx`, `trade-watch.tsx`,
  `next-event-card.tsx`, `event-risk-banner.tsx`, `what-matters.tsx`, `selectors.ts`;
  `frontend/lib/freshness.ts`, `frontend/lib/event-risk.ts`.
- Regresii noi: `tests/test_publication_continuity.py`, `tests/test_interpretation_freshness.py`,
  `frontend/components/dashboard/selectors.test.ts`.
- Regresii existente actualizate: `tests/test_interpretation.py`, `tests/test_ui_audit_be3.py`,
  `tests/test_llm_limiter.py`, testele frontend `freshness.test.ts`, `event-risk.test.ts`,
  `detail.spec.ts`, `event-aware.spec.ts`, `event-aware-real.spec.ts`, `dashboard.spec.ts`,
  `workspace.spec.ts`, `coverage.spec.ts`, `pages.spec.ts`, plus fixture-ul `event-risk.real.json`.
- Documentație: `docs/ARCHITECTURE.md`, `docs/DECISION_ENGINE.md`, `docs/AI_AGENTS.md`,
  acest raport, `PROGRESS.md`, `DECISIONS.md`, `ERROR_MEMORY.md`.

Versiunile anterioare și patch-urile de revizuire se află în `artifacts/continuation-2026-10-01/` și
`artifacts/dashboard-simplification-2026-10-01/`. Acest ultim director conține și capturile desktop/telefon
pentru date normale, insuficiente și istorice/expirate; fișierele numite `regression-fixture` folosesc date
controlate. Rezultatele finale sunt consemnate în `final-verification.json`.
