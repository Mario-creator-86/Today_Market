# Investigația logicii de piață

**Actualizare: cele cinci probleme de mai jos au fost corectate.** Constatările originale sunt păstrate pentru trasabilitate. Detaliile implementării și verificării sunt în `LOGIC_FIXES_2026-09-29.md`. Scriptul de audit verifică acum comportamentul corectat.

Audit efectuat la 29 septembrie 2026. Inspecție de cod, citire `/api/dashboard` și reproduceri locale fără apeluri către furnizori sau modificarea bazei de date. Logica de producție nu a fost modificată în această investigație.

## Constatări

### 1. P1 — O cotație veche poate confirma un instrument într-un regim live

`backend/event_risk.py:1596-1603` revine la caracteristici istorice sau la cotația brută dacă nu există observație curentă. Protecția din `market_view`, liniile 1635-1640, blochează această confirmare numai când regimul are baza `last_close`.

Reproducere: SP500 cu sesiune LIVE, cotație STALE de ieri și schimbare +1,50%, fără decizie, într-un context RISK_ON/live. Rezultat: FAVORED, cu explicația `latest quote`. Nu s-a demonstrat că acest caz este activ în snapshotul real, care are regim last_close; defectul de ramură este însă reprodus.

Corecție recomandată: confirmarea curentă trebuie să solicite dovezi curente indiferent de baza regimului. Observația istorică poate rămâne explicație contextuală, fără a vota ca o confirmare actuală. O decizie reutilizată necesită și verificarea vechimii proprii.

### 2. P1 — Rezumatul AI poate păstra cifre care nu mai corespund datelor

`backend/interpretation.py:555-581` exclude valorile numerice ale driverilor din cheia de reutilizare. `for_snapshot`, liniile 1452-1455, returnează textul AI salvat dacă cheia și starea de prospețime coincid, fără revalidarea afirmațiilor numerice.

Reproducere: intrarea SP500 se schimbă de la +0,50% la +2,50%, păstrând direcția up. Textul AI care citează +0,50% este păstrat. Acesta este un caz controlat, nu o afirmație că textul AI actual din aplicație conține exact această eroare.

Corecție recomandată: separarea cadenței de generare AI de validarea textului afișat. Revalidarea numerelor și bazelor citate la reutilizare; la nepotrivire, rezumat determinist actual, fără cerere AI suplimentară obligatorie.

### 3. P1 — Filtrarea intrărilor pentru regimul live este incompletă

`backend/regime.py:149-156` verifică exclusiv `freshness_status == FRESH`. Nu exclude explicit stub, sesiune închisă sau clasă de date diferită de LIVE, deși funcția promite excluderea piețelor închise. `current_change` folosește filtre mai stricte: două componente au definiții diferite pentru o observație utilizabilă.

Reproducere: o cotație FRESH, stub=True, sesiune CLOSED este acceptată de `features` ca intrare live, dar respinsă de `current_change`. În fluxul normal, serviciul marchează deja închiderile zilnice STALE, ceea ce reduce riscul pentru acea clasă; filtrul de sesiune și cel de stub lipsesc în continuare. Reproducerea demonstrează acceptarea intrării, nu existența unui semnal executabil greșit în aplicația curentă.

Corecție recomandată: criteriu comun de eligibilitate a cotației live, utilizat înainte de extragerea caracteristicilor. Trebuie păstrate și cotațiile valide care au nivel, dar nu schimbare, de exemplu VIX.

### 4. P2 — Clasamentul știrilor confundă autoritatea sursei cu impactul

`backend/service.py:141-154` folosește ponderea sursei drept importanță în lipsa extracției. `frontend/components/dashboard/selectors.ts:255-267` ordonează direct după această valoare.

Confirmat în API: comunicatele despre plățile UE către Austria și Franța și un comunicat SEC au importance=1, importance_basis=source tier, extraction=null. UI convertește 1 în 10/10, deși impactul asupra activelor nu este stabilit. Eticheta source tier explică originea numărului, dar clasamentul tot tratează autoritatea ca importanță măsurată.

Corecție recomandată: câmpuri separate pentru credibilitate și relevanță de piață; impactul neanalizat să rămână necunoscut. Prioritizare pe active afectate, actualitate și impact documentat, cu autoritatea sursei ca informație separată.

### 5. P2 — WAIT nu explică informațiile disponibile și lipsurile reale

`backend/scoring.py:442-458` elimină componentele lipsă și componentele măsurate cu valoare zero. `backend/event_risk.py:1772` transmite exclusiv `decision.why` în card. Blocajele și explicațiile din `component_detail` nu sunt preluate.

Confirmat în API: BTC are componente tehnică și poziționare măsurate, neutre, și cinci componente lipsă, dar why=[]. NASDAQ are blocajul `Critical data missing or stale` și toate componentele lipsă, însă why=[]. UI ajunge la mesajul generic `No verified reasons were supplied`, deși există explicații precise.

Corecție recomandată: explicații distincte pentru decizia de a aștepta: ce este măsurat și neutru, ce lipsește și ce blochează publicarea. Acest lucru nu trebuie să crească scorul sau să transforme WAIT într-un semnal.

## Ce funcționează corect în fluxul inspectat

- Clasificarea istorică afișată și regimul folosit la calculul deciziilor sunt separate. Snapshotul poate afișa RISK_OFF istoric în timp ce decizia are INSUFFICIENT_DATA pe baza live; diferența nu este, singură, un bug.
- Proxy-urile oferă direcție fără a deveni nivelul executabil al indicelui.
- Deciziile sunt calculate determinist; AI explică, iar auditul poate bloca publicarea.
- Absența consensului nu este convertită într-o surpriză numerică inventată.
- WAIT pentru lipsa prețului curent sau a dovezilor nu trebuie eliminat doar pentru a produce mai multe semnale.

## Dovezi și limite

`artifacts/audit_market_logic.py` reproduce primele trei defecte și valoarea de importanță din punctul 4. Rulare: `.venv/Scripts/python.exe -X utf8 artifacts/audit_market_logic.py`. Toate aserțiunile de reproducere au trecut; ele confirmă comportamentul problematic existent, nu corectitudinea lui. Datele sintetice sunt exclusiv cazuri de audit și nu sunt introduse în aplicație.

Punctele 4 și 5 au fost confirmate în răspunsul real `/api/dashboard`, la aproximativ 20:23 Europe/Bucharest. Testele generale trecute anterior nu acoperă toate aceste combinații. Auditul nu validează profitabilitatea regulilor și nu dovedește că fiecare funcție a sistemului este corectă.

Ordine recomandată: eligibilitatea comună a datelor → confirmarea curentă → reutilizarea AI → explicațiile WAIT → clasamentul știrilor. Conexiunile și integrarea X rămân în afara modificărilor.
