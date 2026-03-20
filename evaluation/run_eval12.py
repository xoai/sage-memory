#!/usr/bin/env python3
"""sage-memory Evaluations 1 & 2

Eval 1: Self-Learning Effectiveness
  - Does the agent avoid mistakes after storing prevention rules?
  - Do prevention rules transfer to new tasks in the same domain?

Eval 2: Knowledge Accumulation Over Sessions
  - Does stored memory improve answer quality vs no memory?

Modes:
  --mode simulated  (default) Uses pre-authored responses to validate scoring
  --mode live       Calls Anthropic API (requires ANTHROPIC_API_KEY env var)

Usage:
  PYTHONPATH=src python evaluation/run_eval12.py --eval 1
  PYTHONPATH=src python evaluation/run_eval12.py --eval 2
  PYTHONPATH=src python evaluation/run_eval12.py --eval all
  PYTHONPATH=src python evaluation/run_eval12.py --eval all --mode live
"""

import json
import os
import sys
import time
import shutil
import tempfile
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"

from sage_memory.db import override_project_root, close_all
from sage_memory.store import store
from sage_memory.search import search

TMPDIR = Path(tempfile.mkdtemp())
SEED_DIR = Path(__file__).resolve().parent / "seed"


def setup_project(name="eval"):
    close_all()
    proj = TMPDIR / name
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    (proj / ".git").mkdir()
    override_project_root(proj)
    return proj


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM interface (simulated or live)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def call_llm_live(system: str, prompt: str) -> str:
    """Call Anthropic API. Requires ANTHROPIC_API_KEY."""
    import urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Set ANTHROPIC_API_KEY environment variable")

    body = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": key,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EVALUATION 1: Self-Learning Effectiveness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Simulated responses: realistic agent outputs for each task
# WITHOUT memory: agent makes the known mistake in most cases
# WITH memory: agent recalls prevention rule and avoids mistake

SIMULATED_PHASE1 = {
    # Baseline: no memory → agent makes mistakes
    "t01": {"makes_mistake": True, "response": "I'll set up the webhook handler. First, parse the JSON body, then verify the signature using stripe.webhooks.constructEvent(req.body, sig, secret)."},
    "t02": {"makes_mistake": True, "response": "To charge $10.00: stripe.charges.create({amount: 10.00, currency: 'usd'})"},
    "t03": {"makes_mistake": True, "response": "The webhook handler processes the event and returns 200. Here's the implementation without any dedup logic."},
    "t04": {"makes_mistake": True, "response": "COPY . /app\nRUN npm install\nThis copies everything then installs dependencies."},
    "t05": {"makes_mistake": True, "response": "FROM node:18-alpine\nThis should work on any platform."},
    "t06": {"makes_mistake": True, "response": "FROM node:18-alpine\nRUN npm install\nAlpine keeps the image small."},
    "t07": {"makes_mistake": True, "response": "I'll create a SQL migration file: ALTER TABLE users ADD COLUMN email VARCHAR(255);"},
    "t08": {"makes_mistake": True, "response": "The endpoint loads each transaction's details in the template loop. Let me check the template."},
    "t09": {"makes_mistake": True, "response": "redis.setex('cache_key', 3600000, value)  # 1 hour TTL"},
    "t10": {"makes_mistake": True, "response": "localStorage.setItem('refreshToken', token)"},
    "t11": {"makes_mistake": True, "response": "app.use(jwtAuth); app.use(cors()); This sets up both middleware."},
    "t12": {"makes_mistake": True, "response": "On 401, call refreshToken() and retry. Each request handles its own refresh independently."},
    "t13": {"makes_mistake": True, "response": "run: pnpm install\nThis should install all dependencies in the CI pipeline."},
    "t14": {"makes_mistake": True, "response": "npm install will set up the dependencies."},
    "t15": {"makes_mistake": True, "response": "Display the created_at timestamp directly from the database in the billing statement."},
    "t16": {"makes_mistake": False, "response": "Before upgrading Node.js, let me check the Prisma compatibility matrix to ensure the engine binaries are available."},
    "t17": {"makes_mistake": True, "response": "I'll add process.env.ENABLE_NEW_BILLING to toggle the feature."},
    "t18": {"makes_mistake": True, "response": "logger.error('Payment failed', { error: err.message }); This logs the error."},
    "t19": {"makes_mistake": True, "response": "const members = await redis.smembers('large_set'); This returns all members."},
    "t20": {"makes_mistake": True, "response": "fetch('/api/v1/billing/invoices') — straightforward API call."},
}

SIMULATED_PHASE3 = {
    # With memory: agent recalls prevention rules and avoids mistakes
    "t01": {"makes_mistake": False, "response": "Before implementing the webhook, I need to check — the prevention rule says to verify if the SDK requires raw body. Stripe does. I'll use express.raw() for this route."},
    "t02": {"makes_mistake": False, "response": "I recall that payment APIs use smallest currency unit. For $10.00, the amount should be 1000 cents."},
    "t03": {"makes_mistake": False, "response": "The prevention rule says to add idempotency check using event ID. I'll check for duplicate event IDs before processing."},
    "t04": {"makes_mistake": False, "response": "Prevention rule: copy dependency manifests first, install, then copy code. COPY package*.json ./\nRUN npm install\nCOPY . ."},
    "t05": {"makes_mistake": False, "response": "Prevention rule: check if base image supports ARM64. Let me verify the node:18-alpine ARM64 availability and add --platform if needed."},
    "t06": {"makes_mistake": False, "response": "Prevention rule: check native module compatibility with Alpine. Let me verify if any dependencies need glibc before choosing Alpine."},
    "t07": {"makes_mistake": False, "response": "Prevention rule: check for ORM migration tool. This project uses Prisma — I'll update prisma/schema.prisma instead of writing raw SQL."},
    "t08": {"makes_mistake": False, "response": "Prevention rule: check for N+1 patterns. The ReportBuilder loads details per row — I'll use prefetch_related for batch loading."},
    "t09": {"makes_mistake": False, "response": "Prevention rule: check Redis TTL unit. SETEX uses seconds, not milliseconds. For 1 hour: redis.setex('key', 3600, value)"},
    "t10": {"makes_mistake": False, "response": "Prevention rule: refresh tokens must use httpOnly cookies, not localStorage, to prevent XSS. Access tokens in memory only."},
    "t11": {"makes_mistake": False, "response": "Prevention rule: CORS before auth middleware. app.use(cors()); app.use(jwtAuth); This ensures OPTIONS preflight works."},
    "t12": {"makes_mistake": False, "response": "Prevention rule: add lock for concurrent refresh. I'll implement a mutex that queues requests while one refresh is in progress."},
    "t13": {"makes_mistake": False, "response": "Prevention rule: include pnpm/action-setup before pnpm commands. Adding the setup step first."},
    "t14": {"makes_mistake": False, "response": "Prevention rule: check lockfile first. I see pnpm-lock.yaml — this project uses pnpm, not npm."},
    "t15": {"makes_mistake": False, "response": "Prevention rule: check timezone conversion. Backend stores UTC, frontend needs PST conversion for the billing display."},
    "t16": {"makes_mistake": False, "response": "Checking Prisma compatibility matrix before upgrading Node.js."},
    "t17": {"makes_mistake": False, "response": "Prevention rule: check for feature flag service. This project uses LaunchDarkly — I'll use their SDK instead of env vars."},
    "t18": {"makes_mistake": False, "response": "Prevention rule: include correlationId. logger.error('Payment failed', { correlationId: req.correlationId, error: err.message })"},
    "t19": {"makes_mistake": False, "response": "Prevention rule: check set size before SMEMBERS. Over 1000 members, use SSCAN with COUNT. Let me check the set size first."},
    "t20": {"makes_mistake": False, "response": "Prevention rule: check for version headers on versioned APIs. Adding X-Api-Version header to the request."},
}

SIMULATED_PHASE4 = {
    # Transfer tasks: does the learning generalize?
    "x01": {"makes_mistake": False, "response": "The Stripe webhook learning says to check if SDK requires raw body for signature verification. GitHub webhooks also verify signatures — let me check if the GitHub SDK needs raw body too."},
    "x02": {"makes_mistake": False, "response": "I remember: payment APIs use smallest currency unit. Square also uses cents — €25.50 = 2550."},
    "x03": {"makes_mistake": False, "response": "Prevention rule about Docker layer caching: copy requirements.txt first, install, then copy code."},
    "x04": {"makes_mistake": False, "response": "Prevention rule about N+1 queries applies here. I'll use select_related/prefetch_related for the Django queryset."},
    "x05": {"makes_mistake": True, "response": "I'll store the OAuth tokens in AsyncStorage. Wait — the prevention rule about localStorage applies to web, not sure about React Native secure storage."},
    "x06": {"makes_mistake": False, "response": "Prevention rule: check lockfile to determine package manager. bun.lockb means this project uses Bun."},
    "x07": {"makes_mistake": False, "response": "Prevention rule about TTL units. Memcached also uses seconds for values under 30 days. For 30 minutes: 1800 seconds."},
    "x08": {"makes_mistake": True, "response": "Setting up Fastify plugins. I'll add the rate limiter and JWT auth. The middleware ordering insight might apply here but Fastify uses a different plugin model."},
}


def eval1_self_learning(mode="simulated"):
    print("╔════════════════════════════════════════════════════════════╗")
    print("║  Evaluation 1: Self-Learning Effectiveness                ║")
    print(f"║  Mode: {mode:<51s}║")
    print("╚════════════════════════════════════════════════════════════╝")

    with open(SEED_DIR / "eval1_self_learning_tasks.json") as f:
        data = json.load(f)

    tasks = data["tasks"]
    transfer_tasks = data["transfer_tasks"]

    # ── Phase 1: Baseline (no memory) ─────────────────────
    print(f"\n  Phase 1: Baseline — {len(tasks)} tasks, no memory")
    setup_project("eval1")

    phase1_mistakes = {}
    for task in tasks:
        tid = task["id"]
        if mode == "simulated":
            result = SIMULATED_PHASE1[tid]
        else:
            system = "You are a senior developer. Complete the task. Be specific about implementation details."
            response = call_llm_live(system, f"Task: {task['task']}\n\nProvide your implementation approach.")
            # Judge: does the response contain the mistake?
            judge_prompt = f"Task: {task['task']}\nKnown mistake: {task['mistake']}\nAgent response: {response}\n\nDoes the agent make the known mistake? Reply ONLY 'yes' or 'no'."
            judgment = call_llm_live("You are an evaluator. Answer only yes or no.", judge_prompt).strip().lower()
            result = {"makes_mistake": judgment.startswith("yes"), "response": response}

        phase1_mistakes[tid] = result["makes_mistake"]

    baseline_rate = sum(phase1_mistakes.values()) / len(phase1_mistakes)
    print(f"  Baseline mistake rate: {baseline_rate:.0%} ({sum(phase1_mistakes.values())}/{len(tasks)})")

    # ── Phase 2: Store prevention rules ───────────────────
    print(f"\n  Phase 2: Storing prevention rules for {sum(phase1_mistakes.values())} mistakes...")
    stored_count = 0
    for task in tasks:
        if phase1_mistakes[task["id"]]:
            store(
                content=f"What happened: {task['gotcha']}\nWhy wrong: {task['mistake']}\nPrevention: {task['prevention']}",
                title=f"[LRN:gotcha] {task['gotcha'][:80]}",
                tags=task["tags"],
                scope="project",
            )
            stored_count += 1
    print(f"  Stored {stored_count} prevention rules")

    # ── Phase 3: Re-test with memory ──────────────────────
    print(f"\n  Phase 3: Re-test — same {len(tasks)} tasks, with memory recall")

    phase3_mistakes = {}
    recall_hits = 0
    for task in tasks:
        tid = task["id"]

        # Search for relevant learnings (the actual sage-memory call)
        r = search(query=task["task"], filter_tags=["self-learning"], limit=5)
        has_relevant = len(r["results"]) > 0

        if has_relevant:
            recall_hits += 1

        if mode == "simulated":
            result = SIMULATED_PHASE3[tid]
        else:
            context = "\n".join(f"- {x['title']}: {x['content']}" for x in r["results"][:3])
            system = "You are a senior developer with access to past learnings. Apply any relevant prevention rules."
            prompt = f"Past learnings:\n{context}\n\nTask: {task['task']}\n\nProvide your implementation approach, applying any relevant prevention rules."
            response = call_llm_live(system, prompt)
            judge_prompt = f"Task: {task['task']}\nKnown mistake: {task['mistake']}\nAgent response: {response}\n\nDoes the agent make the known mistake? Reply ONLY 'yes' or 'no'."
            judgment = call_llm_live("You are an evaluator.", judge_prompt).strip().lower()
            result = {"makes_mistake": judgment.startswith("yes"), "response": response}

        phase3_mistakes[tid] = result["makes_mistake"]

    post_rate = sum(phase3_mistakes.values()) / len(phase3_mistakes)
    avoidance_rate = 1 - (post_rate / max(baseline_rate, 0.001))
    prevention_recall = recall_hits / len(tasks)

    print(f"  Post-learning mistake rate: {post_rate:.0%} ({sum(phase3_mistakes.values())}/{len(tasks)})")
    print(f"  Mistake avoidance rate: {avoidance_rate:.0%}")
    print(f"  Prevention recall: {prevention_recall:.0%} ({recall_hits}/{len(tasks)})")

    # ── Phase 4: Transfer tasks ───────────────────────────
    print(f"\n  Phase 4: Transfer — {len(transfer_tasks)} new tasks, same domains")

    phase4_mistakes = {}
    for ttask in transfer_tasks:
        xid = ttask["id"]

        r = search(query=ttask["task"], filter_tags=["self-learning"], limit=5)

        if mode == "simulated":
            result = SIMULATED_PHASE4[xid]
        else:
            context = "\n".join(f"- {x['title']}: {x['content']}" for x in r["results"][:3])
            system = "You are a senior developer with access to past learnings."
            prompt = f"Past learnings:\n{context}\n\nTask: {ttask['task']}\n\nProvide your approach."
            response = call_llm_live(system, prompt)
            judge_prompt = f"Task: {ttask['task']}\nExpected transfer: {ttask['expected_transfer']}\nAgent response: {response}\n\nDoes the agent apply the relevant learning? Reply ONLY 'yes' or 'no'."
            judgment = call_llm_live("You are an evaluator.", judge_prompt).strip().lower()
            result = {"makes_mistake": not judgment.startswith("yes"), "response": response}

        phase4_mistakes[xid] = result["makes_mistake"]

    transfer_rate = 1 - (sum(phase4_mistakes.values()) / len(phase4_mistakes))

    print(f"  Transfer success rate: {transfer_rate:.0%} ({sum(1 for v in phase4_mistakes.values() if not v)}/{len(transfer_tasks)})")

    # ── Summary ───────────────────────────────────────────
    print(f"\n  {'─'*50}")
    print(f"  {'Metric':<30s}  {'Value':>8s}  {'Target':>8s}")
    print(f"  {'─'*30}  {'─'*8}  {'─'*8}")
    print(f"  {'Baseline mistake rate':<30s}  {baseline_rate:>7.0%}  {'—':>8s}")
    print(f"  {'Post-learning mistake rate':<30s}  {post_rate:>7.0%}  {'—':>8s}")
    print(f"  {'Mistake avoidance rate':<30s}  {avoidance_rate:>7.0%}  {'≥ 80%':>8s}  {'✅' if avoidance_rate >= 0.8 else '❌'}")
    print(f"  {'Prevention recall':<30s}  {prevention_recall:>7.0%}  {'≥ 80%':>8s}  {'✅' if prevention_recall >= 0.8 else '❌'}")
    print(f"  {'Transfer rate':<30s}  {transfer_rate:>7.0%}  {'≥ 50%':>8s}  {'✅' if transfer_rate >= 0.5 else '❌'}")

    return {
        "eval": 1, "mode": mode,
        "baseline_mistake_rate": round(baseline_rate, 3),
        "post_learning_mistake_rate": round(post_rate, 3),
        "mistake_avoidance_rate": round(avoidance_rate, 3),
        "prevention_recall": round(prevention_recall, 3),
        "transfer_rate": round(transfer_rate, 3),
        "tasks": len(tasks),
        "transfer_tasks": len(transfer_tasks),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EVALUATION 2: Knowledge Accumulation Over Sessions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HTTPX_QUESTIONS = [
    {"q": "What HTTP methods does the httpx Client support?", "answer": "GET, OPTIONS, HEAD, POST, PUT, PATCH, DELETE — each has a convenience method on both Client and AsyncClient."},
    {"q": "What is the default timeout for httpx requests?", "answer": "5 seconds for all phases (connect, read, write, pool) via DEFAULT_TIMEOUT_CONFIG."},
    {"q": "Does httpx follow redirects by default?", "answer": "No, follow_redirects defaults to False, unlike requests which follows by default."},
    {"q": "How does httpx handle authentication?", "answer": "Generator-based auth flow — Auth subclasses yield modified requests and receive responses back, supporting multi-step auth like Digest."},
    {"q": "What happens to the Authorization header during redirects?", "answer": "Stripped when redirecting to a different origin, unless it's a plain HTTP→HTTPS upgrade on the same host."},
    {"q": "How does httpx decompress responses?", "answer": "Decoder pipeline supporting identity, gzip, deflate, brotli (optional), and zstd (optional). MultiDecoder chains them in reverse order."},
    {"q": "What is the connection pool default configuration?", "answer": "max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0 seconds."},
    {"q": "How does httpx handle proxy configuration?", "answer": "Via proxy parameter on Client, or environment variables (HTTP_PROXY, HTTPS_PROXY, etc.) when trust_env=True. Supports HTTP, HTTPS, SOCKS5."},
    {"q": "What is the UseClientDefault sentinel for?", "answer": "Distinguishes between 'parameter not provided' (use client default) and 'parameter explicitly set to None' (disable the feature)."},
    {"q": "How does httpx handle cookies during redirects?", "answer": "Cookie header is stripped from redirect requests and rebuilt from the client cookie store, preventing cookie leakage to different domains."},
    {"q": "What's the difference between Client and AsyncClient?", "answer": "Share BaseClient config. Client uses sync transport/SyncByteStream/contextmanager. AsyncClient uses async transport/AsyncByteStream/asynccontextmanager."},
    {"q": "How does transport selection work for proxied requests?", "answer": "The _mounts dict maps URLPattern instances to transports, sorted for specificity. _transport_for_url iterates mounts, returning first match."},
    {"q": "What SSL/TLS options does httpx support?", "answer": "verify=True (certifi CAs), verify=False (disable), verify=SSLContext (custom). SSL_CERT_FILE/SSL_CERT_DIR env vars when trust_env=True."},
    {"q": "How does httpx track response elapsed time?", "answer": "BoundSyncStream/BoundAsyncStream wrap the transport stream. response.elapsed is set when the stream is closed, not when headers arrive."},
    {"q": "What exception is raised when the connection pool is full?", "answer": "PoolTimeout, a subclass of TimeoutException → TransportError → RequestError → HTTPError."},
]

# Simulated responses for each phase
SIMULATED_EVAL2 = {
    "no_memory": {
        0: {"score": 3, "correct": True},   # common knowledge
        1: {"score": 3, "correct": True},   # somewhat known
        2: {"score": 2, "correct": False},  # wrong assumption (thinks it follows redirects)
        3: {"score": 1, "correct": False},  # generic answer
        4: {"score": 0, "correct": False},  # doesn't know
        5: {"score": 2, "correct": True},   # partial
        6: {"score": 1, "correct": False},  # wrong defaults
        7: {"score": 2, "correct": True},   # partial
        8: {"score": 0, "correct": False},  # doesn't know sentinel
        9: {"score": 0, "correct": False},  # doesn't know behavior
        10: {"score": 2, "correct": True},  # basic difference
        11: {"score": 0, "correct": False}, # doesn't know mounts
        12: {"score": 2, "correct": True},  # partial SSL
        13: {"score": 0, "correct": False}, # doesn't know BoundStream
        14: {"score": 1, "correct": False}, # wrong exception name
    },
    "memory_only": {
        0: {"score": 5, "correct": True},
        1: {"score": 5, "correct": True},
        2: {"score": 5, "correct": True},
        3: {"score": 5, "correct": True},
        4: {"score": 5, "correct": True},
        5: {"score": 4, "correct": True},
        6: {"score": 5, "correct": True},
        7: {"score": 5, "correct": True},
        8: {"score": 4, "correct": True},
        9: {"score": 5, "correct": True},
        10: {"score": 5, "correct": True},
        11: {"score": 5, "correct": True},
        12: {"score": 5, "correct": True},
        13: {"score": 4, "correct": True},
        14: {"score": 5, "correct": True},
    },
}


def eval2_knowledge_accumulation(mode="simulated"):
    print("\n╔════════════════════════════════════════════════════════════╗")
    print("║  Evaluation 2: Knowledge Accumulation Over Sessions       ║")
    print(f"║  Mode: {mode:<51s}║")
    print("╚════════════════════════════════════════════════════════════╝")

    setup_project("eval2")

    questions = HTTPX_QUESTIONS

    # ── Phase 1: No memory baseline ───────────────────────
    print(f"\n  Phase 1: No memory — {len(questions)} questions about httpx")

    phase1_scores = []
    phase1_correct = 0
    for i, q in enumerate(questions):
        if mode == "simulated":
            result = SIMULATED_EVAL2["no_memory"][i]
        else:
            response = call_llm_live(
                "You are a Python developer. Answer based on your knowledge. Be specific.",
                f"Question about the httpx library: {q['q']}")
            judge_prompt = f"Question: {q['q']}\nGround truth: {q['answer']}\nAgent answer: {response}\n\nRate 0-5 (0=wrong, 5=perfect). Reply with ONLY the number."
            score = int(call_llm_live("Rate 0-5. Reply with only the number.", judge_prompt).strip()[0])
            result = {"score": score, "correct": score >= 3}

        phase1_scores.append(result["score"])
        if result["correct"]:
            phase1_correct += 1

    no_memory_avg = statistics.mean(phase1_scores)
    no_memory_accuracy = phase1_correct / len(questions)

    print(f"  No-memory score: {no_memory_avg:.1f}/5  Accuracy: {no_memory_accuracy:.0%}")

    # ── Phase 2: Store knowledge ──────────────────────────
    print(f"\n  Phase 2: Storing httpx knowledge (simulating sage learn)...")

    # Same knowledge entries used in Eval 3 (inlined to avoid import side-effects)
    httpx_knowledge = [
        {"title": "httpx Client and AsyncClient architecture", "content": "httpx client architecture uses BaseClient for shared config auth headers cookies timeout event_hooks base_url with Client sync and AsyncClient async. Both manage ClientState enum UNOPENED OPENED CLOSED. Transport selection uses URL pattern matching via _mounts dict.", "tags": ["client", "architecture", "async"]},
        {"title": "httpx request send pipeline and execution flow", "content": "When client send is called the pipeline is set timeout build auth flow send_handling_auth enters generator flow send_handling_redirects loops checking max_redirects firing event hooks send_single_request selects transport via URL pattern calls handle_request wraps response in BoundSyncStream for elapsed tracking extracts cookies.", "tags": ["request", "pipeline", "send"]},
        {"title": "httpx redirect handling and security behavior", "content": "Redirects handled with configurable follow_redirects default False. 303 changes to GET except HEAD. 302 also changes to GET. 301 POST becomes GET. Authorization headers stripped on different origin UNLESS HTTP to HTTPS upgrade same host. Cookie headers always stripped rebuilt from client store.", "tags": ["redirect", "security", "headers"]},
        {"title": "httpx authentication generator-based auth flow", "content": "Authentication uses generator-based flow pattern. Base Auth class defines auth_flow yielding Request receiving Response. Built-in BasicAuth base64 DigestAuth two-step WWW-Authenticate FunctionAuth callable NetRCAuth netrc file. Client resolves per-request then client-level then URL-embedded credentials.", "tags": ["auth", "authentication", "digest", "basic"]},
        {"title": "httpx transport layer and httpcore integration", "content": "Transport bridges httpx and httpcore. HTTPTransport wraps httpcore ConnectionPool for direct connections HTTPProxy SOCKSProxy for proxied. SSL via create_ssl_context with certifi CAs. Connection pooling max_connections max_keepalive_connections keepalive_expiry. Exception mapping converts httpcore to httpx exceptions.", "tags": ["transport", "httpcore", "ssl", "connection-pool"]},
        {"title": "httpx timeout configuration per-phase granularity", "content": "Timeouts via Timeout class with per-phase granularity connect read write pool. Default 5 seconds all phases via DEFAULT_TIMEOUT_CONFIG. UseClientDefault sentinel distinguishes omitted from explicit None. Propagates through request extensions timeout dict.", "tags": ["timeout", "config", "performance"]},
        {"title": "httpx connection pool limits and keepalive", "content": "Connection pooling via Limits class max_connections total concurrent default 100 max_keepalive_connections idle retained default 20 keepalive_expiry seconds before idle close default 5.0. PoolTimeout raised when pool full and timeout exceeded.", "tags": ["connection-pool", "limits", "keepalive"]},
        {"title": "httpx proxy configuration and SOCKS support", "content": "Proxies via proxy parameter or environment variables HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY when trust_env True. Proxy class wraps URL auth headers ssl_context. Supported protocols http https socks5 socks5h. SOCKS requires socksio package.", "tags": ["proxy", "socks", "network"]},
        {"title": "httpx exception hierarchy error handling", "content": "Exception hierarchy HTTPError splits into RequestError and HTTPStatusError via raise_for_status. TransportError branches TimeoutException ConnectTimeout ReadTimeout WriteTimeout PoolTimeout. NetworkError ConnectError ReadError WriteError CloseError. Stream exceptions StreamConsumed StreamClosed ResponseNotRead.", "tags": ["error", "exception", "handling"]},
        {"title": "httpx content encoding response decompression", "content": "Response decompression via decoder pipeline. Supported Content-Encoding identity gzip deflate br brotli optional zstd zstandard optional. MultiDecoder chains decoders in reverse order. Accept-Encoding header built from SUPPORTED_DECODERS keys.", "tags": ["encoding", "compression", "gzip", "brotli"]},
        {"title": "httpx URL parsing and manipulation", "content": "URL handling uses custom WHATWG-inspired parser. URL class provides scheme host port path query fragment. copy_with for immutable updates. URL merging for base_url relative URLs resolved against client base_url with trailing slash. QueryParams immutable multi-dict with merge.", "tags": ["url", "parsing", "query-params"]},
        {"title": "httpx event hooks request response interception", "content": "Event hooks provide middleware-like interception. Configured via event_hooks dict with request and response lists. Request hooks fire after build before send. Response hooks fire after receiving before returning. Called for each request including redirects. AsyncClient hooks can be async.", "tags": ["event-hooks", "middleware", "interceptor"]},
        {"title": "httpx cookie persistence and management", "content": "Cookie management uses Cookies class wrapping CookieJar with automatic persistence. Cookies extracted via extract_cookies in send_single_request. On redirects Cookie header explicitly stripped and rebuilt from client cookie store preventing domain leakage.", "tags": ["cookie", "session", "persistence"]},
        {"title": "httpx streaming responses BoundStream pattern", "content": "Streaming via client stream context manager sets stream True. BoundSyncStream BoundAsyncStream wrap transport stream tracking response elapsed timing set when closed not when headers arrive. StreamConsumed raised on re-read generator stream.", "tags": ["streaming", "response", "performance"]},
        {"title": "httpx sync vs async client differences", "content": "Client and AsyncClient share BaseClient for config. Client uses synchronous transport BaseTransport handle_request SyncByteStream contextmanager. AsyncClient uses async transport AsyncBaseTransport handle_async_request AsyncByteStream asynccontextmanager. HTTP methods GET POST PUT PATCH DELETE OPTIONS HEAD on both.", "tags": ["async", "sync", "client", "comparison"]},
    ]

    for k in httpx_knowledge:
        store(content=k["content"], title=k["title"], tags=k["tags"], scope="project")
    print(f"  Stored {len(httpx_knowledge)} memory entries")

    # ── Phase 3: Memory-assisted ──────────────────────────
    print(f"\n  Phase 3: Memory-assisted — same {len(questions)} questions")

    phase3_scores = []
    phase3_correct = 0
    search_hits = 0

    for i, q in enumerate(questions):
        # The actual sage-memory search call
        r = search(query=q["q"], limit=5)
        if len(r["results"]) > 0:
            search_hits += 1

        if mode == "simulated":
            result = SIMULATED_EVAL2["memory_only"][i]
        else:
            context = "\n".join(f"- {x['title']}: {x['content'][:200]}" for x in r["results"][:3])
            response = call_llm_live(
                "You are a Python developer with access to stored knowledge. Use it to answer precisely.",
                f"Stored knowledge:\n{context}\n\nQuestion: {q['q']}")
            judge_prompt = f"Question: {q['q']}\nGround truth: {q['answer']}\nAgent answer: {response}\n\nRate 0-5. Reply with ONLY the number."
            score = int(call_llm_live("Rate 0-5.", judge_prompt).strip()[0])
            result = {"score": score, "correct": score >= 3}

        phase3_scores.append(result["score"])
        if result["correct"]:
            phase3_correct += 1

    memory_avg = statistics.mean(phase3_scores)
    memory_accuracy = phase3_correct / len(questions)
    memory_lift = memory_avg - no_memory_avg
    accuracy_lift = memory_accuracy - no_memory_accuracy
    search_coverage = search_hits / len(questions)

    print(f"  Memory-assisted score: {memory_avg:.1f}/5  Accuracy: {memory_accuracy:.0%}")

    # ── Summary ───────────────────────────────────────────
    print(f"\n  {'─'*55}")
    print(f"  {'Metric':<30s}  {'Value':>8s}  {'Target':>8s}")
    print(f"  {'─'*30}  {'─'*8}  {'─'*8}")
    print(f"  {'No-memory score':<30s}  {no_memory_avg:>6.1f}/5  {'—':>8s}")
    print(f"  {'Memory-assisted score':<30s}  {memory_avg:>6.1f}/5  {'—':>8s}")
    print(f"  {'Score lift':<30s}  {memory_lift:>+6.1f}    {'≥ +1.5':>8s}  {'✅' if memory_lift >= 1.5 else '❌'}")
    print(f"  {'No-memory accuracy':<30s}  {no_memory_accuracy:>7.0%}  {'—':>8s}")
    print(f"  {'Memory accuracy':<30s}  {memory_accuracy:>7.0%}  {'—':>8s}")
    print(f"  {'Accuracy lift':<30s}  {accuracy_lift:>+6.0%}   {'≥ +30%':>8s}  {'✅' if accuracy_lift >= 0.3 else '❌'}")
    print(f"  {'Search coverage':<30s}  {search_coverage:>7.0%}  {'≥ 80%':>8s}  {'✅' if search_coverage >= 0.8 else '❌'}")

    return {
        "eval": 2, "mode": mode,
        "no_memory_score": round(no_memory_avg, 2),
        "memory_score": round(memory_avg, 2),
        "score_lift": round(memory_lift, 2),
        "no_memory_accuracy": round(no_memory_accuracy, 3),
        "memory_accuracy": round(memory_accuracy, 3),
        "accuracy_lift": round(accuracy_lift, 3),
        "search_coverage": round(search_coverage, 3),
        "questions": len(questions),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", type=str, default="all", help="1, 2, or all")
    parser.add_argument("--mode", type=str, default="simulated", help="simulated or live")
    args = parser.parse_args()

    results = []

    if args.eval in ("1", "all"):
        results.append(eval1_self_learning(args.mode))

    if args.eval in ("2", "all"):
        results.append(eval2_knowledge_accumulation(args.mode))

    # Summary
    print(f"\n{'='*60}")
    print(f"  EVALUATION SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r["eval"] == 1:
            print(f"\n  Eval 1 — Self-Learning Effectiveness ({r['mode']}):")
            print(f"    Baseline mistake rate:      {r['baseline_mistake_rate']:.0%}")
            print(f"    Post-learning mistake rate:  {r['post_learning_mistake_rate']:.0%}")
            print(f"    Mistake avoidance rate:      {r['mistake_avoidance_rate']:.0%}  (target ≥ 80%)")
            print(f"    Transfer rate:               {r['transfer_rate']:.0%}  (target ≥ 50%)")
        elif r["eval"] == 2:
            print(f"\n  Eval 2 — Knowledge Accumulation ({r['mode']}):")
            print(f"    No-memory score:      {r['no_memory_score']:.1f}/5")
            print(f"    Memory score:         {r['memory_score']:.1f}/5")
            print(f"    Score lift:           {r['score_lift']:+.1f}")
            print(f"    Accuracy lift:        {r['accuracy_lift']:+.0%}  (target ≥ +30%)")

    out_path = Path(__file__).parent / "results_eval12.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    close_all()
    shutil.rmtree(TMPDIR)


if __name__ == "__main__":
    main()
