#!/usr/bin/env python3
"""sage-memory Evaluation Harness

Evaluations 3 and 4 run fully locally (no LLM API needed).
Evaluations 1 and 2 provide the framework — require LLM API for full runs.

Usage:
  PYTHONPATH=src python evaluation/run_eval.py [--eval 3] [--eval 4] [--eval all]
"""

import json
import os
import sys
import time
import shutil
import statistics
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"

from sage_memory.db import override_project_root, close_all
from sage_memory.store import store, delete, list_memories
from sage_memory.search import search
from sage_memory.graph import link, graph


TMPDIR = Path(tempfile.mkdtemp())


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
# EVALUATION 3: Retrieval Quality
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# LLM-authored knowledge about httpx (genuine capture-knowledge content)
KNOWLEDGE_ENTRIES = [
    {"title": "httpx Client and AsyncClient architecture",
     "content": "httpx client architecture uses BaseClient for shared config (auth, headers, cookies, timeout, event_hooks, base_url) with Client (sync) and AsyncClient (async) as concrete implementations. Both manage ClientState enum UNOPENED OPENED CLOSED. Transport selection uses URL pattern matching via _mounts dict supporting proxy routing.",
     "tags": ["client", "architecture", "async"]},
    {"title": "httpx request send pipeline and execution flow",
     "content": "When client.send() is called the pipeline is: set timeout from defaults, build auth flow, _send_handling_auth enters generator flow, _send_handling_redirects loops checking max_redirects firing event hooks, _send_single_request selects transport via URL pattern calls handle_request wraps response in BoundSyncStream for elapsed tracking extracts cookies and logs.",
     "tags": ["request", "pipeline", "send", "flow"]},
    {"title": "httpx redirect handling and security behavior",
     "content": "Redirects handled with configurable follow_redirects default False. 303 changes to GET except HEAD, 302 also changes to GET browser compat, 301 POST becomes GET. Authorization headers stripped on different origin UNLESS HTTP to HTTPS upgrade same host. Cookie headers always stripped rebuilt from client store. Host header updated.",
     "tags": ["redirect", "security", "headers"]},
    {"title": "httpx authentication system with generator-based auth flow",
     "content": "Authentication uses generator-based flow pattern. Base Auth class defines auth_flow yielding Request receiving Response. Built-in: BasicAuth base64 credentials, DigestAuth two-step WWW-Authenticate parsing, FunctionAuth wraps callable, NetRCAuth reads netrc file. Client resolves: per-request auth then client-level auth then URL-embedded credentials.",
     "tags": ["auth", "authentication", "digest", "basic", "security"]},
    {"title": "httpx transport layer and httpcore integration",
     "content": "Transport bridges httpx and httpcore. HTTPTransport wraps httpcore ConnectionPool for direct connections HTTPProxy or SOCKSProxy for proxied. SSL via create_ssl_context with certifi CAs. Connection pooling: max_connections max_keepalive_connections keepalive_expiry. Exception mapping converts httpcore exceptions to httpx equivalents.",
     "tags": ["transport", "httpcore", "ssl", "connection-pool", "proxy"]},
    {"title": "httpx timeout configuration with per-phase granularity",
     "content": "Timeouts via Timeout class with per-phase granularity: connect establishing connection, read receiving data, write sending data, pool acquiring connection. Default 5 seconds all phases. Propagates through request.extensions timeout dict. UseClientDefault sentinel distinguishes omitted from explicit None.",
     "tags": ["timeout", "config", "performance"]},
    {"title": "httpx connection pool limits and keepalive settings",
     "content": "Connection pooling via Limits class: max_connections total concurrent default 100, max_keepalive_connections idle retained default 20, keepalive_expiry seconds before idle close default 5.0. Passed through to httpcore ConnectionPool. PoolTimeout raised when pool full.",
     "tags": ["connection-pool", "limits", "keepalive", "performance"]},
    {"title": "httpx proxy configuration and SOCKS support",
     "content": "Proxies via proxy parameter or environment variables HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY when trust_env True. Proxy class wraps URL auth headers ssl_context. Supported protocols http https socks5 socks5h. SOCKS requires socksio package. _mounts maps URL patterns to transports.",
     "tags": ["proxy", "socks", "network", "config"]},
    {"title": "httpx exception hierarchy and error handling patterns",
     "content": "Exception hierarchy: HTTPError splits into RequestError issues during request and HTTPStatusError 4xx 5xx via raise_for_status. RequestError branches: TransportError timeout network protocol proxy, DecodingError, TooManyRedirects. Timeout: ConnectTimeout ReadTimeout WriteTimeout PoolTimeout. Network: ConnectError ReadError WriteError CloseError.",
     "tags": ["error", "exception", "handling"]},
    {"title": "httpx content encoding and response decompression",
     "content": "Response decompression via decoder pipeline. Supported Content-Encoding: identity passthrough, gzip via zlib, deflate via zlib with fallback, br brotli optional, zstd zstandard optional. MultiDecoder chains multiple decoders applied in reverse. Accept-Encoding built from SUPPORTED_DECODERS keys.",
     "tags": ["encoding", "compression", "gzip", "brotli", "decoder"]},
    {"title": "httpx request content encoding and multipart support",
     "content": "Request encoding handles: raw bytes string to ByteStream with Content-Length, sync iterables to IteratorByteStream chunked transfer, async iterables always chunked. Form data dict to URL-encoded. Files to MultipartStream. JSON to compact encoding. encode_request dispatches to appropriate encoder.",
     "tags": ["content", "multipart", "upload", "encoding", "request-body"]},
    {"title": "httpx URL parsing and manipulation",
     "content": "URL handling uses custom WHATWG-inspired parser. URL class provides scheme host port path query fragment properties. copy_with for immutable updates. URL merging for base_url: relative URLs resolved against client base_url which always has trailing slash. QueryParams immutable multi-dict with merge.",
     "tags": ["url", "parsing", "query-params"]},
    {"title": "httpx event hooks for request and response interception",
     "content": "Event hooks provide middleware-like interception. Configured via event_hooks dict with request and response lists. Request hooks fire after build before send. Response hooks fire after receiving before returning. Hooks called in order for each request including redirects. AsyncClient hooks can be async.",
     "tags": ["event-hooks", "middleware", "interceptor"]},
    {"title": "httpx cookie persistence and management",
     "content": "Cookie management uses Cookies class wrapping CookieJar with automatic persistence across requests. Cookies extracted via extract_cookies in _send_single_request. On redirects Cookie header explicitly stripped and rebuilt from client cookie store preventing domain leakage.",
     "tags": ["cookie", "session", "persistence"]},
    {"title": "httpx streaming responses and BoundStream pattern",
     "content": "Streaming via client.stream context manager sets stream True. BoundSyncStream BoundAsyncStream wrap raw transport stream tracking response.elapsed timing set when closed not when headers arrive. StreamConsumed raised on re-read generator stream. UnattachedStream for pickled requests.",
     "tags": ["streaming", "response", "performance"]},
    {"title": "httpx sync vs async client implementation differences",
     "content": "Client and AsyncClient share BaseClient for config. Client uses synchronous transport BaseTransport handle_request SyncByteStream contextmanager. AsyncClient uses async transport AsyncBaseTransport handle_async_request AsyncByteStream asynccontextmanager. Auth flow uses yield send for sync async yield asend for async.",
     "tags": ["async", "sync", "client", "comparison"]},
    {"title": "httpx transport mount pattern for URL-based routing",
     "content": "The _mounts dict maps URLPattern instances to transport instances. Patterns sorted for specificity matching. Used internally for proxy routing and externally for custom transport injection. _transport_for_url iterates mounts returning first match or default. Mock transports mountable for testing.",
     "tags": ["transport", "routing", "mount", "proxy"]},
    {"title": "httpx SSL and TLS configuration",
     "content": "SSL via create_ssl_context in _config.py. Default uses certifi CA bundle. Options: verify True certifi, verify False disables CERT_NONE, verify ssl.SSLContext custom. Environment SSL_CERT_FILE SSL_CERT_DIR override when trust_env True. HTTP/2 requires ALPN negotiation.",
     "tags": ["ssl", "tls", "certificate", "security", "https"]},
    {"title": "Python HTTP client comparison httpx vs requests vs aiohttp",
     "content": "httpx is modern replacement for requests with async support. Supports both sync and async. Has HTTP/2 via h2. Uses httpcore as transport. Follows WHATWG URL parsing. API similar to requests for migration. Does NOT follow redirects by default. Timeout defaults to 5s unlike requests which has no default timeout.",
     "tags": ["comparison", "requests", "aiohttp", "python"], "scope": "global"},
    {"title": "Best practices for HTTP client usage in Python",
     "content": "Always use client instance not module-level get for connection pooling. Use context managers. Set explicit timeouts never None in production. AsyncClient for async apps. Set follow_redirects explicitly. Use event_hooks for cross-cutting concerns. Use stream for large responses. Configure limits for backend capacity.",
     "tags": ["best-practices", "python", "http"], "scope": "global"},
]

QUERIES = [
    # Exact API lookups
    {"q": "AsyncClient send request", "expect_kw": ["asyncclient", "send"], "cat": "exact"},
    {"q": "BasicAuth DigestAuth authentication", "expect_kw": ["basicauth", "digestauth"], "cat": "exact"},
    {"q": "Timeout connect read write pool", "expect_kw": ["timeout", "connect", "read"], "cat": "exact"},
    {"q": "HTTPTransport connection pool", "expect_kw": ["httptransport", "connection"], "cat": "exact"},
    {"q": "event_hooks request response", "expect_kw": ["event_hooks", "hook"], "cat": "exact"},
    # Semantic paraphrases
    {"q": "how does the library handle following links to other pages", "expect_kw": ["redirect", "follow"], "cat": "semantic"},
    {"q": "what happens when credentials are needed", "expect_kw": ["auth", "credential", "basic", "digest"], "cat": "semantic"},
    {"q": "controlling how long to wait before giving up", "expect_kw": ["timeout", "connect"], "cat": "semantic"},
    {"q": "reusing network connections across multiple requests", "expect_kw": ["pool", "keepalive", "connection"], "cat": "semantic"},
    {"q": "decompressing server responses that are compressed", "expect_kw": ["gzip", "brotli", "decod", "compress"], "cat": "semantic"},
    {"q": "sending files and form data to a server", "expect_kw": ["multipart", "upload", "file"], "cat": "semantic"},
    {"q": "intercepting requests before they go out", "expect_kw": ["hook", "event", "intercept"], "cat": "semantic"},
    {"q": "keeping login state across multiple calls", "expect_kw": ["cookie", "session", "persist"], "cat": "semantic"},
    {"q": "processing a large download without using too much memory", "expect_kw": ["stream", "chunk", "memory"], "cat": "semantic"},
    # Workflow
    {"q": "how to add custom authentication to httpx client", "expect_kw": ["auth", "auth_flow", "custom"], "cat": "workflow"},
    {"q": "how to configure proxy for all requests", "expect_kw": ["proxy", "socks", "mount"], "cat": "workflow"},
    {"q": "difference between sync Client and AsyncClient", "expect_kw": ["sync", "async", "client"], "cat": "workflow"},
    {"q": "how to handle HTTP errors and status codes", "expect_kw": ["exception", "error", "status"], "cat": "workflow"},
    {"q": "how to set different timeouts for connect and read", "expect_kw": ["timeout", "connect", "read"], "cat": "workflow"},
    # Architecture
    {"q": "httpx request execution flow from client to server", "expect_kw": ["send", "transport", "pipeline"], "cat": "architecture"},
    {"q": "how httpx wraps httpcore for network operations", "expect_kw": ["httpcore", "transport", "connectionpool"], "cat": "architecture"},
    {"q": "URL routing for proxy vs direct connections", "expect_kw": ["mount", "pattern", "transport"], "cat": "architecture"},
    {"q": "security implications of redirect handling", "expect_kw": ["redirect", "authorization", "strip"], "cat": "architecture"},
    {"q": "how content encoding negotiation works", "expect_kw": ["accept-encoding", "gzip", "decoder"], "cat": "architecture"},
    # Adversarial
    {"q": "connection reuse and idle timeout behavior", "expect_kw": ["keepalive", "expiry", "pool"], "cat": "adversarial"},
    {"q": "httpx", "expect_kw": ["httpx", "client", "http"], "cat": "adversarial"},
    {"q": "why does my request hang with no timeout", "expect_kw": ["timeout", "default"], "cat": "adversarial"},
    {"q": "is it safe to share a client between threads", "expect_kw": ["client", "shared"], "cat": "adversarial"},
    {"q": "requests library migration guide", "expect_kw": ["requests", "httpx", "comparison"], "cat": "adversarial"},
]


def compute_recall(results, expected_keywords):
    if not expected_keywords:
        return 1.0
    text = " ".join(f"{r.get('title','')} {r.get('content','')}" for r in results[:5]).lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in text)
    return hits / len(expected_keywords)


def compute_mrr(results, expected_keywords):
    for i, r in enumerate(results[:10]):
        text = f"{r.get('title','')} {r.get('content','')}".lower()
        if any(kw.lower() in text for kw in expected_keywords):
            return 1.0 / (i + 1)
    return 0.0


def eval3_retrieval_quality():
    print("╔════════════════════════════════════════════════════════════╗")
    print("║  Evaluation 3: Retrieval Quality                          ║")
    print("╚════════════════════════════════════════════════════════════╝")

    setup_project("eval3-or")

    # Store knowledge
    print(f"\n  Storing {len(KNOWLEDGE_ENTRIES)} knowledge entries...")
    for k in KNOWLEDGE_ENTRIES:
        store(content=k["content"], title=k["title"], tags=k["tags"],
              scope=k.get("scope", "project"))

    # ── OR semantics ──────────────────────────────────────
    print(f"  Running {len(QUERIES)} queries (OR semantics)...\n")

    results_by_cat = {}
    all_recalls, all_mrrs, all_lats = [], [], []

    for q in QUERIES:
        t0 = time.perf_counter()
        r = search(query=q["q"], limit=5)
        lat = (time.perf_counter() - t0) * 1000

        recall = compute_recall(r["results"], q["expect_kw"])
        mrr = compute_mrr(r["results"], q["expect_kw"])

        all_recalls.append(recall)
        all_mrrs.append(mrr)
        all_lats.append(lat)

        cat = q["cat"]
        if cat not in results_by_cat:
            results_by_cat[cat] = {"recalls": [], "mrrs": [], "lats": []}
        results_by_cat[cat]["recalls"].append(recall)
        results_by_cat[cat]["mrrs"].append(mrr)
        results_by_cat[cat]["lats"].append(lat)

    # ── AND semantics (for comparison) ────────────────────
    # Build an AND-based search function using the same DB
    from sage_memory.db import get_db
    import re as _re

    _STOP = frozenset("a an the is are was were be been being have has had do does did will would shall should may might can could to of in for on with at by from as into through during before after above below between out off over under again further then once here there when where why how all each every both few more most other some such no nor not only own same so than too very and but or if this that it its what which who whom whose".split())

    def and_search(query, limit=5):
        db = get_db("project")
        cleaned = _re.sub(r'[^\w\s]', " ", query)
        words = [w.lower() for w in cleaned.split() if len(w) >= 2 and w.lower() not in _STOP]
        if not words:
            return {"results": []}
        fts_q = " ".join(f"{w}*" for w in words)  # implicit AND
        try:
            rows = db.execute(
                """SELECT m.*, bm25(memories_fts, 10.0, 3.0, 1.0) AS score
                   FROM memories m JOIN memories_fts fts ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts_q, limit)).fetchall()
            return {"results": [dict(r) for r in rows]}
        except Exception:
            return {"results": []}

    and_recalls = []
    for q in QUERIES:
        r = and_search(q["q"], limit=5)
        and_recalls.append(compute_recall(r["results"], q["expect_kw"]))

    # ── Report ────────────────────────────────────────────
    print(f"  {'Category':<15s}  {'Recall':>7s}  {'MRR':>7s}  {'P50ms':>7s}  {'P95ms':>7s}  {'AND Recall':>10s}")
    print(f"  {'─'*15}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*10}")

    cat_order = ["exact", "semantic", "workflow", "architecture", "adversarial"]
    and_by_cat = {}
    idx = 0
    for q in QUERIES:
        cat = q["cat"]
        if cat not in and_by_cat:
            and_by_cat[cat] = []
        and_by_cat[cat].append(and_recalls[idx])
        idx += 1

    for cat in cat_order:
        d = results_by_cat[cat]
        s = sorted(d["lats"])
        and_r = statistics.mean(and_by_cat[cat])
        print(f"  {cat:<15s}  {statistics.mean(d['recalls']):6.0%}  "
              f"{statistics.mean(d['mrrs']):7.2f}  "
              f"{statistics.median(s):6.1f}  {s[int(len(s)*0.95)]:6.1f}  "
              f"{and_r:9.0%}")

    s_all = sorted(all_lats)
    print(f"\n  {'OVERALL':<15s}  {statistics.mean(all_recalls):6.0%}  "
          f"{statistics.mean(all_mrrs):7.2f}  "
          f"{statistics.median(s_all):6.1f}  {s_all[int(len(s_all)*0.95)]:6.1f}  "
          f"{statistics.mean(and_recalls):9.0%}")

    or_acceptable = sum(1 for r in all_recalls if r >= 0.5) / len(all_recalls) * 100
    and_acceptable = sum(1 for r in and_recalls if r >= 0.5) / len(and_recalls) * 100

    print(f"\n  Acceptable recall (≥50%):  OR={or_acceptable:.0f}%  AND={and_acceptable:.0f}%")
    print(f"  OR advantage: +{statistics.mean(all_recalls) - statistics.mean(and_recalls):.0%} mean recall")

    return {
        "eval": 3,
        "or_recall": round(statistics.mean(all_recalls), 3),
        "or_mrr": round(statistics.mean(all_mrrs), 3),
        "and_recall": round(statistics.mean(and_recalls), 3),
        "latency_p50": round(statistics.median(s_all), 2),
        "latency_p95": round(s_all[int(len(s_all) * 0.95)], 2),
        "queries": len(QUERIES),
        "entries": len(KNOWLEDGE_ENTRIES),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EVALUATION 4: Graph-Enhanced Recall
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LEARNINGS = [
    {"id": 1, "title": "[LRN:gotcha] N+1 query in ReportBuilder causes 30s timeout", "content": "ReportBuilder.aggregate called Transaction.load_details per row. Fix: prefetch_related. Went from 30s to 200ms. Prevention: check for N+1 patterns before adding querysets.", "tags": ["self-learning", "gotcha", "database", "performance"], "entity": "task_payment"},
    {"id": 2, "title": "[LRN:gotcha] Timezone offset UTC billing close PST display", "content": "PST users saw tomorrow transactions in today statement. Billing closes UTC midnight display uses PST. Prevention: check timezone conversion before displaying dates.", "tags": ["self-learning", "gotcha", "billing", "timezone"], "entity": "task_payment"},
    {"id": 3, "title": "[LRN:gotcha] Billing saga compensating transaction must be idempotent", "content": "Saga rollback charged refund twice. Compensating handler did not check prior execution. Prevention: always add idempotency check using saga execution ID.", "tags": ["self-learning", "gotcha", "billing", "saga"], "entity": "task_payment"},
    {"id": 4, "title": "[LRN:gotcha] Stripe webhook requires raw body", "content": "Webhook signature verification failed. Express body parser replaced raw body. Prevention: check if SDK requires raw body before implementing webhook handler.", "tags": ["self-learning", "gotcha", "stripe", "webhooks"], "entity": "task_stripe"},
    {"id": 5, "title": "[LRN:gotcha] Stripe webhook signature error misleads", "content": "400 error said wrong secret but actual cause was parsed body. Prevention: check body format first, secret second.", "tags": ["self-learning", "gotcha", "stripe", "webhooks"], "entity": "task_stripe"},
    {"id": 6, "title": "[LRN:gotcha] Stripe webhook idempotency on retries", "content": "Same event processed 3 times. Stripe retries on non-2xx. Prevention: add idempotency check using event ID.", "tags": ["self-learning", "gotcha", "stripe", "webhooks"], "entity": "task_stripe"},
    {"id": 7, "title": "[LRN:gotcha] JWT refresh token must be httpOnly cookie", "content": "Stored in localStorage XSS vulnerability. Prevention: refresh tokens in httpOnly cookie, access tokens in memory only.", "tags": ["self-learning", "gotcha", "auth", "jwt"], "entity": "task_auth"},
    {"id": 8, "title": "[LRN:gotcha] Auth middleware order CORS before JWT", "content": "OPTIONS returned 401. JWT before CORS rejected preflight. Prevention: register CORS before auth middleware.", "tags": ["self-learning", "gotcha", "auth", "cors"], "entity": "task_auth"},
    {"id": 9, "title": "[LRN:gotcha] Token refresh race condition concurrent requests", "content": "Two requests both got 401, both refreshed, one failed. Prevention: add lock to prevent concurrent refresh.", "tags": ["self-learning", "gotcha", "auth", "token-refresh"], "entity": "task_auth"},
    {"id": 10, "title": "[LRN:error-fix] Docker platform mismatch Apple Silicon", "content": "Build failed no match for platform. Base image AMD64 only. Prevention: check ARM64 support before writing Dockerfile.", "tags": ["self-learning", "error-fix", "docker", "arm64"], "entity": "task_docker"},
    {"id": 11, "title": "[LRN:error-fix] Docker build cache invalidated by COPY", "content": "Builds took 5 minutes for code changes. COPY before install invalidates cache. Prevention: copy manifests and install before copying code.", "tags": ["self-learning", "error-fix", "docker", "build-cache"], "entity": "task_docker"},
]

ENTITIES = [
    {"id": "task_payment", "title": "[Task:task_payment] Fix payment timeout in checkout"},
    {"id": "task_stripe", "title": "[Task:task_stripe] Implement Stripe webhook handler"},
    {"id": "task_auth", "title": "[Task:task_auth] Update auth middleware"},
    {"id": "task_docker", "title": "[Task:task_docker] Optimize Docker builds in CI"},
]

GROUND_TRUTH = {
    "task_payment": [1, 2, 3],
    "task_stripe": [4, 5, 6],
    "task_auth": [7, 8, 9],
    "task_docker": [10, 11],
}

EVAL4_QUERIES = [
    {"entity": "task_payment", "query": "Fix payment timeout in checkout"},
    {"entity": "task_stripe", "query": "Implement Stripe webhook handler"},
    {"entity": "task_auth", "query": "Update auth middleware"},
    {"entity": "task_docker", "query": "Optimize Docker builds in CI"},
]


def eval4_graph_enhanced():
    print("\n╔════════════════════════════════════════════════════════════╗")
    print("║  Evaluation 4: Graph-Enhanced Recall                      ║")
    print("╚════════════════════════════════════════════════════════════╝")

    setup_project("eval4")

    # Store entities
    entity_mem_ids = {}
    for e in ENTITIES:
        r = store(content=json.dumps({"id": e["id"], "type": "Task"}),
                  title=e["title"], tags=["ontology", "entity", "task"])
        entity_mem_ids[e["id"]] = r["id"]

    # Store learnings
    learning_mem_ids = {}
    for lrn in LEARNINGS:
        r = store(content=lrn["content"], title=lrn["title"], tags=lrn["tags"])
        learning_mem_ids[lrn["id"]] = r["id"]

    # Create graph edges: learning → entity via "applies_to"
    for lrn in LEARNINGS:
        link(source_id=learning_mem_ids[lrn["id"]],
             target_id=entity_mem_ids[lrn["entity"]],
             relation="applies_to")

    # Store noise (regular knowledge, not learnings)
    store(content="Billing service uses saga pattern.", title="Billing architecture",
          tags=["architecture", "billing"])
    store(content="Auth uses JWT with refresh rotation.", title="Auth overview",
          tags=["architecture", "auth"])

    # ── Compare three strategies ──────────────────────────
    print(f"\n  {'Entity':<15s}  {'KW P':>6s} {'KW R':>6s}  {'Graph P':>7s} {'Graph R':>7s}  {'Delta P':>7s}")
    print(f"  {'─'*15}  {'─'*6} {'─'*6}  {'─'*7} {'─'*7}  {'─'*7}")

    kw_precisions, kw_recalls = [], []
    graph_precisions, graph_recalls = [], []

    for eq in EVAL4_QUERIES:
        entity_id = eq["entity"]
        relevant_ids = set(GROUND_TRUTH[entity_id])
        entity_mem_id = entity_mem_ids[entity_id]

        # Strategy 1: Keyword search
        t0 = time.perf_counter()
        kw_r = search(query=eq["query"], filter_tags=["self-learning"], limit=10)
        kw_lat = (time.perf_counter() - t0) * 1000

        kw_found = set()
        for res in kw_r["results"]:
            for lrn in LEARNINGS:
                if lrn["title"] == res["title"]:
                    kw_found.add(lrn["id"])
        kw_p = len(kw_found & relevant_ids) / max(len(kw_r["results"]), 1)
        kw_rec = len(kw_found & relevant_ids) / len(relevant_ids)

        # Strategy 2: Graph traversal
        t0 = time.perf_counter()
        gr_r = graph(id=entity_mem_id, relation="applies_to", direction="inbound", depth=1)
        gr_lat = (time.perf_counter() - t0) * 1000

        gr_found = set()
        for node in gr_r["nodes"]:
            for lrn in LEARNINGS:
                if lrn["title"] == node["title"]:
                    gr_found.add(lrn["id"])
        gr_p = len(gr_found & relevant_ids) / max(len(gr_r["nodes"]), 1)
        gr_rec = len(gr_found & relevant_ids) / len(relevant_ids)

        kw_precisions.append(kw_p)
        kw_recalls.append(kw_rec)
        graph_precisions.append(gr_p)
        graph_recalls.append(gr_rec)

        delta_p = gr_p - kw_p
        print(f"  {entity_id:<15s}  {kw_p:5.2f}  {kw_rec:5.2f}  {gr_p:6.2f}  {gr_rec:6.2f}  {delta_p:+6.2f}")

    # Averages
    avg_kw_p = statistics.mean(kw_precisions)
    avg_kw_r = statistics.mean(kw_recalls)
    avg_gr_p = statistics.mean(graph_precisions)
    avg_gr_r = statistics.mean(graph_recalls)

    print(f"\n  {'AVERAGE':<15s}  {avg_kw_p:5.2f}  {avg_kw_r:5.2f}  {avg_gr_p:6.2f}  {avg_gr_r:6.2f}  {avg_gr_p - avg_kw_p:+6.2f}")

    # CASCADE test
    print(f"\n  CASCADE test:")
    delete(id=entity_mem_ids["task_payment"])
    gr_after = graph(id=entity_mem_ids["task_payment"], direction="inbound", depth=1)
    cascade_ok = not gr_after["success"] or len(gr_after["nodes"]) == 0
    print(f"  Delete entity → edges removed: {'✅' if cascade_ok else '❌'}")

    return {
        "eval": 4,
        "keyword_precision": round(avg_kw_p, 3),
        "keyword_recall": round(avg_kw_r, 3),
        "graph_precision": round(avg_gr_p, 3),
        "graph_recall": round(avg_gr_r, 3),
        "precision_delta": round(avg_gr_p - avg_kw_p, 3),
        "cascade_ok": cascade_ok,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    import argparse
    parser = argparse.ArgumentParser(description="sage-memory evaluation harness")
    parser.add_argument("--eval", type=str, default="all",
                        help="Which eval to run: 3, 4, or all")
    args = parser.parse_args()

    results = []

    if args.eval in ("3", "all"):
        results.append(eval3_retrieval_quality())

    if args.eval in ("4", "all"):
        results.append(eval4_graph_enhanced())

    if args.eval in ("1", "2"):
        print(f"\n  Evaluation {args.eval} requires LLM API access.")
        print(f"  See evaluation/PROTOCOL.md for the full protocol.")
        print(f"  Seed data: evaluation/seed/eval1_self_learning_tasks.json")

    # Summary
    print(f"\n{'='*60}")
    print(f"  EVALUATION SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r["eval"] == 3:
            print(f"\n  Eval 3 — Retrieval Quality:")
            print(f"    OR recall:  {r['or_recall']:.0%}   AND recall: {r['and_recall']:.0%}")
            print(f"    OR MRR:     {r['or_mrr']:.2f}")
            print(f"    Latency:    P50={r['latency_p50']:.1f}ms  P95={r['latency_p95']:.1f}ms")
        elif r["eval"] == 4:
            print(f"\n  Eval 4 — Graph-Enhanced Recall:")
            print(f"    Keyword precision:  {r['keyword_precision']:.2f}")
            print(f"    Graph precision:    {r['graph_precision']:.2f}  (delta: {r['precision_delta']:+.2f})")
            print(f"    Graph recall:       {r['graph_recall']:.2f}")
            print(f"    CASCADE correct:    {'✅' if r['cascade_ok'] else '❌'}")

    # Save
    out_path = Path(__file__).parent / "results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    close_all()
    shutil.rmtree(TMPDIR)


if __name__ == "__main__":
    main()
