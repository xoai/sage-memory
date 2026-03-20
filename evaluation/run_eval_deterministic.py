#!/usr/bin/env python3
"""sage-memory Evaluation 1 & 2 — Deterministic (no LLM judge)

Eval 1: Self-Learning Retrieval Effectiveness
  Tests: does the right prevention rule surface for the right task?
  Method: store 50 prevention rules, run 50 task queries, check if
  the correct rule appears in top-5 results via keyword matching.
  Fully deterministic, reproducible, no API key needed.

Eval 2: Knowledge Context Coverage
  Tests: does search return entries containing the answer's key facts?
  Method: store 30 knowledge entries, ask 30 questions, check if
  results contain the expected keywords from ground truth.
  Fully deterministic, reproducible, no API key needed.

Usage:
  PYTHONPATH=src python evaluation/run_eval_deterministic.py
"""

import json, os, sys, time, shutil, tempfile, statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"

from sage_memory.db import override_project_root, close_all
from sage_memory.store import store
from sage_memory.search import search

TMPDIR = Path(tempfile.mkdtemp())


def setup(name):
    close_all()
    p = TMPDIR / name
    if p.exists(): shutil.rmtree(p)
    p.mkdir(parents=True); (p / ".git").mkdir()
    override_project_root(p)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EVAL 1: Self-Learning Retrieval — 50 tasks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Each entry: a prevention rule (stored) and a task query (searched).
# Ground truth: which prevention rule(s) should surface for which task.
# Scoring: keyword match — does the retrieved result contain the
# expected keywords from the correct prevention rule?

LEARNINGS = [
    # Stripe (8)
    {"id":"L01","title":"[LRN:gotcha] Stripe webhook requires raw body before JSON parsing","content":"Prevention: Before implementing any webhook handler that verifies signatures, check whether the SDK requires the raw request body. If yes, ensure body parsing middleware is skipped.","tags":["self-learning","gotcha","stripe","webhooks"],"keywords":["raw body","signature","body parsing","middleware"]},
    {"id":"L02","title":"[LRN:gotcha] Stripe API amounts must be in cents not dollars","content":"Prevention: Before passing any monetary amount to a payment API, check the unit. Stripe, Square, and most payment APIs use smallest currency unit (cents).","tags":["self-learning","gotcha","stripe","payments","currency"],"keywords":["cents","smallest currency","amount","unit"]},
    {"id":"L03","title":"[LRN:gotcha] Stripe webhook idempotency for retries","content":"Prevention: When implementing any webhook handler, add idempotency check using the event ID. Stripe retries on non-2xx within 5 seconds.","tags":["self-learning","gotcha","stripe","webhooks","idempotency"],"keywords":["idempotency","event id","retries","duplicate"]},
    {"id":"L04","title":"[LRN:gotcha] Stripe webhook signature error misleads about root cause","content":"Prevention: When Stripe webhook verification fails, check body format first (raw vs parsed), signing secret second. The error message is misleading.","tags":["self-learning","gotcha","stripe","webhooks","signature"],"keywords":["body format","signing secret","misleading","verification"]},
    {"id":"L05","title":"[LRN:gotcha] Payment amount display timezone offset UTC vs PST","content":"Prevention: Before displaying any date or time to users, check if backend stores UTC and frontend needs local timezone conversion. Billing closes at UTC midnight.","tags":["self-learning","gotcha","billing","timezone"],"keywords":["timezone","utc","conversion","display"]},
    {"id":"L06","title":"[LRN:gotcha] Billing saga compensating transaction must be idempotent","content":"Prevention: When writing saga compensating transactions, always add idempotency check using saga execution ID. Rollback can fire multiple times.","tags":["self-learning","gotcha","billing","saga","idempotency"],"keywords":["saga","compensating","idempotency","rollback"]},
    {"id":"L07","title":"[LRN:gotcha] Legacy billing v1 API requires X-Api-Version header","content":"Prevention: Before calling any versioned API, check if it requires explicit version headers. The v1 billing API returns 400 without X-Api-Version.","tags":["self-learning","gotcha","billing","api","headers"],"keywords":["version header","x-api-version","400","versioned api"]},
    {"id":"L08","title":"[LRN:gotcha] Payment amount rounding causes off-by-one cent errors","content":"Prevention: When calculating payment amounts, use integer arithmetic in cents. Never use floating point for money. Round before converting to cents.","tags":["self-learning","gotcha","payments","rounding"],"keywords":["rounding","integer arithmetic","floating point","cents"]},

    # Docker (7)
    {"id":"L09","title":"[LRN:gotcha] Docker COPY before install invalidates layer cache","content":"Prevention: In Dockerfiles, always copy dependency manifests (package.json, requirements.txt) and install dependencies before copying application code.","tags":["self-learning","gotcha","docker","build-cache"],"keywords":["dependency manifests","install before","layer cache","copy"]},
    {"id":"L10","title":"[LRN:error-fix] Docker platform mismatch on Apple Silicon M1","content":"Prevention: Before writing Dockerfiles, check if the base image supports ARM64. If on Apple Silicon and image is AMD64 only, add --platform flag.","tags":["self-learning","error-fix","docker","arm64","apple-silicon"],"keywords":["arm64","apple silicon","platform","amd64"]},
    {"id":"L11","title":"[LRN:gotcha] Alpine images lack glibc breaking native Node modules","content":"Prevention: Before choosing Alpine as Docker base, check if application uses native modules (bcrypt, sharp, etc.) that require glibc.","tags":["self-learning","gotcha","docker","alpine","native-modules"],"keywords":["alpine","glibc","native modules","bcrypt"]},
    {"id":"L12","title":"[LRN:gotcha] Multi-stage Docker build leaks secrets in intermediate layers","content":"Prevention: Never copy secrets (env files, keys) in build stages. Use Docker BuildKit secrets mount or multi-stage builds where secrets only exist in builder stage.","tags":["self-learning","gotcha","docker","security","secrets"],"keywords":["secrets","buildkit","intermediate layers","multi-stage"]},
    {"id":"L13","title":"[LRN:gotcha] Docker compose depends_on does not wait for service readiness","content":"Prevention: depends_on only waits for container start, not service readiness. Use healthchecks with condition: service_healthy for database dependencies.","tags":["self-learning","gotcha","docker","compose","healthcheck"],"keywords":["depends_on","healthcheck","service_healthy","readiness"]},
    {"id":"L14","title":"[LRN:error-fix] Docker build context too large slows builds","content":"Prevention: Always create a .dockerignore file excluding node_modules, .git, test fixtures, and other non-essential files before building.","tags":["self-learning","error-fix","docker","dockerignore"],"keywords":["dockerignore","build context","node_modules",".git"]},
    {"id":"L15","title":"[LRN:gotcha] Docker ENTRYPOINT vs CMD signal handling differences","content":"Prevention: Use exec form for ENTRYPOINT (JSON array, not shell string). Shell form wraps in /bin/sh which doesn't forward signals like SIGTERM.","tags":["self-learning","gotcha","docker","entrypoint","signals"],"keywords":["entrypoint","exec form","sigterm","signal","shell form"]},

    # Database (8)
    {"id":"L16","title":"[LRN:correction] Project uses Prisma ORM not raw SQL migrations","content":"Prevention: Before creating database changes, check if the project uses an ORM migration tool (Prisma, Alembic, TypeORM). If yes, use its migration system.","tags":["self-learning","correction","prisma","database","migrations"],"keywords":["prisma","orm","migration tool","schema"]},
    {"id":"L17","title":"[LRN:gotcha] N+1 query pattern causes timeout in ReportBuilder","content":"Prevention: Before adding any new queryset that loads related objects in a loop, check for N+1 patterns. Use prefetch_related or eager loading.","tags":["self-learning","gotcha","database","performance","n-plus-one"],"keywords":["n+1","prefetch","eager loading","queryset","loop"]},
    {"id":"L18","title":"[LRN:correction] Redis EXPIRE TTL is in seconds not milliseconds","content":"Prevention: Before setting cache TTL in Redis, check the unit. EXPIRE and SETEX use seconds. PEXPIRE and PSETEX use milliseconds.","tags":["self-learning","correction","redis","cache","ttl"],"keywords":["seconds","milliseconds","expire","setex","ttl"]},
    {"id":"L19","title":"[LRN:gotcha] Redis SMEMBERS blocks on large sets use SSCAN","content":"Prevention: Before using SMEMBERS, check set size. If over 1000 members, use SSCAN with COUNT parameter to avoid blocking Redis.","tags":["self-learning","gotcha","redis","performance","sets"],"keywords":["smembers","sscan","count","blocking","large set"]},
    {"id":"L20","title":"[LRN:error-fix] Prisma 5.x engine binary not available for Node 21","content":"Prevention: Before upgrading Node.js version, check ORM and database driver compatibility matrix.","tags":["self-learning","error-fix","prisma","node","compatibility"],"keywords":["compatibility","node version","engine binary","prisma"]},
    {"id":"L21","title":"[LRN:gotcha] PostgreSQL connection pool exhaustion under load","content":"Prevention: Always configure connection pool limits matching your database max_connections. Set pool timeout to fail fast rather than queue indefinitely.","tags":["self-learning","gotcha","database","connection-pool","postgresql"],"keywords":["connection pool","max_connections","pool timeout","exhaustion"]},
    {"id":"L22","title":"[LRN:gotcha] Database migration locks table blocking reads in production","content":"Prevention: For large tables, use online migration tools (pt-online-schema-change, gh-ost) or add columns as nullable first, then backfill.","tags":["self-learning","gotcha","database","migrations","locking"],"keywords":["table lock","online migration","nullable","backfill"]},
    {"id":"L23","title":"[LRN:correction] MongoDB ObjectId is not sortable by creation time by default","content":"Prevention: ObjectId contains a timestamp component but sorting by _id only gives approximate chronological order. Use explicit createdAt field for time-based queries.","tags":["self-learning","correction","mongodb","objectid","sorting"],"keywords":["objectid","timestamp","createdAt","sorting","chronological"]},

    # Auth (8)
    {"id":"L24","title":"[LRN:gotcha] JWT refresh token must use httpOnly cookie not localStorage","content":"Prevention: Refresh tokens (long-lived) must use httpOnly cookies. Access tokens (short-lived) should use memory only. Never store tokens in localStorage.","tags":["self-learning","gotcha","auth","jwt","security"],"keywords":["httponly","cookie","localstorage","refresh token","xss"]},
    {"id":"L25","title":"[LRN:gotcha] CORS middleware must come before JWT auth in Express","content":"Prevention: When setting up middleware, ensure CORS handler is registered before auth middleware. OPTIONS preflight requests don't carry auth headers.","tags":["self-learning","gotcha","auth","cors","middleware"],"keywords":["cors","before auth","options","preflight","middleware order"]},
    {"id":"L26","title":"[LRN:gotcha] Token refresh race condition with concurrent requests","content":"Prevention: When implementing token refresh, add a lock mechanism to prevent concurrent refresh attempts. Queue pending requests until refresh completes.","tags":["self-learning","gotcha","auth","token-refresh","race-condition"],"keywords":["lock","concurrent","refresh","queue","race condition"]},
    {"id":"L27","title":"[LRN:gotcha] JWT RS256 requires public key for verification not secret","content":"Prevention: When verifying RS256 JWTs, use the public key (or JWKS endpoint), not the signing secret. HS256 uses shared secret, RS256 uses key pair.","tags":["self-learning","gotcha","auth","jwt","rs256"],"keywords":["rs256","public key","jwks","hs256","key pair"]},
    {"id":"L28","title":"[LRN:gotcha] Session fixation after login without regenerating session ID","content":"Prevention: After successful authentication, always regenerate the session ID. This prevents session fixation attacks where attacker sets a known session ID.","tags":["self-learning","gotcha","auth","session","security"],"keywords":["session fixation","regenerate","session id","authentication"]},
    {"id":"L29","title":"[LRN:gotcha] OAuth state parameter missing enables CSRF on callback","content":"Prevention: Always include a random state parameter in OAuth authorization requests and verify it on callback. Without it, the callback is vulnerable to CSRF.","tags":["self-learning","gotcha","auth","oauth","csrf"],"keywords":["state parameter","oauth","csrf","callback","random"]},
    {"id":"L30","title":"[LRN:gotcha] bcrypt max password length is 72 bytes","content":"Prevention: Before using bcrypt, check input length. bcrypt silently truncates passwords longer than 72 bytes. Pre-hash with SHA-256 if longer passwords are possible.","tags":["self-learning","gotcha","auth","bcrypt","password"],"keywords":["bcrypt","72 bytes","truncate","pre-hash","sha-256"]},
    {"id":"L31","title":"[LRN:gotcha] API rate limiting must use sliding window not fixed window","content":"Prevention: Fixed window rate limiting allows burst at window boundaries (2x rate). Use sliding window or token bucket for consistent rate enforcement.","tags":["self-learning","gotcha","auth","rate-limiting"],"keywords":["sliding window","fixed window","token bucket","burst","rate limit"]},

    # CI/CD (7)
    {"id":"L32","title":"[LRN:correction] Project uses pnpm workspaces not npm","content":"Prevention: Before running any package install command, check for pnpm-lock.yaml, yarn.lock, or bun.lockb in the project root.","tags":["self-learning","correction","pnpm","package-manager"],"keywords":["pnpm-lock","yarn.lock","bun.lockb","package manager","lockfile"]},
    {"id":"L33","title":"[LRN:error-fix] GitHub Actions pnpm requires setup action first","content":"Prevention: When setting up CI with pnpm, always include pnpm/action-setup action before any pnpm commands. Runner doesn't have pnpm by default.","tags":["self-learning","error-fix","ci","github-actions","pnpm"],"keywords":["action-setup","pnpm","github actions","runner"]},
    {"id":"L34","title":"[LRN:convention] Feature flags use LaunchDarkly not env vars","content":"Prevention: Before implementing any feature toggle, check if the project uses a feature flag service like LaunchDarkly, Unleash, or Flagsmith.","tags":["self-learning","convention","feature-flags","launchdarkly"],"keywords":["launchdarkly","feature flag","toggle","env var"]},
    {"id":"L35","title":"[LRN:convention] Error logs must include correlation ID","content":"Prevention: Before adding any logger.error call, include correlationId from the request context as a structured field for request tracing.","tags":["self-learning","convention","logging","observability"],"keywords":["correlation","correlationid","request context","structured","tracing"]},
    {"id":"L36","title":"[LRN:error-fix] CI npm cache invalidated by lockfile changes","content":"Prevention: When caching node_modules in CI, hash the lockfile (package-lock.json or pnpm-lock.yaml) as cache key. Lockfile change should bust the cache.","tags":["self-learning","error-fix","ci","npm","cache"],"keywords":["cache key","lockfile","hash","node_modules","bust"]},
    {"id":"L37","title":"[LRN:gotcha] GitHub Actions matrix strategy fails fast by default","content":"Prevention: Set fail-fast: false in matrix strategy if you want all combinations to run even when one fails. Default true stops all on first failure.","tags":["self-learning","gotcha","ci","github-actions","matrix"],"keywords":["fail-fast","matrix","strategy","false","combinations"]},
    {"id":"L38","title":"[LRN:convention] All API handlers return data error meta envelope","content":"Prevention: Before creating any new API handler, check an existing handler for the response envelope pattern. This project uses {data, error, meta}.","tags":["self-learning","convention","api","response-format"],"keywords":["envelope","data error meta","response format","handler"]},

    # API/SDK (6)
    {"id":"L39","title":"[LRN:api-drift] OpenAI SDK v1.x uses tools param not functions","content":"Prevention: Before using any LLM SDK for function/tool calling, check the installed version and verify current parameter names.","tags":["self-learning","api-drift","openai","tools","sdk"],"keywords":["tools","functions","openai","v1","sdk version"]},
    {"id":"L40","title":"[LRN:api-drift] Anthropic model names changed to claude-sonnet-4","content":"Prevention: Before hardcoding any LLM model name, verify it against the provider's current model list. Model names change across versions.","tags":["self-learning","api-drift","anthropic","claude","models"],"keywords":["model name","claude","deprecated","current","hardcoding"]},
    {"id":"L41","title":"[LRN:gotcha] GraphQL N+1 requires DataLoader pattern","content":"Prevention: In GraphQL resolvers that load related entities, always use DataLoader for batching. Without it, each field resolution triggers a separate query.","tags":["self-learning","gotcha","graphql","dataloader","n-plus-one"],"keywords":["dataloader","batching","resolver","graphql","n+1"]},
    {"id":"L42","title":"[LRN:gotcha] REST API pagination cursor must be opaque not offset","content":"Prevention: For large datasets, use cursor-based pagination (opaque token encoding last seen ID) instead of offset-based. Offset skips are O(n) in most databases.","tags":["self-learning","gotcha","api","pagination","cursor"],"keywords":["cursor","offset","opaque","pagination","o(n)"]},
    {"id":"L43","title":"[LRN:gotcha] WebSocket connection needs heartbeat to detect stale connections","content":"Prevention: Implement ping/pong heartbeat on WebSocket connections. Without it, dead connections are not detected until the next write attempt fails.","tags":["self-learning","gotcha","websocket","heartbeat"],"keywords":["heartbeat","ping","pong","websocket","stale","dead connection"]},
    {"id":"L44","title":"[LRN:gotcha] gRPC deadline propagation requires explicit passing","content":"Prevention: When making downstream gRPC calls, explicitly propagate the deadline from the incoming request context. Deadlines don't propagate automatically.","tags":["self-learning","gotcha","grpc","deadline"],"keywords":["deadline","propagation","context","grpc","downstream"]},

    # Testing (6)
    {"id":"L45","title":"[LRN:convention] Test fixtures use module-scoped database sessions","content":"Prevention: Before creating DB test fixtures, check conftest.py for existing fixture scope patterns. This project uses module-scoped for performance.","tags":["self-learning","convention","testing","pytest","database"],"keywords":["module-scoped","fixture","conftest","scope","database"]},
    {"id":"L46","title":"[LRN:gotcha] Mocking datetime.now breaks other time-dependent code","content":"Prevention: Instead of mocking datetime.now globally, inject a clock/time provider dependency. Or use freezegun which handles all datetime references.","tags":["self-learning","gotcha","testing","mocking","datetime"],"keywords":["freezegun","datetime","mock","clock","inject"]},
    {"id":"L47","title":"[LRN:gotcha] Test database not isolated between parallel test runs","content":"Prevention: Use unique database names or schemas per test worker when running tests in parallel. Or use transactions that rollback after each test.","tags":["self-learning","gotcha","testing","database","isolation"],"keywords":["parallel","isolation","rollback","transaction","unique database"]},
    {"id":"L48","title":"[LRN:gotcha] Snapshot tests break on non-deterministic output","content":"Prevention: Before writing snapshot tests, ensure output is deterministic. Sort object keys, fix timestamps, seed random generators.","tags":["self-learning","gotcha","testing","snapshot","deterministic"],"keywords":["snapshot","deterministic","sort","timestamp","seed random"]},
    {"id":"L49","title":"[LRN:convention] Integration tests use testcontainers for databases","content":"Prevention: Before creating integration test infrastructure, check if the project uses testcontainers. Spin up real databases in Docker for tests.","tags":["self-learning","convention","testing","testcontainers","docker"],"keywords":["testcontainers","docker","integration test","real database"]},
    {"id":"L50","title":"[LRN:gotcha] Flaky test from race condition in async event handler","content":"Prevention: In async tests, always await or explicitly wait for event handlers to complete. Use test utilities like waitFor or flush promises.","tags":["self-learning","gotcha","testing","async","flaky"],"keywords":["flaky","async","await","waitfor","race condition","event handler"]},
]

# Each task maps to 1-3 expected learning IDs
TASKS = [
    # Stripe domain
    {"task": "Implement a Stripe webhook handler for payment_intent.succeeded", "expect": ["L01","L03"], "domain": "stripe"},
    {"task": "Create a charge for 29.99 USD using the Stripe payments API", "expect": ["L02"], "domain": "stripe"},
    {"task": "Debug why Stripe webhook signature verification returns 400", "expect": ["L01","L04"], "domain": "stripe"},
    {"task": "Handle Stripe webhook delivery retries gracefully", "expect": ["L03"], "domain": "stripe"},
    {"task": "Display billing statement dates for users in Pacific timezone", "expect": ["L05"], "domain": "stripe"},
    {"task": "Implement saga rollback for failed multi-service payment", "expect": ["L06"], "domain": "stripe"},
    {"task": "Call the legacy billing v1 REST endpoint", "expect": ["L07"], "domain": "stripe"},
    {"task": "Calculate prorated subscription amount for mid-cycle upgrade", "expect": ["L08","L02"], "domain": "stripe"},
    # Docker domain
    {"task": "Write a Dockerfile for a Node.js Express application", "expect": ["L09"], "domain": "docker"},
    {"task": "Fix Docker build failing on M1 MacBook with platform error", "expect": ["L10"], "domain": "docker"},
    {"task": "Choose a base image for a Node app using bcrypt", "expect": ["L11"], "domain": "docker"},
    {"task": "Secure Docker build to not leak API keys", "expect": ["L12"], "domain": "docker"},
    {"task": "Fix Docker compose service failing because database not ready", "expect": ["L13"], "domain": "docker"},
    {"task": "Speed up slow Docker builds that take 5 minutes", "expect": ["L09","L14"], "domain": "docker"},
    {"task": "Fix Node process not receiving SIGTERM in Docker container", "expect": ["L15"], "domain": "docker"},
    # Database domain
    {"task": "Write a database migration to add email column to users", "expect": ["L16"], "domain": "database"},
    {"task": "Fix API endpoint timing out when loading transaction reports", "expect": ["L17"], "domain": "database"},
    {"task": "Set Redis cache key to expire in 1 hour", "expect": ["L18"], "domain": "database"},
    {"task": "Fetch all members from a Redis set with 50K entries", "expect": ["L19"], "domain": "database"},
    {"task": "Upgrade Node.js from 18 to 21 in the project", "expect": ["L20"], "domain": "database"},
    {"task": "Handle database connection errors under high traffic", "expect": ["L21"], "domain": "database"},
    {"task": "Add a new column to a table with 10M rows in production", "expect": ["L22"], "domain": "database"},
    {"task": "Sort MongoDB documents by creation time", "expect": ["L23"], "domain": "database"},
    # Auth domain
    {"task": "Store authentication refresh tokens in the React frontend", "expect": ["L24"], "domain": "auth"},
    {"task": "Set up Express middleware with CORS and JWT authentication", "expect": ["L25"], "domain": "auth"},
    {"task": "Fix 401 errors when two API calls fire simultaneously", "expect": ["L26"], "domain": "auth"},
    {"task": "Verify RS256 JWT tokens from Auth0 in the backend", "expect": ["L27"], "domain": "auth"},
    {"task": "Secure user sessions after login against fixation attacks", "expect": ["L28"], "domain": "auth"},
    {"task": "Implement Google OAuth login flow with callback", "expect": ["L29"], "domain": "auth"},
    {"task": "Hash user passwords with bcrypt for storage", "expect": ["L30"], "domain": "auth"},
    {"task": "Add rate limiting to the public API endpoints", "expect": ["L31"], "domain": "auth"},
    # CI domain
    {"task": "Install project dependencies in CI pipeline", "expect": ["L32","L33"], "domain": "ci"},
    {"task": "Add a feature toggle for the new billing UI", "expect": ["L34"], "domain": "ci"},
    {"task": "Add structured error logging to API error handler", "expect": ["L35"], "domain": "ci"},
    {"task": "Fix CI builds re-downloading node_modules every run", "expect": ["L36"], "domain": "ci"},
    {"task": "Run test matrix across Node 18, 20, 21 in GitHub Actions", "expect": ["L37"], "domain": "ci"},
    {"task": "Create a new API endpoint for invoice retrieval", "expect": ["L38"], "domain": "ci"},
    # API/SDK domain
    {"task": "Add function calling to OpenAI chat completion", "expect": ["L39"], "domain": "api"},
    {"task": "Update Anthropic SDK model name in the codebase", "expect": ["L40"], "domain": "api"},
    {"task": "Fix slow GraphQL query that loads nested user orders", "expect": ["L41"], "domain": "api"},
    {"task": "Implement pagination for the /api/products endpoint", "expect": ["L42"], "domain": "api"},
    {"task": "Detect and handle dropped WebSocket connections", "expect": ["L43"], "domain": "api"},
    {"task": "Propagate request timeout to downstream gRPC service", "expect": ["L44"], "domain": "api"},
    # Testing domain
    {"task": "Create database fixtures for the new test module", "expect": ["L45"], "domain": "testing"},
    {"task": "Test a function that uses the current timestamp", "expect": ["L46"], "domain": "testing"},
    {"task": "Fix tests failing randomly when run in parallel", "expect": ["L47","L50"], "domain": "testing"},
    {"task": "Add snapshot tests for the invoice PDF renderer", "expect": ["L48"], "domain": "testing"},
    {"task": "Set up integration tests that need a real PostgreSQL", "expect": ["L49"], "domain": "testing"},
    {"task": "Debug flaky test in async webhook processing", "expect": ["L50"], "domain": "testing"},
]

# ── Eval 1 scoring ────────────────────────────────────────

def eval1():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Eval 1: Self-Learning Retrieval (50 tasks, deterministic)║")
    print("╚══════════════════════════════════════════════════════════╝")
    setup("eval1d")

    # Store all learnings
    id_map = {}
    for lrn in LEARNINGS:
        r = store(content=lrn["content"], title=lrn["title"], tags=lrn["tags"], scope="project")
        id_map[lrn["id"]] = {"mem_id": r["id"], "keywords": lrn["keywords"]}

    # Also store some noise (regular knowledge, not self-learning)
    noise = [
        "Billing service uses saga pattern for multi-service atomicity.",
        "Auth service uses JWT with refresh token rotation.",
        "CI pipeline runs on GitHub Actions with pnpm setup.",
        "Docker production uses multi-stage builds with Alpine base.",
        "Redis is used as cache layer with 5 minute default TTL.",
        "PostgreSQL database with Prisma ORM for schema management.",
        "GraphQL API with Apollo Server and DataLoader for batching.",
        "WebSocket connections use socket.io for real-time updates.",
    ]
    for i, n in enumerate(noise):
        store(content=n, title=f"Architecture note {i+1}", tags=["architecture"], scope="project")

    print(f"\n  Stored {len(LEARNINGS)} learnings + {len(noise)} noise entries")

    # Run tasks
    print(f"  Running {len(TASKS)} task queries...\n")

    results = []
    for i, task in enumerate(TASKS):
        t0 = time.perf_counter()
        r = search(query=task["task"], filter_tags=["self-learning"], limit=5)
        lat = (time.perf_counter() - t0) * 1000

        # Check: do any results contain keywords from expected learnings?
        expected_kw_sets = {lid: set(id_map[lid]["keywords"]) for lid in task["expect"]}
        found_expected = set()

        for res in r["results"]:
            text = f"{res['title']} {res['content']}".lower()
            for lid, kws in expected_kw_sets.items():
                hits = sum(1 for kw in kws if kw.lower() in text)
                if hits >= len(kws) * 0.5:  # at least 50% keyword match
                    found_expected.add(lid)

        recall = len(found_expected) / len(task["expect"])
        precision = len(found_expected) / max(len(r["results"]), 1)

        # Check noise leaked in
        noise_count = sum(1 for res in r["results"] if "self-learning" not in res.get("tags", []))

        results.append({
            "task": task["task"][:60], "domain": task["domain"],
            "expect": task["expect"], "found": list(found_expected),
            "recall": recall, "precision": precision,
            "noise": noise_count, "n_results": len(r["results"]), "lat_ms": lat,
        })

    # ── Report ────────────────────────────────────────────
    recalls = [r["recall"] for r in results]
    precisions = [r["precision"] for r in results]
    lats = [r["lat_ms"] for r in results]
    noise_leaks = sum(1 for r in results if r["noise"] > 0)

    print(f"  {'Domain':<10s}  {'Tasks':>5s}  {'Recall':>7s}  {'Prec':>7s}")
    print(f"  {'─'*10}  {'─'*5}  {'─'*7}  {'─'*7}")

    domains = sorted(set(r["domain"] for r in results))
    for d in domains:
        dr = [r for r in results if r["domain"] == d]
        print(f"  {d:<10s}  {len(dr):>5d}  {statistics.mean([r['recall'] for r in dr]):>6.0%}  {statistics.mean([r['precision'] for r in dr]):>6.0%}")

    s = sorted(lats)
    print(f"\n  {'OVERALL':<10s}  {len(results):>5d}  {statistics.mean(recalls):>6.0%}  {statistics.mean(precisions):>6.0%}")
    print(f"\n  Latency: P50={statistics.median(s):.1f}ms  P95={s[int(len(s)*0.95)]:.1f}ms")
    print(f"  Noise isolation: {len(results)-noise_leaks}/{len(results)} queries had zero noise")
    print(f"  filter_tags effectiveness: {(len(results)-noise_leaks)/len(results):.0%}")

    # Per-task detail for failures
    failures = [r for r in results if r["recall"] < 1.0]
    if failures:
        print(f"\n  Partial/missed retrievals ({len(failures)}):")
        for f in failures[:10]:
            print(f"    [{f['domain']:8s}] \"{f['task']}\"  recall={f['recall']:.0%}  expected={f['expect']} found={f['found']}")

    overall = {
        "eval": 1, "method": "deterministic",
        "tasks": len(TASKS), "learnings": len(LEARNINGS), "noise": len(noise),
        "mean_recall": round(statistics.mean(recalls), 3),
        "mean_precision": round(statistics.mean(precisions), 3),
        "perfect_recall": round(sum(1 for r in recalls if r >= 1.0) / len(recalls), 3),
        "noise_isolation": round((len(results)-noise_leaks)/len(results), 3),
        "latency_p50": round(statistics.median(s), 2),
        "latency_p95": round(s[int(len(s)*0.95)], 2),
    }

    print(f"\n  {'─'*50}")
    print(f"  {'Metric':<30s}  {'Value':>8s}  {'Target':>8s}")
    print(f"  {'─'*30}  {'─'*8}  {'─'*8}")
    print(f"  {'Mean recall':<30s}  {overall['mean_recall']:>7.0%}  {'≥ 80%':>8s}  {'✅' if overall['mean_recall']>=0.8 else '❌'}")
    print(f"  {'Perfect recall rate':<30s}  {overall['perfect_recall']:>7.0%}  {'≥ 70%':>8s}  {'✅' if overall['perfect_recall']>=0.7 else '❌'}")
    print(f"  {'Noise isolation':<30s}  {overall['noise_isolation']:>7.0%}  {'≥ 95%':>8s}  {'✅' if overall['noise_isolation']>=0.95 else '❌'}")
    print(f"  {'Latency P95':<30s}  {overall['latency_p95']:>6.1f}ms  {'< 10ms':>8s}  {'✅' if overall['latency_p95']<10 else '❌'}")

    return overall


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EVAL 2: Knowledge Context Coverage — 30 questions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

KNOWLEDGE = [
    {"title": "httpx Client and AsyncClient architecture", "content": "httpx client architecture uses BaseClient for shared config auth headers cookies timeout event_hooks base_url with Client sync and AsyncClient async. Both manage ClientState enum UNOPENED OPENED CLOSED. Transport selection uses URL pattern matching via _mounts dict supporting proxy routing.", "tags": ["client", "architecture", "async"], "facts": ["baseclient", "clientstate", "_mounts"]},
    {"title": "httpx request send pipeline", "content": "When client send is called the pipeline is set timeout build auth flow send_handling_auth enters generator send_handling_redirects loops checking max_redirects firing event hooks send_single_request selects transport calls handle_request wraps response in BoundSyncStream for elapsed tracking.", "tags": ["request", "pipeline", "send"], "facts": ["send_handling_auth", "send_handling_redirects", "boundsyncstream"]},
    {"title": "httpx redirect handling security", "content": "Redirects handled with configurable follow_redirects default False. 303 changes to GET except HEAD. 302 also GET. 301 POST becomes GET. Authorization headers stripped on different origin UNLESS HTTP to HTTPS upgrade same host. Cookie headers stripped rebuilt from client store.", "tags": ["redirect", "security"], "facts": ["follow_redirects", "false", "authorization", "stripped", "cookie"]},
    {"title": "httpx authentication generator auth flow", "content": "Authentication uses generator-based flow. Base Auth class defines auth_flow yielding Request receiving Response. Built-in BasicAuth DigestAuth FunctionAuth NetRCAuth. Client resolves per-request then client-level then URL-embedded credentials.", "tags": ["auth", "authentication"], "facts": ["generator", "auth_flow", "basicauth", "digestauth", "netrcauth"]},
    {"title": "httpx transport httpcore integration", "content": "Transport bridges httpx and httpcore. HTTPTransport wraps httpcore ConnectionPool. SSL via create_ssl_context with certifi. Connection pooling max_connections max_keepalive_connections keepalive_expiry. Exception mapping converts httpcore to httpx.", "tags": ["transport", "httpcore"], "facts": ["httpcore", "connectionpool", "certifi", "exception mapping"]},
    {"title": "httpx timeout per-phase granularity", "content": "Timeouts via Timeout class with per-phase connect read write pool. Default 5 seconds all phases. UseClientDefault sentinel distinguishes omitted from explicit None. Propagates through request extensions.", "tags": ["timeout", "config"], "facts": ["5 seconds", "connect", "read", "write", "pool", "useclientdefault"]},
    {"title": "httpx connection pool limits keepalive", "content": "Limits class max_connections total default 100 max_keepalive_connections idle default 20 keepalive_expiry seconds default 5.0. PoolTimeout raised when pool full.", "tags": ["pool", "limits"], "facts": ["100", "20", "5.0", "pooltimeout"]},
    {"title": "httpx proxy SOCKS configuration", "content": "Proxies via proxy parameter or environment variables HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY when trust_env True. Supports http https socks5 socks5h. SOCKS requires socksio package.", "tags": ["proxy", "socks"], "facts": ["http_proxy", "socks5", "trust_env", "socksio"]},
    {"title": "httpx exception hierarchy", "content": "HTTPError splits into RequestError and HTTPStatusError. TransportError branches TimeoutException NetworkError ProtocolError. Timeout ConnectTimeout ReadTimeout WriteTimeout PoolTimeout. StreamConsumed StreamClosed ResponseNotRead.", "tags": ["error", "exception"], "facts": ["httperror", "requesterror", "httpstatuserror", "transporterror", "pooltimeout"]},
    {"title": "httpx content encoding decompression", "content": "Decoder pipeline supports identity gzip deflate br brotli optional zstd optional. MultiDecoder chains in reverse. Accept-Encoding built from SUPPORTED_DECODERS. ByteChunker TextChunker for fixed-size delivery.", "tags": ["encoding", "compression"], "facts": ["gzip", "deflate", "brotli", "zstd", "multidecoder"]},
    {"title": "httpx request content multipart", "content": "Request encoding handles bytes to ByteStream iterables to IteratorByteStream chunked transfer. Form data dict to URL-encoded. Files to MultipartStream. JSON compact encoding. encode_request dispatches.", "tags": ["content", "multipart"], "facts": ["bytestream", "multipartstream", "chunked", "encode_request"]},
    {"title": "httpx URL parsing WHATWG", "content": "URL handling uses custom WHATWG-inspired parser. URL class provides scheme host port path query fragment. copy_with immutable updates. base_url merging relative URLs. QueryParams immutable multi-dict.", "tags": ["url", "parsing"], "facts": ["whatwg", "copy_with", "base_url", "queryparams"]},
    {"title": "httpx event hooks interception", "content": "Event hooks configured via event_hooks dict with request and response lists. Request hooks fire after build before send. Response hooks after receiving before returning. Called for each request including redirects.", "tags": ["hooks", "middleware"], "facts": ["event_hooks", "request", "response", "redirects"]},
    {"title": "httpx cookie persistence", "content": "Cookies class wrapping CookieJar with automatic persistence. extract_cookies in send_single_request. On redirects Cookie header stripped rebuilt from client store preventing domain leakage.", "tags": ["cookie", "session"], "facts": ["cookiejar", "extract_cookies", "domain leakage"]},
    {"title": "httpx streaming BoundStream", "content": "Streaming via client stream context manager. BoundSyncStream BoundAsyncStream wrap transport tracking response elapsed set when closed not headers. StreamConsumed on re-read.", "tags": ["streaming", "response"], "facts": ["boundsyncstream", "elapsed", "closed", "streamconsumed"]},
    {"title": "httpx sync async differences", "content": "Client uses BaseTransport handle_request SyncByteStream contextmanager. AsyncClient uses AsyncBaseTransport handle_async_request AsyncByteStream asynccontextmanager. HTTP methods GET POST PUT PATCH DELETE HEAD OPTIONS.", "tags": ["sync", "async", "client"], "facts": ["basetransport", "asyncbasetransport", "handle_request", "handle_async_request"]},
    {"title": "httpx transport mount URL routing", "content": "_mounts dict maps URLPattern to transport instances sorted specificity. _transport_for_url iterates first match. Used for proxy routing and custom transport. Mock transports for testing.", "tags": ["transport", "mount"], "facts": ["_mounts", "urlpattern", "_transport_for_url", "specificity"]},
    {"title": "httpx SSL TLS configuration", "content": "SSL via create_ssl_context. Default certifi CA bundle. verify True certifi False disables SSLContext custom. SSL_CERT_FILE SSL_CERT_DIR env vars. HTTP/2 ALPN negotiation.", "tags": ["ssl", "tls"], "facts": ["certifi", "ssl_cert_file", "alpn"]},
    {"title": "httpx vs requests comparison", "content": "httpx modern replacement for requests with async support. Both sync and async. HTTP/2 via h2. httpcore transport layer. WHATWG URL parsing. Does NOT follow redirects by default. Timeout 5s default unlike requests no default.", "tags": ["comparison", "requests"], "facts": ["async", "http/2", "not follow redirects", "5s", "no default timeout"]},
    {"title": "httpx best practices", "content": "Use client instance for connection pooling. Context managers for cleanup. Explicit timeouts never None in production. AsyncClient for async apps. event_hooks for cross-cutting. stream for large responses.", "tags": ["best-practices"], "facts": ["connection pooling", "context manager", "explicit timeout", "stream"]},
]

QUESTIONS = [
    {"q": "What is the default timeout for httpx?", "expect_facts": ["5 seconds"]},
    {"q": "Does httpx follow redirects by default?", "expect_facts": ["false", "not follow"]},
    {"q": "How does httpx handle authentication?", "expect_facts": ["generator", "auth_flow"]},
    {"q": "What happens to Authorization on cross-origin redirect?", "expect_facts": ["stripped"]},
    {"q": "Connection pool defaults in httpx?", "expect_facts": ["100", "20"]},
    {"q": "How to configure proxy in httpx?", "expect_facts": ["http_proxy", "socks5"]},
    {"q": "What is UseClientDefault for?", "expect_facts": ["useclientdefault"]},
    {"q": "How are cookies handled during redirects?", "expect_facts": ["stripped", "domain leakage"]},
    {"q": "How does transport URL routing work?", "expect_facts": ["_mounts", "urlpattern"]},
    {"q": "How is response elapsed time tracked?", "expect_facts": ["boundsyncstream", "closed"]},
    {"q": "Difference between Client and AsyncClient?", "expect_facts": ["basetransport", "asyncbasetransport"]},
    {"q": "What SSL options does httpx support?", "expect_facts": ["certifi"]},
    {"q": "What exception when connection pool is full?", "expect_facts": ["pooltimeout"]},
    {"q": "How does httpx decompress gzip responses?", "expect_facts": ["gzip", "deflate"]},
    {"q": "How to send multipart form data?", "expect_facts": ["multipartstream"]},
    {"q": "How does httpx parse URLs?", "expect_facts": ["whatwg"]},
    {"q": "How do event hooks work?", "expect_facts": ["event_hooks"]},
    {"q": "How is cookie persistence managed?", "expect_facts": ["cookiejar"]},
    {"q": "How does streaming work in httpx?", "expect_facts": ["boundsyncstream", "streamconsumed"]},
    {"q": "What is the httpx request execution pipeline?", "expect_facts": ["send_handling_auth", "send_handling_redirects"]},
    {"q": "How does httpx compare to requests library?", "expect_facts": ["async", "not follow redirects"]},
    {"q": "Best practices for using httpx?", "expect_facts": ["connection pooling", "explicit timeout"]},
    {"q": "What happens with 301 redirect and POST?", "expect_facts": ["post", "get"]},
    {"q": "How does DigestAuth work in httpx?", "expect_facts": ["digestauth"]},
    {"q": "What proxy protocols does httpx support?", "expect_facts": ["socks5"]},
    {"q": "What is httpcore in httpx?", "expect_facts": ["httpcore", "connectionpool"]},
    {"q": "How are redirects handled securely?", "expect_facts": ["authorization", "stripped", "cookie"]},
    {"q": "What is the max keepalive connections default?", "expect_facts": ["20"]},
    {"q": "How to use base_url with relative paths?", "expect_facts": ["base_url"]},
    {"q": "What content encodings does httpx support?", "expect_facts": ["gzip", "brotli"]},
]


def eval2():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Eval 2: Knowledge Context Coverage (30 Qs, deterministic)║")
    print("╚══════════════════════════════════════════════════════════╝")
    setup("eval2d")

    # Store knowledge
    for k in KNOWLEDGE:
        store(content=k["content"], title=k["title"], tags=k["tags"], scope=k.get("scope", "project"))
    print(f"\n  Stored {len(KNOWLEDGE)} knowledge entries")

    # Run questions
    print(f"  Running {len(QUESTIONS)} queries...\n")

    results = []
    for q in QUESTIONS:
        t0 = time.perf_counter()
        r = search(query=q["q"], limit=5)
        lat = (time.perf_counter() - t0) * 1000

        # Check: do retrieved results contain the expected facts?
        all_text = " ".join(f"{x['title']} {x['content']}" for x in r["results"]).lower()
        fact_hits = sum(1 for f in q["expect_facts"] if f.lower() in all_text)
        coverage = fact_hits / len(q["expect_facts"])

        results.append({"q": q["q"][:55], "coverage": coverage, "n_results": len(r["results"]),
                         "expected": q["expect_facts"], "lat_ms": lat})

    coverages = [r["coverage"] for r in results]
    lats = [r["lat_ms"] for r in results]
    s = sorted(lats)

    print(f"  {'Metric':<30s}  {'Value':>8s}  {'Target':>8s}")
    print(f"  {'─'*30}  {'─'*8}  {'─'*8}")
    print(f"  {'Mean fact coverage':<30s}  {statistics.mean(coverages):>7.0%}  {'≥ 80%':>8s}  {'✅' if statistics.mean(coverages)>=0.8 else '❌'}")
    print(f"  {'Perfect coverage rate':<30s}  {sum(1 for c in coverages if c>=1.0)/len(coverages):>7.0%}  {'≥ 70%':>8s}  {'✅' if sum(1 for c in coverages if c>=1.0)/len(coverages)>=0.7 else '❌'}")
    print(f"  {'Questions with results':<30s}  {sum(1 for r in results if r['n_results']>0)/len(results):>7.0%}  {'100%':>8s}  {'✅' if all(r['n_results']>0 for r in results) else '❌'}")
    print(f"  {'Latency P50':<30s}  {statistics.median(s):>6.1f}ms  {'< 5ms':>8s}  {'✅' if statistics.median(s)<5 else '❌'}")

    # Show partial/missed
    partial = [r for r in results if r["coverage"] < 1.0]
    if partial:
        print(f"\n  Partial coverage ({len(partial)}):")
        for p in partial[:8]:
            print(f"    \"{p['q']}\"  {p['coverage']:.0%}  missing={[f for f in p['expected'] if f.lower() not in ' '.join(str(x) for x in [p]).lower()]}")

    return {
        "eval": 2, "method": "deterministic",
        "questions": len(QUESTIONS), "knowledge": len(KNOWLEDGE),
        "mean_coverage": round(statistics.mean(coverages), 3),
        "perfect_coverage": round(sum(1 for c in coverages if c >= 1.0) / len(coverages), 3),
        "latency_p50": round(statistics.median(s), 2),
    }


if __name__ == "__main__":
    r1 = eval1()
    r2 = eval2()

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Eval 1: {r1['tasks']} tasks, {r1['learnings']} learnings")
    print(f"    Recall: {r1['mean_recall']:.0%}  Precision: {r1['mean_precision']:.0%}  Noise isolation: {r1['noise_isolation']:.0%}")
    print(f"  Eval 2: {r2['questions']} questions, {r2['knowledge']} entries")
    print(f"    Fact coverage: {r2['mean_coverage']:.0%}  Perfect: {r2['perfect_coverage']:.0%}")

    out = Path(__file__).parent / "results_deterministic.json"
    with open(out, "w") as f:
        json.dump({"eval1": r1, "eval2": r2}, f, indent=2)
    print(f"\n  Results saved to {out}")

    close_all(); shutil.rmtree(TMPDIR)
