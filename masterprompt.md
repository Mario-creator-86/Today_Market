PROJECT NAME
Market Intelligence & Decision Hub

ROLE
You are the lead software architect, senior full-stack engineer, data engineer,
AI engineer, quantitative systems engineer and QA engineer responsible for
building this application end-to-end.

Do not treat this as a prototype or visual mockup.

Build a working, modular, production-oriented application.

The final product is a real-time market intelligence and decision-support hub
that gathers live market data, economic data, official announcements, news,
social/X signals and geopolitical developments, verifies them, determines what
actually matters to markets, measures real market reaction, and produces
clear LONG / SHORT / WAIT market setups with evidence, confidence, confirmation
and invalidation.

The app must NOT blindly trust LLM output.

The deterministic application logic controls:
- facts
- prices
- timestamps
- calculations
- scoring
- thresholds
- source reliability
- decision gates
- market reaction measurements
- backtesting
- calibration

LLMs are used only for:
- research planning
- information extraction
- semantic classification
- contradiction detection
- reasoning over already verified facts
- market interpretation
- explanation
- auditing

NO LLM may invent market data or override deterministic safety rules.


==================================================
0. BEFORE WRITING CODE
==================================================

First inspect the entire repository.

If an existing application exists:
- preserve working functionality;
- reuse existing architecture where sensible;
- do not unnecessarily rewrite working modules;
- document every major architectural change.

If the project is empty, initialize it according to this specification.

Create immediately:

/docs/PROJECT_SPEC.md
/docs/ARCHITECTURE.md
/docs/DATA_SOURCES.md
/docs/DECISION_ENGINE.md
/docs/AI_AGENTS.md
/docs/AUTOLEARN.md
/docs/SECURITY.md
/PROGRESS.md
/DECISIONS.md
/ERROR_MEMORY.md

PROGRESS.md must be maintained after every implementation stage.

DECISIONS.md must contain important architectural decisions and reasons.

ERROR_MEMORY.md must record:
- serious bugs,
- root cause,
- permanent prevention rule.

Example:

ERROR:
A stale BTC quote was shown as live.

ROOT CAUSE:
Web search result used instead of real-time market feed.

PERMANENT RULE:
If a real-time market provider is available, prices from articles/search
must never be used as current market prices.


==================================================
1. CORE PRINCIPLE
==================================================

Implement this exact information pipeline:

DATA SOURCES
    ↓
INGESTION
    ↓
NORMALIZATION
    ↓
DEDUPLICATION
    ↓
FACT VERIFICATION
    ↓
EVENT CLASSIFICATION
    ↓
CAUSAL / MARKET IMPACT ENGINE
    ↓
SPECIALIST AI ANALYSIS
    ↓
REAL MARKET REACTION ENGINE
    ↓
DETERMINISTIC SCORING ENGINE
    ↓
LONG / SHORT / WAIT
    ↓
CHIEF ANALYST EXPLANATION
    ↓
AI AUDITOR
    ↓
UI
    ↓
OUTCOME TRACKING
    ↓
AUTOLEARN / CALIBRATION


The system must always distinguish:

FACT

from

INTERPRETATION

from

MARKET REACTION

from

DECISION.


==================================================
2. TECHNOLOGY STACK
==================================================

Use:

FRONTEND
- Next.js
- TypeScript
- App Router
- Tailwind CSS
- shadcn/ui
- TanStack Query
- lightweight-charts or equivalent financial chart library
- native WebSocket client

BACKEND
- Python
- FastAPI
- Pydantic v2
- SQLAlchemy 2
- Alembic

DATABASE
- PostgreSQL
- TimescaleDB extension for time-series data
- pgvector for semantic similarity / deduplication

CACHE / EVENT SYSTEM
- Redis
- Redis Streams

BACKGROUND JOBS
- Celery workers
- Celery Beat scheduler

AI
- Ollama Cloud as primary runtime AI provider

Optional future provider:
- OpenAI

But implement through an abstraction:

LLMProvider

with:

OllamaProvider
OpenAIProvider optional

Do not couple business logic directly to a model vendor.

CONTAINERS
- Docker
- docker-compose

TESTS
- pytest
- Vitest
- Playwright

OBSERVABILITY
- structured JSON logging
- health endpoints
- OpenTelemetry-ready architecture
- provider latency and failure metrics

TIMEZONE

Store ALL timestamps internally as UTC.

Render user-facing times using:
Europe/Bucharest

NEVER hardcode UTC+2 or UTC+3.

DST conversion must use the IANA timezone database.


==================================================
3. OLLAMA CLOUD
==================================================

Use official Ollama Cloud API.

Configuration:

OLLAMA_BASE_URL=https://ollama.com/api
OLLAMA_API_KEY=<environment variable>

NEVER expose OLLAMA_API_KEY to browser code.

At application startup and periodically call the Ollama model-list endpoint.

Maintain a MODEL REGISTRY.

Do NOT hardcode model names into business logic.

The system must dynamically discover currently available Ollama Cloud models.

Store:

model_id
display_name
capabilities
tools_supported
thinking_supported
context_window if discoverable
availability
last_seen
status
preferred_roles

Administrators must be able to select models from Settings.

Each AI role must support:

PRIMARY MODEL
BACKUP MODEL
OPTIONAL SECOND-OPINION MODEL

If a model disappears or becomes unavailable:
automatically fall back.

Do not automatically migrate to an unknown model without logging the change.


==================================================
4. AI ROLE REQUIREMENTS
==================================================

Create these AI roles.

IMPORTANT:
All important AI outputs must use strict Pydantic / JSON schemas.
Do not depend on parsing arbitrary prose.


--------------------------------------------------
A. RESEARCH PLANNER
--------------------------------------------------

Purpose:
Determine what should be searched when a new event or market anomaly appears.

Inputs:
- triggering event
- asset movements
- known recent events
- active monitored topics

Output ONLY:

{
  "queries": [],
  "priority": 0-100,
  "reason": "...",
  "domains_preferred": []
}

Example trigger:

Brent +3.5% in 12 minutes.

Expected queries:

Iran Hormuz latest
Saudi energy infrastructure attack
US Iran negotiations
OPEC announcement
shipping disruption Gulf

The Research Planner:
DOES NOT make trade decisions.
DOES NOT claim facts.
DOES NOT calculate market direction.


--------------------------------------------------
B. NEWS / DOCUMENT EXTRACTOR
--------------------------------------------------

Purpose:
Extract structured information from articles, releases, filings and posts.

Output:

{
  "who": [],
  "what": "",
  "when": "",
  "where": "",
  "direct_claims": [],
  "new_information": [],
  "background_information": [],
  "mentioned_assets": [],
  "mentioned_entities": [],
  "event_type": "",
  "potential_importance": 0-100
}

NO trading interpretation.


--------------------------------------------------
C. FACT CHECKER
--------------------------------------------------

Purpose:
Compare evidence from several sources.

Possible results:

CONFIRMED
PARTIALLY_CONFIRMED
UNCONFIRMED
CONTRADICTED
FALSE
STALE

Output:

{
  "status": "...",
  "fact_confidence": 0-100,
  "verified_statements": [],
  "unverified_statements": [],
  "contradictions": [],
  "evidence_ids": [],
  "explanation": ""
}

fact_confidence MUST NOT be a random LLM-generated feeling.

The deterministic backend calculates the final fact confidence.

The AI may propose evidence relationships.
The backend applies source weights.


--------------------------------------------------
D. MACRO ANALYST
--------------------------------------------------

Analyze only verified inputs concerning:

Fed
Treasury
CPI
PCE
NFP
jobless claims
GDP
PMI
retail sales
housing
inflation expectations
Treasury auctions
yield curve
credit conditions

Return structured impact hypotheses by asset.

Do NOT calculate final LONG/SHORT decision.


--------------------------------------------------
E. FX ANALYST
--------------------------------------------------

Mandatory currencies:

DXY
EUR/USD
GBP/USD
USD/JPY

Add:
AUD/USD
USD/CHF
USD/CAD
NZD/USD

when relevant.

FX analysis MUST be relative.

Never use:
"Fed hikes therefore USDJPY must rise."

Instead compare:

Fed stance
versus
BoJ stance

Fed
versus
ECB

Fed
versus
BoE

Include:

current policy rates
expected next meeting
market repricing
government bond yield differential
guidance
economic surprise differential
intervention risk
risk sentiment

Output:

USD-positive
USD-negative
neutral/wait

plus:
confirmation
invalidation
causal explanation.


--------------------------------------------------
F. CRYPTO ANALYST
--------------------------------------------------

Analyze:

BTC
ETH

and crypto equities:

COIN
MSTR
BMNR

Include where available:

spot price
volume
funding
open interest
liquidations
basis
options IV
skew
put/call
large expiries
ETF flows
key technical levels
cross-asset relationship with DXY/yields/Nasdaq

Do not infer a bullish signal merely because calls > puts.

Do not use max pain as a price prediction.


--------------------------------------------------
G. EQUITY / CREDIT ANALYST
--------------------------------------------------

Analyze:

S&P 500
Nasdaq
Russell
semiconductors
banks
AI equities
systemically important earnings
credit spreads
corporate debt events

Track major names such as:

NVDA
MSFT
AAPL
AMZN
META
GOOGL
TSLA

but do not hardcode analysis only around these names.


--------------------------------------------------
H. GEOPOLITICAL / ENERGY ANALYST
--------------------------------------------------

Analyze only market-relevant developments.

Examples:

Iran
Israel
Hormuz
Red Sea
Houthi attacks
Russia / Ukraine
China / Taiwan
sanctions
shipping disruption
oil infrastructure
energy policy
major diplomatic developments

Output explicit causal pathways.

Example:

Hormuz disruption
→ supply risk
→ oil
→ inflation expectations
→ bond yields
→ Fed expectations
→ USD / equities / crypto


--------------------------------------------------
I. CHIEF ANALYST
--------------------------------------------------

The Chief Analyst is NOT allowed to calculate prices or final scores.

It receives:

verified facts
event scores
market reaction
asset scores
technical levels
specialist analyses
historical statistics

Its job is to answer:

WHY?

Example:

BTC — WAIT, bearish lean

Reasons:
- Treasury yields increased.
- DXY confirms USD strength.
- Oil adds inflation risk.
- BTC has not broken support.

Confirmation:
BTC < 83,250 + yields remain elevated.

Invalidation:
BTC > 85,200 while DXY and yields fall.

Keep explanations concise.


--------------------------------------------------
J. AI AUDITOR
--------------------------------------------------

Run immediately before publication.

Check:

Are all current prices fresh?
Are timestamps valid?
Is the market currently open?
Are sources attached?
Is an old article being presented as new?
Does the claim go further than the source?
Are contradictory sources visible?
Does LONG/SHORT correspond with deterministic score?
Are probabilities statistically valid?
Has an LLM invented numerical data?
Is timezone conversion correct?
Is an unverified X post presented as fact?

Return:

PASS

or

BLOCK

with specific reasons.

If BLOCK:
the card must not be published as a trade setup.


==================================================
5. DATA PROVIDER ARCHITECTURE
==================================================

Use provider interfaces.

MarketDataProvider
NewsProvider
EconomicCalendarProvider
SocialProvider
RegulatoryProvider
CryptoDerivativesProvider

Providers must be replaceable.


==================================================
6. MARKET DATA SOURCES
==================================================

Preferred architecture:

US EQUITIES / INDICES / OPTIONS / FX
Provider adapter for Massive or equivalent professional real-time provider.

CRYPTO SPOT
Exchange WebSockets and/or professional market provider.

DERIBIT
Use official Deribit WebSocket/API for crypto derivatives.

Track:

BTC options
ETH options
open interest
expiry
strike concentrations
IV
skew
funding where applicable

NEVER use news articles as the current price source if a market API is healthy.

Every market data object must contain:

price
timestamp
provider
latency
market_status
freshness_status


==================================================
7. NEWS SOURCES
==================================================

Implement source hierarchy.

TIER 1 — SOURCE OF TRUTH

Federal Reserve
US Treasury
BLS
BEA
SEC
CFTC
Congress.gov
White House / official US government
ECB
BoJ
BoE
EIA
OPEC
company SEC filings
company investor relations
exchange announcements

TIER 2

Reuters
Associated Press

Use licensed APIs when credentials are available.

Do not bypass paywalls or restrictions.

TIER 3

credible financial publications.

TIER 4

specialized publications.

TIER 5

social media / X.

Social media must primarily be treated as:
DISCOVERY
unless the account is an official source.


==================================================
8. OLLAMA WEB SEARCH
==================================================

Use Ollama Web Search for:

event discovery
deep research
corroboration
finding official documentation
investigating unexplained market moves

NOT as the primary live price source.

Implement:

SearchBudgetManager

Priority:

P0 critical breaking
P1 high impact
P2 scheduled event research
P3 broad sweep

Do not endlessly search the web.

Suggested cadence:

active breaking topic:
every 60 seconds if unresolved

broad global sweep:
every 5 minutes

scheduled central-bank event:
higher frequency surrounding release

post-price anomaly:
immediate targeted search


==================================================
9. X / TWITTER
==================================================

Create an X integration.

Support:

official account watchlists
trusted journalist watchlists
keyword monitoring
cashtag monitoring
filtered stream when available
recent search fallback

Maintain SourceRegistry:

account
type
trust_weight
official
topics
historical_accuracy

Example:

Federal Reserve official:
trust very high.

Reuters journalist:
high.

Known analyst:
medium.

Unknown account:
low.

IMPORTANT:

An unknown X post must never establish a confirmed fact.

Example:

"Explosion near Hormuz"

must initially become:

UNVERIFIED EVENT

Then automatically trigger checks against:

Reuters
AP
official military/government sources
credible maritime sources
additional independent sources

X should be displayed visually as:

UNVERIFIED
OFFICIAL STATEMENT
CORROBORATED
CONFIRMED
CONTRADICTED


==================================================
10. REFRESH / POLLING RULES
==================================================

Implement these default refresh rules.

REAL TIME

BTC/ETH:
WebSocket.

US equities during market hours:
WebSocket.

FX during open market:
WebSocket.

futures:
WebSocket when provider supports.

UI live price tiles:
update max once per second to avoid UI overload.

DERIVED MARKET METRICS
5 seconds.

CRYPTO DERIVATIVES
5-15 seconds.

BREAKING NEWS
stream / long poll when available.

X stream
real-time when available.

X search fallback
30-60 seconds.

HOT WEB SEARCH TOPICS
60 seconds.

GLOBAL NEWS SWEEP
5 minutes.

SEC FILINGS
60 seconds during US market hours.

CONGRESS / REGULATION
5 minutes when an important bill/event is active.
30 minutes otherwise.

CENTRAL BANK PAGES
5 minutes normally.

Within 15 minutes before/after major scheduled decisions:
30 seconds.

ECONOMIC CALENDAR
full refresh every 6 hours.

15 minutes before major macro event:
activate event watch mode.

At scheduled release:
poll authoritative source immediately.

FULL MARKET INTERPRETATION
every 5 minutes if markets are active.

Immediate recalculation after:
breaking event
macro release
central-bank statement
large price move
technical breakout
cross-asset divergence
fact status change

Do NOT call LLMs for every market tick.


==================================================
11. EVENT OBJECT
==================================================

Every meaningful event becomes a structured object.

Example:

{
  "event_id": "...",
  "event_type": "GEOPOLITICAL_ENERGY",
  "topic": "IRAN_HORMUZ",
  "first_seen_utc": "...",
  "last_updated_utc": "...",
  "verification_status": "CONFIRMED",
  "fact_confidence": 93,
  "novelty": 0.88,
  "importance": 0.94,
  "entities": [],
  "assets": [],
  "sources": [],
  "summary_fact": "",
  "interpretation": null
}


==================================================
12. DEDUPLICATION
==================================================

The same story may arrive from:

Reuters
AP
X
company release
10 websites
50 reposts

It must remain ONE EVENT.

Use:

entity matching
timestamp similarity
semantic similarity with embeddings
URL canonicalization
headline normalization

pgvector may be used for semantic comparison.

Store all source evidence under the single event.


==================================================
13. SOURCE RELIABILITY
==================================================

Create configurable deterministic defaults.

Example:

official statistical release   1.00
SEC/exchange filing             1.00
central bank                    1.00
official government statement   0.98
Reuters                         0.97
AP                              0.97
company IR                      0.95
major financial press           0.85
specialized publication         0.75
trusted analyst                 0.65
unknown X                       0.20
anonymous source                0.10

Do NOT hardcode these values into application logic.

Store them in versioned configuration/database.

Only administrator or validated autolearn process may change them.

LLMs cannot modify source weights.


==================================================
14. FACT CONFIDENCE
==================================================

Fact confidence must be calculated algorithmically.

Consider:

number of independent confirmations
source reliability
official confirmation
contradictions
age
direct vs second-hand reporting

Example:

official release
+ Reuters
+ AP

should generate extremely high fact confidence.

One unknown X post:
very low confidence.


==================================================
15. MARKET CALENDAR
==================================================

Build a proper intelligent calendar.

Sources:

economic calendar provider
official release calendars
central-bank calendars
Treasury auctions
major earnings calendar
crypto option expiries
legislative votes
court dates when market relevant

Display Europe/Bucharest time.

Example card:

15:30 🇺🇸 CPI

Consensus: 2.8%
Previous: 2.7%

Impact: 10/10

Scenarios:

≤2.6
Fed pricing dovish
USD negative
Treasuries positive
Nasdaq positive
BTC potentially positive

2.7-2.9
near consensus

≥3.0
Fed repricing hawkish
yields positive
USD positive
Nasdaq negative
BTC/ETH pressure


==================================================
16. MACRO SURPRISE ENGINE
==================================================

Calculate:

actual - consensus

AND

historical normalized surprise.

Use:

surprise_z_score =
(actual - consensus) /
historical standard deviation of surprises

Do this algorithmically.

Example:

CPI expected 2.8
actual 3.1
raw surprise +0.3pp
z-score +2.1 sigma

The LLM explains the result.

It does not calculate it.


==================================================
17. CAUSAL KNOWLEDGE GRAPH
==================================================

Build a deterministic, versioned causal rule engine.

Do NOT leave basic market relationships entirely to LLM reasoning.

Rules should represent relationships such as:

Hotter inflation
→ Fed hawkish repricing
→ front-end yields up
→ USD relative support
→ pressure on high-duration equities

Hormuz disruption
→ oil supply risk
→ oil up
→ inflation expectations up
→ potential yields/Fed repricing up

But also:

Hormuz escalation
→ safe haven demand
→ possible gold support

Support competing effects.

Each rule should include:

trigger
affected variable
direction prior
strength prior
half-life
regime dependencies
exceptions
version


==================================================
18. MARKET REACTION ENGINE
==================================================

For each important event capture market reaction at:

T0
T+1m
T+5m
T+15m
T+30m
T+1h
T+4h
close
next session

Track where available:

US2Y
US5Y
US10Y
US30Y
DXY
EURUSD
USDJPY
GBPUSD
NASDAQ
SP500
Russell
VIX
Gold
WTI
Brent
BTC
ETH

Determine whether expected reaction was:

CONFIRMED
PARTIAL
DIVERGENT
REVERSED
NO_REACTION

Example:

Hawkish Fed event.

Expected:
US2Y up
DXY up
Nasdaq down

Actual:
US2Y +10 bp
DXY +0.6%
Nasdaq -1.1%

Market confirmation high.

If Nasdaq rises despite yields and USD rising:
flag DIVERGENCE.


==================================================
19. PRICE ANOMALY DETECTOR
==================================================

Detect unusual moves.

Examples:

BTC > 1% in 5m
BTC > 2% in 15m
Brent > 1.5% in 10m
US10Y > 5 bp quickly
DXY > 0.4% quickly
NASDAQ futures > 0.8% quickly

Thresholds must be configurable and later volatility-adjusted.

A price anomaly triggers:

Research Planner
targeted web search
X search
event correlation analysis


==================================================
20. TECHNICAL LEVEL ENGINE
==================================================

Technical levels must be calculated.

Do not ask an LLM to invent supports/resistances.

Include:

previous day high/low
previous week high/low
session high/low
VWAP
ATR
recent swing highs/lows
volume profile if available
20d high/low
50d high/low
important options strikes
large open-interest concentrations

LLM can explain levels after calculation.


==================================================
21. POSITIONING ENGINE
==================================================

Where data permits include:

options OI
put/call
IV
skew
gamma concentrations
funding
futures basis
liquidations
ETF flows

Positioning is only one decision component.

Never output:

calls > puts therefore bullish.


==================================================
22. ASSET SCORING ENGINE
==================================================

Final asset scores MUST be deterministic.

Range:

-100 to +100

Suggested initial component weights:

EVENT / NEWS IMPACT       30%
MACRO / RATES             20%
MARKET REACTION           15%
PRICE / TECHNICAL         15%
POSITIONING                8%
CROSS-ASSET                7%
REGIME                     5%

Keep weights in versioned configuration.

Do not hardcode.

Event contribution should consider:

direction
impact
fact confidence
source quality
novelty
freshness
surprise
priced-in estimate
persistence
market confirmation


==================================================
23. DECISION CATEGORIES
==================================================

Start with:

+60 to +100
STRONG LONG SETUP

+30 to +59
LONG SETUP

+15 to +29
BULLISH LEAN / WAIT

-14 to +14
WAIT

-15 to -29
BEARISH LEAN / WAIT

-30 to -59
SHORT SETUP

-60 to -100
STRONG SHORT SETUP

These thresholds must be configurable and versioned.


==================================================
24. DECISION GATES
==================================================

Even a -90 score cannot automatically produce SHORT if critical conditions fail.

Examples of BLOCK conditions:

critical data stale
major provider offline
important fact unverified
high-quality sources contradict
market closed and price unavailable
score based mainly on social rumors
required price confirmation absent
AI Auditor fails

Output becomes:

WAIT / DATA INSUFFICIENT

No AI may bypass a gate.


==================================================
25. PROBABILITIES
==================================================

The UI needs percentages, but do this correctly.

Initially show:

DIRECTION SCORE
-100...+100

EVIDENCE CONFIDENCE
0-100%

MARKET CONFIRMATION
0-100%

DATA QUALITY
0-100%

Do NOT label an arbitrary LLM confidence as:
"70% probability price will rise."

Only show statistical directional probability after autolearn has enough
historical observations.

Example:

Historical downside probability:
64%

Comparable observations:
n=823

Horizon:
4 hours

Calibration:
good

If sample size is insufficient:

Historical probability:
NOT ENOUGH DATA


==================================================
26. MARKET REGIME ENGINE
==================================================

Classify current regime algorithmically using market features.

Possible regimes:

RISK_ON
RISK_OFF
INFLATION_SHOCK
GROWTH_SCARE
LIQUIDITY_STRESS
GEOPOLITICAL_SHOCK
USD_STRENGTH
USD_WEAKNESS
VOLATILITY_EXPANSION
LOW_VOLATILITY
MIXED

Use:

rates
yield curve
DXY
oil
gold
VIX
equities
credit
BTC
cross-asset correlations

Allow more than one active regime.

Example:

DEFENSIVE
Inflation shock + USD strength

Confidence:
78%


==================================================
27. UI / UX
==================================================

The backend may be complicated.

The UI must be extremely simple.

Dark professional financial-dashboard interface.

No visual clutter.

Desktop first but responsive.

MAIN PAGE


HEADER

Market Intelligence

LIVE indicator

Bucharest time

Provider health

Last full analysis time


--------------------------------------------------
MARKET REGIME
--------------------------------------------------

Example:

DEFENSIVE

Confidence 78%

Rates ↑
USD ↑
Oil ↑
Risk appetite ↓


--------------------------------------------------
MARKET STRIP
--------------------------------------------------

BTC
ETH
S&P
NASDAQ
DXY
US2Y
US10Y
EURUSD
GBPUSD
USDJPY
GOLD
WTI
BRENT

Show:

price
change
market state
freshness


--------------------------------------------------
TODAY'S BEST SETUPS
--------------------------------------------------

Maximum 3-5 cards.

Example:

EUR/USD

SHORT

Decision score:
-67

Evidence confidence:
94%

Market confirmation:
81%

Historical probability:
64%
n=418
4h horizon

WHY

• Fed repricing more hawkish than ECB
• US-EU yield spread widening
• DXY confirming

CONFIRMATION

1.xxxx break

INVALIDATION

US yields fall + DXY loses xxxx


--------------------------------------------------
WHAT MATTERS NOW
--------------------------------------------------

Display maximum 5.

Example:

1. Iran / Hormuz        9.2/10
2. Fed / rates          8.8/10
3. US-China             7.5/10

For each:

FACT STATUS
impact
affected assets
one-sentence explanation


--------------------------------------------------
NEXT IMPORTANT EVENTS
--------------------------------------------------

Example:

15:30 CPI
Impact 10/10

17:00 Michigan
Impact 8/10

20:00 Treasury auction
Impact 7/10


--------------------------------------------------
BREAKING / VERIFIED
--------------------------------------------------

Live stream.

Each item clearly labelled:

CONFIRMED
PARTIAL
UNVERIFIED
CONTRADICTED

Show source.


==================================================
28. ASSET DETAIL PAGE
==================================================

Example BTC.

Top:

BTC
live price
24h
current signal
score
confidence

Then:

WHY IS BTC MOVING?

Display contributions:

Rates             -18
USD               -12
Geopolitics       -9
Technical         +8
Positioning       +5

Net:
-26

Then:

SUPPORT / RESISTANCE

Then:

CONFIRMATION

Then:

INVALIDATION

Then:

EVENT TIMELINE

Then:

cross-asset chart:

BTC
DXY
US10Y
Nasdaq

Then:

historical comparable signals.


==================================================
29. EVENT DETAIL PAGE
==================================================

Show:

FACT

WHAT HAPPENED

VERIFICATION STATUS

SOURCES

CONTRADICTIONS

CAUSAL CHAIN

Example:

Iran escalation
↓
Oil +4.2%
↓
Inflation risk
↓
US10Y +7 bp
↓
DXY +0.5%
↓
BTC -1.9%

Expected reaction:
...

Actual reaction:
...

Market confirmation:
...


==================================================
30. FX PAGE
==================================================

Dedicated table.

Pair
Current price
Bias
Fed rate
Other central bank rate
Policy differential
2Y yield differential
Next meeting expectation
Confirmation
Invalidation

Include:

EUR/USD
GBP/USD
USD/JPY
AUD/USD
USD/CAD
USD/CHF

Also DXY.


==================================================
31. CALENDAR PAGE
==================================================

Group by day.

Highlight only medium/high-impact events.

Default display:
HIGH IMPACT only.

Each card:

time Europe/Bucharest
country
event
previous
consensus
actual
surprise
surprise z-score
market impact score

Before event:
scenario analysis.

After event:
market reaction.


==================================================
32. SOURCES / VERIFICATION PAGE
==================================================

Provide transparency.

For each event show:

source
publication time
ingestion time
trust tier
verification result
supporting/contradicting evidence

Never hide uncertainty.


==================================================
33. SETTINGS
==================================================

Tabs:

DATA PROVIDERS

AI MODELS

SOURCES

REFRESH

SCORING

ALERTS

AUTOLEARN

Model settings must display dynamically discovered Ollama models.

For each AI role:

Primary
Backup
Second opinion

Do not make users edit code to change models.


==================================================
34. PROVIDER HEALTH
==================================================

Implement status dashboard:

Ollama        ONLINE
Market Data   ONLINE
Deribit       ONLINE
Reuters       ONLINE
AP            ONLINE
X             DEGRADED
Calendar      ONLINE

Statuses:

ONLINE
DEGRADED
OFFLINE
AUTH_ERROR
RATE_LIMITED

If critical market feed becomes unavailable:

disable relevant live signals.

Never silently replace it with stale search data.


==================================================
35. AUTOLEARN
==================================================

This feature is critical.

Do NOT build a system where an LLM rewrites its own rules.

Autolearn must be quantitative and controlled.

For every decision store:

decision_id
timestamp
asset
market regime
price
direction
decision score
confidence
components
events
features
sources
model versions
rule version
weights version

Then calculate outcomes:

5m
15m
1h
4h
close
next session

Store:

return
maximum favorable excursion
maximum adverse excursion
whether confirmation occurred
whether invalidation occurred


==================================================
36. HISTORICAL PERFORMANCE
==================================================

Analyze performance by:

asset
event type
regime
score bucket
confidence bucket
time horizon
source combination
technical setup

Metrics:

hit rate
average return
median return
profit factor for paper signals
maximum drawdown
MFE
MAE
Brier score
calibration error
false-positive rate


==================================================
37. CHAMPION / CHALLENGER
==================================================

Production scoring engine = CHAMPION.

New candidate weighting/model = CHALLENGER.

Challenger operates in shadow mode.

Never change live production behavior immediately.

Promotion requires statistically meaningful improvement across:

out-of-sample performance
calibration
drawdown
false-positive rate
regime robustness

Store all versions.

Support automatic rollback.


==================================================
38. NO LOOKAHEAD BIAS
==================================================

Backtesting must only use information that was actually available at that time.

Store ingestion timestamps separately from event timestamps.

An article published later must never influence an earlier simulated decision.


==================================================
39. PAPER TRADING
==================================================

Implement internal paper-trading evaluation.

This is NOT automated real trading.

For qualifying signals create virtual positions.

Track:

realistic bid/ask
spread
slippage
entry
invalidation
target
exit
MFE
MAE

The purpose is to determine whether good analysis actually translates into
actionable setups.


==================================================
40. ALERT SYSTEM
==================================================

Support alerts such as:

HIGH PRIORITY

BTC broke support while:
US10Y +8bp
DXY +0.5%

Macro + technical signals aligned.

or:

DIVERGENCE

Oil +4%
US10Y +6bp
DXY +0.4%
BTC still +1.2%

BTC showing unusual relative strength.

Support:

in-app alerts
browser notifications

Architect adapters for future:

Telegram
email
mobile push


==================================================
41. DAILY MARKET BRIEF
==================================================

Automatically generate a morning brief.

Structure:

CONFIRMED FACTS

GLOBAL MARKET STATE

TOP CATALYSTS

TODAY'S SCHEDULE
all times Europe/Bucharest

CENTRAL BANKS

MACRO

GEOPOLITICS

REGULATION

CRYPTO

FX/USD

BONDS

GOLD

OIL

EQUITIES

RISK MAP

LONG / SHORT / WAIT SETUPS

CONFIRMATION / INVALIDATION

Do not include low-impact filler.


==================================================
42. INTRADAY UPDATE
==================================================

Generate an intraday briefing when:

major event appears
macro data is released
market regime changes
asset score changes > threshold
important technical level breaks

The update must explicitly say:

WHAT CHANGED SINCE LAST UPDATE.


==================================================
43. SECURITY
==================================================

API keys only backend.

Use environment variables.

Provide .env.example.

Never commit secrets.

Validate all external content.

Treat website/X/article content as untrusted data.

Prevent prompt injection from source documents.

External article instructions such as:

"Ignore previous instructions"

must be treated as article text and NEVER executed.

Add prompt-injection defenses around all AI ingestion.


==================================================
44. AI STRUCTURED OUTPUT
==================================================

Use strict schemas.

Validate every AI response with Pydantic.

If invalid:

retry once with schema correction.

If still invalid:

mark AI run failed.

Do not publish malformed output.


==================================================
45. AI RUN LOGGING
==================================================

Store:

agent
model
model version/name
prompt template version
input hash
output
latency
tokens if available
success/failure
timestamp

This allows exact auditing.


==================================================
46. DATA FRESHNESS
==================================================

Every data class must have freshness thresholds.

Example:

live market data:
seconds

breaking news:
minutes

macro releases:
event-specific

If stale:

show STALE prominently.

Signals relying on stale critical data must be blocked.


==================================================
47. MARKET HOURS
==================================================

Implement actual market session logic.

Crypto:
24/7

FX:
weekend closure

US equities:
regular session plus pre/post if provider supports

Treasury:
appropriate session logic

US holidays:
official calendar

On weekends do not display Friday stock closes as LIVE.

Display:

LAST CLOSE
FRIDAY CLOSE
AFTER HOURS
PREMARKET
LIVE

correctly.


==================================================
48. ERROR HANDLING
==================================================

Implement:

timeouts
retry with exponential backoff
circuit breakers
rate-limit handling
provider fallback
health monitoring

A provider failure must not crash the entire application.


==================================================
49. DEMO / MISSING CREDENTIALS
==================================================

Some paid APIs may not initially have credentials.

Build their provider interfaces anyway.

If credentials are absent:

display provider as NOT CONFIGURED.

You may provide clearly labelled DEMO fixtures for development.

DEMO data must NEVER be displayed as production/live data.

No fabricated market data.


==================================================
50. PERFORMANCE
==================================================

The browser should not rerender for every tick.

Throttle visual updates.

Store raw ticks only when necessary.

Aggregate bars:

1s
5s
1m
5m
1h

where appropriate.

Use TimescaleDB hypertables.


==================================================
51. DATABASE ENTITIES
==================================================

At minimum implement:

providers
provider_health

sources
source_reliability

raw_documents
social_posts

events
event_evidence
event_relationships

market_ticks
market_bars

assets

macro_calendar
macro_releases

central_bank_state

derivative_metrics

technical_levels

market_regimes

causal_rules

ai_models
ai_role_assignments
ai_runs

asset_scores
score_components

decisions
decision_components

alerts

signal_outcomes
paper_trades

calibration_models

configuration_versions

audit_logs


==================================================
52. AUDITABILITY
==================================================

Every published decision must be reproducible.

Example:

Decision ID:
D-20260926-01827

BTC
WAIT
score -24

Generated:
13:51:32 UTC

Based on:
14 market metrics
7 verified events
2 macro events
1 geopolitical event

Scoring:
v1.3

Causal rules:
v1.7

Source weights:
v1.2

AI:
models listed

Auditor:
PASS


==================================================
53. TESTING
==================================================

Tests are mandatory.

UNIT TESTS

scoring
fact confidence
timezone conversion
market hours
freshness
event dedupe
surprise z-score
decision gates

INTEGRATION TESTS

provider ingestion
Ollama structured output
market update
event verification
decision production

END-TO-END

open dashboard
receive market update
ingest event
verify event
recalculate asset
publish decision card

CRITICAL REGRESSION TESTS:

1.
stale BTC web article must not overwrite live BTC price.

2.
unverified X rumor cannot create STRONG LONG/SHORT.

3.
Friday stock close on Saturday must display LAST CLOSE, not LIVE.

4.
Fed hike must not automatically create LONG USDJPY without comparing BoJ.

5.
calls > puts must not automatically make BTC bullish.

6.
max pain must not be shown as predicted target.

7.
old news must not be presented as current.

8.
Romania time must correctly account for DST.


==================================================
54. IMPLEMENTATION PHASES
==================================================

Implement sequentially.

PHASE 1
foundation
Docker
database
FastAPI
Next.js
Redis
health checks
config

PHASE 2
asset model
market data provider interfaces
WebSockets
live market strip

PHASE 3
news ingestion
official source ingestion
Ollama web search
X architecture
raw document store

PHASE 4
normalization
deduplication
event system
verification

PHASE 5
Ollama model registry
AI role system
structured outputs

PHASE 6
economic calendar
central banks
macro surprise engine

PHASE 7
causal engine
market reaction engine
cross-asset monitoring

PHASE 8
scoring
decision gates
LONG / SHORT / WAIT

PHASE 9
dashboard UI
asset pages
calendar
events
FX

PHASE 10
Chief Analyst
Auditor
daily brief
intraday brief

PHASE 11
alerts
X verification workflow

PHASE 12
outcome tracking
paper trading

PHASE 13
autolearn
calibration
champion/challenger

PHASE 14
full tests
security
performance
documentation


==================================================
55. AFTER EVERY PHASE
==================================================

You MUST:

run tests
fix failures
run lint/type checking
verify application starts
update PROGRESS.md
document unfinished items

Do not continue while the current phase is broken.


==================================================
56. CODING QUALITY
==================================================

Use:

typed code
small modules
clear interfaces
dependency injection where useful
repository/service separation
Pydantic schemas
database migrations
no giant files
no duplicated logic
no magic constants

All thresholds belong in configuration.

Avoid hardcoding.


==================================================
57. DO NOT
==================================================

DO NOT:

fake live data
invent API responses
hardcode Ollama model names
hardcode current prices
let AI choose its own permissions
allow AI to change production weights
allow AI to change source trust directly
use an LLM for arithmetic that code can perform
use an LLM for timezone conversion
use an LLM as database
use news articles as live prices
publish unverified social rumors as fact
call every market move LONG or SHORT
claim statistical probability without calibration
silently ignore provider outages
rebuild existing working code without reason


==================================================
58. PRODUCT OBJECTIVE
==================================================

The user should be able to open the app and understand the market in less
than 10 seconds.

The UI should answer only five primary questions:

1. WHAT IS THE MARKET DOING?

2. WHAT MATTERS RIGHT NOW?

3. WHAT IMPORTANT EVENTS ARE NEXT?

4. WHERE ARE THE BEST LONG / SHORT / WAIT SETUPS?

5. WHAT CONFIRMS OR INVALIDATES EACH SETUP?


The backend can be complex.

The frontend must remain simple, factual and high-impact.


==================================================
59. SUCCESS CRITERIA
==================================================

A successful production flow looks like this:

1. A credible X account reports an explosion near Hormuz.

2. System creates UNVERIFIED event.

3. Research Planner triggers targeted search.

4. Reuters publishes confirmation.

5. Fact Checker changes event to CONFIRMED.

6. Brent moves +2.8%.

7. Inflation-risk causal rules activate.

8. US Treasury futures react.

9. DXY changes.

10. BTC changes.

11. Market Reaction Engine measures alignment.

12. Asset scoring recalculates.

13. BTC changes from:
WAIT
to
BEARISH LEAN

14. Only after technical confirmation and sufficient evidence can it become:
SHORT.

15. Chief Analyst explains:
WHY.

16. Auditor verifies every fact and price.

17. UI updates.

18. Outcome tracker measures result.

19. Autolearn stores performance.

20. Future scoring is recalibrated only after statistically valid testing.


==================================================
60. FINAL IMPLEMENTATION RULE
==================================================

Do not finish by producing only plans, mockups or TODO lists.

Implement the system.

When an external paid credential is required and unavailable:
implement the complete adapter and configuration,
provide a clearly labelled development stub,
continue with the remainder of the system.

Do not invent credentials.

Do not fake production success.

At the end provide:

1. what is fully working;
2. what requires API credentials;
3. provider setup instructions;
4. architecture diagram;
5. database diagram;
6. test results;
7. remaining technical debt;
8. exact commands to launch locally;
9. exact commands to launch with Docker;
10. screenshots or browser verification of all core pages.

Start by:
1. auditing the repository;
2. writing PROJECT_SPEC.md and ARCHITECTURE.md;
3. producing the implementation plan in PROGRESS.md;
4. immediately starting PHASE 1.