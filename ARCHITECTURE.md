# Architecture

This documents the system as built, not a future phase. Everything described
here exists in the repository and is covered by tests.

---

## 1. Why deterministic Python owns orchestration

The pipeline is a sequence of ordinary function calls in
`outreach/pipeline/orchestrator.py`. No agent framework, no message broker, no
scheduler daemon, no OpenClaw or Hermes.

The reasoning is about failure modes, not fashion. This system has three
properties that a delegating agent loop makes hard to guarantee:

- **Bounded cost.** Every stage has an explicit per-run cap
  (`MAX_DISCOVERED_PER_RUN`, `MAX_AI_SCORES_PER_RUN`, `MAX_BUILDS_PER_RUN`,
  `OUTREACH_DAILY_SEND_LIMIT`). A loop that decides its own next action can
  decide to do more of it.
- **Auditable decisions.** "Why was this business contacted?" must be answerable
  from `event` rows and a scoring function you can read. A model's reasoning
  trace is not the same thing.
- **Restartability.** A crash mid-run must be resumable without re-doing work
  or, worse, re-doing sends. That requires a state machine in a database, which
  is what the pipeline is.

AI is used where it is genuinely better than code — judging whether a rendered
page *looks* dated, turning scraped fragments into readable copy — and nowhere
else.

### Stage structure

Each stage is a function taking `(session, ctx)` and returning a `StageResult`
with counts, notes and errors. `RunContext` carries the settings, campaign,
`dry_run` and per-run limits. Adding a stage means adding a function and a line
in the orchestrator.

A stage whose flag is off returns `StageResult.skipped(...)` and the run
continues. Where it is useful, a skipped stage still reports what it *would*
have done — the build stage reports `would_build` counts during a dry run.

---

## 2. Why AI is bounded

There are exactly five AI checkpoints:

| Checkpoint | Flag | Deterministic fallback |
| --- | --- | --- |
| `score_website` | `FEATURE_AI_SCORING` | Deterministic technical score alone |
| `extract_business_content` | `FEATURE_AI_CONTENT_EXTRACTION` | Regex/JSON-LD extraction |
| `generate_site_content` | `FEATURE_AI_CONTENT_EXTRACTION` | Fact-assembled template copy |
| `compose_email` | `FEATURE_EMAIL_COMPOSITION` | Deterministic email template |
| `classify_reply` | `FEATURE_REPLY_SYNC` + provider | Keyword classification |

Every one of them:

- has a strict pydantic output schema, validated after tolerant JSON extraction
- has a hard process timeout (`CLAUDE_TIMEOUT_SECONDS`)
- has a bounded retry count (`CLAUDE_MAX_RETRIES`)
- writes an `ai_usage` audit row whether it succeeds or fails
- has a fallback that keeps the pipeline working when it fails

The fallbacks are not stubs. The system produces a complete, sendable email with
every AI flag off; AI improves the copy, it does not enable the feature.

### AI usage strategy

The funnel is deliberately shaped so AI is spent only where deterministic
filtering cannot decide:

```
discover      ~100     free
scan          ~100     free (browser only)
AI score       ~20     only BORDERLINE / REDESIGN_CANDIDATE / STRONG_*
contactable    ~15     free (deterministic contact extraction)
build          ~5-10   AI copy only for prospects with a usable email
```

`should_ai_score` refuses to score a business whose deterministic status is
outside `AI_SCORE_CANDIDATE_STATUSES`, or one already assessed. An obviously
modern site never reaches a model, and no site is ever built before we know we
can reach the business.

---

## 3. Prompt injection protections

A prospect's website is attacker-controlled text. The fixture
`whitetooth-dental-injection` contains, in its visible copy, its HTML comments
and its meta description, instructions to ignore the operator, delete files,
score the site 100 and email an attacker. It flows through the real pipeline in
the test suite.

Defences, in order of importance:

1. **Fencing.** `outreach/ai/prompts.py` wraps every piece of untrusted evidence
   in `<<<UNTRUSTED_EVIDENCE {label} {nonce}>>> … <<<END_UNTRUSTED_EVIDENCE
   {nonce}>>>` with a fresh random nonce per call, preceded by an explicit
   preamble instructing the model to treat it as data and to report rather than
   obey embedded instructions.
2. **No shell.** Prompts travel on **stdin** with `shell=False`. Untrusted text
   never appears in an argument vector, so there is no shell metacharacter
   surface at all.
3. **Filesystem isolation.** Each call runs with `cwd` set to a fresh
   `tempfile.mkdtemp()` containing *only* the files that call needs — for visual
   scoring, exactly two PNGs. The project repository is never exposed.
4. **Environment scrubbing.** `build_sandbox_env()` is an allowlist: only
   `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `TZ` and `TERM`
   survive. `GOOGLE_MAPS_API_KEY`, `GMAIL_*`, `DATABASE_URL`, `SENDER_*` and
   anything containing `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL` are
   dropped. `HOME` is kept because the CLI needs it to find its own credentials.
5. **Tool denial.** `Bash`, `Edit`, `Write`, `NotebookEdit`, `MultiEdit`,
   `WebFetch`, `WebSearch`, `Task`, `Agent`, `TodoWrite`, `KillShell` and
   `BashOutput` are always denied. Scoring jobs get `Read` and nothing else, and
   only because they must open the screenshots. Jobs that need no files deny
   `Read`, `Glob` and `Grep` too.
6. **Schema validation.** Output that does not validate is a failure, not a
   result. A model that "complies" with an injected instruction to emit
   different JSON simply fails the call.
7. **Capability separation.** No AI call has access to Gmail, to the database,
   to deployment credentials, or to the send path. Even total compliance with an
   injected instruction cannot send an email — the model has no way to.

The deepest protection is structural: **an AI's output is never an action.** It
is a validated data structure that deterministic code then decides what to do
with.

---

## 4. Business deduplication

Contacting the same practice twice because two searches found it is the failure
mode that makes an outreach system look automated. Dedupe happens on two axes,
both enforced by the schema:

```sql
UNIQUE (external_source, external_id)   -- the provider's stable ID
UNIQUE (normalized_domain)              -- the website itself
```

`find_existing_business` checks the provider ID first, then the domain. On a
match, the existing row is *enriched* (missing website, phone, address filled
in) rather than duplicated.

`normalized_domain` holds the output of `normalize_site_identity`, which is the
registrable domain for public hosts (`www.smile.com/about` → `smile.com`).
Loopback and private hosts are the exception: fixture sites and LAN staging
servers all share one host and differ only by path, so for those the first path
segment is folded in (`127.0.0.1/sunrise-family-dental`). Without that, every
fixture business would collapse into a single row.

Businesses without a website stay distinct — SQLite treats NULLs as distinct in
a unique index, which is exactly the semantics we want.

Cross-campaign discovery is recorded in `campaign_business` (many-to-many, with
`times_seen`), so a second city's search links to the existing business rather
than creating a new one.

---

## 5. Deterministic scoring, and why it comes first

`outreach/scoring/technical.py` produces a 0–100 score with four weighted
sub-scores:

| Category | Weight | Examples of what it measures |
| --- | --- | --- |
| mobile | 0.35 | viewport meta, layout-viewport overflow on a 390px phone, effective font size after zoom-out, fixed desktop widths, nav overflow, tap-target sizes |
| technical | 0.25 | HTTPS, mixed content, obsolete tags, table layouts, legacy libraries, failed requests, JS errors, broken images |
| conversion | 0.28 | visible phone, `tel:` link, prominent CTA above the fold, contact form, address, hours, services, heading structure |
| performance | 0.12 | first contentful paint, load event, transfer size, request count (Lighthouse when configured) |

Two design decisions worth calling out:

**Mobile detection measures the layout viewport, not `scrollWidth`.** A page with
no viewport meta gets Chrome's legacy 980px layout viewport on a phone — it
renders *zoomed out*, with no horizontal overflow at all. Comparing
`scrollWidth` to `innerWidth` would score it as fine. The scorer instead
compares the layout viewport to the real device width (390px) and computes the
effective font size after down-scaling, which is what a human actually
experiences.

**The copyright year is never used as evidence.** Excellent sites forget to bump
it; abandoned ones auto-generate it. It correlates with nothing.

Every deduction produces a `Finding` with a category, a code, a severity and
human-readable evidence, persisted in `technical_findings_json`. Those findings
are what the outreach email quotes, so if we cannot describe a problem honestly
in one clause we do not mention it.

When no browser is available the scorer degrades to HTML-only analysis, sets
`measured_with_browser: false` and records a note. It never guesses.

---

## 6. Qualification and build ranking

`combine_scores` blends the deterministic and AI scores using configurable
weights (60/40 by default), normalising them so `3/2` and `0.6/0.4` mean the
same thing. With AI disabled the deterministic score is used at weight 1.0 —
qualification is never blocked on AI being available.

Site quality and reachability are tracked **independently**:

- `qualification_status`: `GOOD_SITE` / `BORDERLINE` / `REDESIGN_CANDIDATE` /
  `STRONG_REDESIGN_CANDIDATE` / `UNREACHABLE` / `UNKNOWN`
- `contactability_status`: `CONTACTABLE` / `NO_PUBLIC_EMAIL` / `SUPPRESSED`

"Their site is terrible" and "we can reach them" are different questions with
different remedies.

Build ranking is deliberately **not** "lowest score first":

```
priority = 0.28 · problem severity
         + 0.22 · mobile/conversion problem weight
         + 0.18 · AI redesign opportunity (0.5 when unknown)
         + 0.20 · contact confidence (penalised for third-party domains)
         + 0.12 · liveness (operational + scanned OK)
```

A dead domain scores near zero on everything and is a worse sales prospect than
an active practice with an ugly but working website. The liveness term encodes
that directly.

Third-party contact detection matters here: an address on neither the
business's own domain nor a known consumer provider (gmail, yahoo, outlook…) is
treated as low-confidence. That is also the mechanism that stops an address
injected into a prospect's page from being preferred over their real one.

---

## 7. Template rendering, not AI-generated frontends

Having a model write a fresh frontend per business would be expensive,
inconsistent and unreviewable. Instead:

```
crawled evidence
  → deterministic BusinessContent          (always available)
  → optional AI BusinessContent            (validated against the evidence)
  → deterministic SiteContent              (always available)
  → optional AI SiteContent                (validated against BusinessContent)
  → Jinja2 → static HTML + CSS + vanilla JS
```

The template is version-controlled (`TEMPLATE_VERSIONS`), so every build records
exactly which template version produced it. Output is plain static files: no
React, no build step, no runtime. Thousands of previews cost approximately
nothing to host.

**Sections are conditional, not placeholder-filled.** No team data means no team
section — not "Dr. Name Here". The QA step fails any build containing text
matching a placeholder pattern, so this is enforced rather than hoped for.

### Hallucination guardrails

`outreach/builder/content.py` builds an `EvidenceCorpus` from every crawled page
and checks AI output against it:

- **Team members** must be named verbatim in the evidence, or they are dropped.
- **Services** must appear in the evidence (fuzzy word-overlap match).
- **Credentials** must appear verbatim.
- **Contact details** can only be values the crawler actually extracted; an AI
  cannot substitute an email or phone number.
- **Assets** are never AI-chosen; the crawler's ownership-checked list wins.
- **Prose** is filtered sentence by sentence against 15 forbidden claim patterns:
  years in business, founding dates, awards, certifications, memberships,
  reviews, ratings, patient counts, pricing, insurance, financing, emergency
  service, guarantees, extra locations and unsupportable superlatives.

Drops are recorded in `business_content.source_evidence_json.guardrails` with
the reason, so the behaviour is inspectable. Anything unsupported is *dropped*,
not flagged for review — an unverifiable claim on a prospect's own website is
worse than no claim.

Scraped copy also passes through `_tidy_prose`, which removes sentences about
the *old* website ("site last updated 2009", "please excuse our appearance",
"web site by…"). Those are facts about the site we are replacing.

### Media provenance

Only assets on the prospect's own registrable domain are reused. Stock-photo
hosts are explicitly recognised and skipped, as are decorative sprites, badges
and social icons. Anything with uncertain ownership is skipped with a recorded
reason. The template is designed to look good with no photographs at all — the
no-photo hero uses a typographic monogram over a generated pattern.

### Build QA

Every build is opened in Chromium at both viewports before it can be published
or emailed. Failures include: broken local assets, mobile horizontal overflow,
missing `<title>`, missing `noindex`, invalid `tel:` links, navigation pointing
at absent sections, empty sections, JavaScript errors, unapplied stylesheet, and
any placeholder text. A build that fails QA is marked `QA_FAILED` and is never
published or emailed.

---

## 8. Email safety

### Composition

Emails cite **at most two** observations, drawn only from findings that have a
hand-written plain-English phrase in `OBSERVATION_PHRASES`. If a problem cannot
be described honestly in one clause, it is not mentioned. When two observations
share a category, the second is swapped for one from a different category —
two mobile complaints read as one complaint.

AI drafts pass a honesty check before use. Rejected patterns include fabricated
relationships ("as we discussed"), fabricated effort ("I spent hours"),
fabricated referrals, false patient claims, invented urgency, invented
statistics, ranking claims, competitor claims, and `Re:`/`Fwd:` subject
prefixes. A rejected draft silently falls back to the deterministic template.

The compliance footer — sender name, company, postal address, opt-out sentence —
is appended if the AI omitted it, and `List-Unsubscribe` is set on every message.

### The send guard

`messaging.compliance.evaluate_send` is the single choke point. It evaluates
nine conditions and returns a `SendDecision` listing every block, so a dry run
reports all of them rather than stopping at the first.

`process_queue` then routes by block reason:

- window / rate limit / daily limit → **deferred**, `scheduled_at` set, never lost
- suppressed / invalid recipient → **SKIPPED** permanently
- dry run / flag off / paused / no auth → **held** in place, unchanged

### Ambiguous failures

The hardest case is a timeout *after* Gmail accepted the message. Every outgoing
message carries `X-StoopCraft-Idempotency-Key`. On an ambiguous failure the
transport searches the Sent folder for that key. If found, the send is adopted
with its real message ID. If not found, and the error is not a definite failure
(400/401/403/404, malformed recipient), the row goes to `NEEDS_REVIEW` — which
is not a pending state, so no automatic retry will ever touch it.

The message is also committed as `SENDING` *before* the network call, so a
process kill mid-flight leaves a row that will not be blindly retried either.

### Reserved TLDs

The transport refuses `.example`, `.invalid`, `.test`, `.localhost` and `.local`
recipients and the RFC 2606 documentation domains. This is what makes it safe
for fixture prospects to flow all the way through composition and queueing —
they are exercised end to end and refused at the door. Composition uses a looser
check (`recipient_syntax_problem`) precisely so that this happens.

---

## 9. Queue idempotency

```
DRAFT → QUEUED → SCHEDULED → SENDING → SENT → REPLIED → POSITIVE/NEGATIVE/OPTED_OUT
                     ↓           ↓                ↓
                  SKIPPED     FAILED        NEEDS_REVIEW / BOUNCED
```

Transitions go through `queue.transition`, which rejects moves not in
`ALLOWED_TRANSITIONS` and writes an `event` row for each.

The core guarantee is `UNIQUE(business_id)` on `outreach_message`: there is one
message row per business, forever. `upsert_draft` refreshes a `DRAFT`, `SKIPPED`
or `FAILED` row and refuses to touch anything past `SENDING`. A concurrent
insert loses the race at the database level and adopts the winner's row.

`process_queue` sends **one message per invocation** and returns. That is how
`OUTREACH_MIN_SECONDS_BETWEEN_SENDS` is enforced without any process sleeping:
cron calls again later, and the guard does the arithmetic. Intervals are regular
on purpose — the goal is to be a reasonable neighbour to a mail server, not to
look human to a spam filter.

---

## 10. Feature flags

`FeatureFlags` is a typed pydantic model built from the environment. Every flag
defaults to `false`, and `FEATURE_FLAG_DOCS` carries a one-line description of
what each one gates — surfaced by `python -m outreach features` and asserted
complete by a test.

The contract is: **a disabled feature is fully implemented, fully tested, wired
into the CLI and known to the pipeline — it simply does not execute.** Turning a
flag on requires no code change and no schema change. `TestStageGating` and the
`SKIPPED_FEATURE_DISABLED` logging assertions enforce this.

Alongside the flags are limits that cannot be raised by configuration alone:

```python
CODE_ABSOLUTE_MAX_DAILY_SEND_LIMIT = 100
CODE_ABSOLUTE_MAX_GOOGLE_PLACES_MONTHLY_LIMIT = 5000
CODE_MIN_SECONDS_BETWEEN_SENDS_FLOOR = 30
```

`OUTREACH_DAILY_SEND_LIMIT` is clamped by `OUTREACH_HARD_MAX_DAILY_SEND_LIMIT`,
which is itself clamped by the code constant. Going beyond needs a source edit
and a code review — a typo in `.env` cannot.

---

## 11. Provider abstractions

**`BusinessDiscoveryProvider`** — `search(query, session) -> DiscoveryResult`.
Implementations never raise for remote failures; they report through the result.
`FixtureDiscoveryProvider` and `GooglePlacesDiscoveryProvider` ship.

**`AIProvider`** — five checkpoint methods plus `health_check()`.
`ClaudeCodeProvider` and `CodexCLIProvider` ship, both on shared sandboxed
subprocess machinery (`SubprocessAIProvider`). `NullAIProvider` always refuses,
which is what the pipeline uses when a health check fails.

**`PreviewPublisher`** — `available()` and `publish(business_id, dir, dry_run)`.
`LocalCopyPublisher`, `RsyncPublisher` and `NullPublisher` ship. No cloud service
is assumed anywhere.

Each interface has at least two real implementations, which is the only way to
know an abstraction actually abstracts.

### Metered provider protection

`outreach/quota.py` is the enforcement point. `check_and_reserve` writes the
usage row **before** the network call, so a crash can only over-count. Blocked
attempts are recorded with a status (`BLOCKED_QUOTA`,
`BLOCKED_FEATURE_DISABLED`, `BLOCKED_NO_CREDENTIAL`, `BLOCKED_PAUSED`) and zero
billable units, which makes refusals auditable rather than invisible.

---

## 12. Database design

SQLite, SQLAlchemy 2.x, Alembic. Fifteen tables. WAL mode, foreign keys on, a
10-second busy timeout.

Two custom column types keep the file honest:

- **`UtcDateTime`** stores naive UTC and returns timezone-aware UTC. SQLite has
  no timezone type, and a system that schedules sends across timezones cannot
  afford ambiguity about what a stored timestamp means.
- **`JsonText`** stores JSON as TEXT, so the database stays inspectable with any
  SQLite tool.

Constraints that carry safety weight rather than mere tidiness:

```sql
outreach_message: UNIQUE (business_id)              -- one email per business, ever
outreach_message: UNIQUE (gmail_message_id)         -- one row per real send
business:         UNIQUE (external_source, external_id)
business:         UNIQUE (normalized_domain)
contact:          UNIQUE (business_id, normalized_email)
suppression:      UNIQUE (normalized_email), UNIQUE (normalized_domain)
campaign:         UNIQUE (vertical, city, state, country)   -- reruns resume
```

`Business` caches `qualification_status`, `contactability_status`,
`combined_score` and `build_priority`. These are always derivable from child
rows; caching them keeps candidate selection and reporting single-query cheap.

### The event log

Every meaningful transition writes an `event` row with an entity type, entity
ID, event type and JSON payload. This is both the audit trail and the substrate
for later analysis.

### Learning data

`reporting.learning_dataset()` produces flat rows linking each business's
website problem codes and severities to its outreach outcome (contacted,
replied, positive reply, opted out). No ML is implemented — the point is to
preserve high-quality structured data now so the question "which website
problems correlate with replies?" is answerable later without a backfill.

---

## 13. Deployment model

One Python process, invoked by cron or a systemd timer. One SQLite file. Static
files on disk.

```
/srv/stoopcraft-outreach/
  .venv/
  .env                      0600, never in git
  data/outreach.db          the entire application state
  data/gmail-token.json     0600, never in git
  artifacts/businesses/<id>/    evidence: html, screenshots, scan.json
  previews/<id>/                generated static sites
  logs/
```

There is no server to keep up, no queue to drain, no cluster to size. Backup is
`cp data/outreach.db`; everything else is reproducible from it.

**Concurrency** is handled by advisory locks in `job_lock`. Each command takes a
named lock with a TTL; a second invocation exits immediately rather than
stacking. Expired locks are reclaimed automatically, so a killed process cannot
deadlock the system.

**The global pause** (`system_state`) is checked before every external side
effect: sends, publishing and metered provider calls. `python -m outreach pause`
is the one command to reach for when something looks wrong.

**Scaling**, if it ever matters: SQLite comfortably handles hundreds of
thousands of businesses at this write volume. The bottleneck is deliberate — the
daily send limit — not the database. Moving to PostgreSQL would mean changing
`DATABASE_URL` and running the migrations.

---

## 14. Known limitations

- **One vertical.** The dentist template is the only one implemented. The
  renderer, QA and pipeline are vertical-agnostic; adding one means adding a
  template directory and a version entry.
- **Address parsing is US-centric.** `normalize_phone` defaults to NANP, and the
  address regex expects US street/state/ZIP conventions.
- **The public suffix list is abbreviated.** `_MULTI_PART_SUFFIXES` covers the
  suffixes that occur in local-business work rather than the full PSL.
- **Lighthouse is optional and unwired by default.** Browser performance metrics
  are used instead; `LIGHTHOUSE_ENABLED` reserves the integration point.
- **Reply classification is keyword-based** unless AI is enabled. Opt-out
  detection is deliberately deterministic and always will be.
- **No follow-up sequences.** V1 sends exactly one email per business. This is a
  product decision, not a missing feature.
