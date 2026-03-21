#!/usr/bin/env python3
"""sage-memory Live Evaluation — Compact (10 tasks + 10 questions)."""
import json, os, sys, time, shutil, tempfile, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"
from sage_memory.db import override_project_root, close_all
from sage_memory.store import store
from sage_memory.search import search
TMPDIR = Path(tempfile.mkdtemp())
OUT_DIR = Path(__file__).resolve().parent

def setup_project(name):
    close_all()
    proj = TMPDIR / name
    if proj.exists(): shutil.rmtree(proj)
    proj.mkdir(parents=True); (proj / ".git").mkdir()
    override_project_root(proj)

def llm(system, prompt):
    import urllib.request
    body = json.dumps({"model": "claude-sonnet-4-20250514", "max_tokens": 300,
        "system": system, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                 "x-api-key": os.environ["ANTHROPIC_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["content"][0]["text"]

def judge_yn(prompt):
    r = llm("Reply ONLY yes or no.", prompt).strip().lower()
    return r.startswith("yes")

def judge_score(q, truth, answer):
    r = llm("Rate 0-5. Reply ONLY the number.",
        f"Q: {q}\nTruth: {truth}\nAnswer: {answer}\n0=wrong 3=mostly 5=perfect")
    try: return int(r.strip()[0])
    except: return 0

TASKS = [
    {"id":"t01","task":"Implement Stripe webhook handler for payment events","mistake":"Using parsed JSON body for signature verification instead of raw body","gotcha":"Stripe webhook requires raw body before JSON parsing","prevention":"Before implementing any webhook handler that verifies signatures, check whether the SDK requires the raw request body.","tags":["self-learning","gotcha","stripe","webhooks"]},
    {"id":"t02","task":"Create a Stripe charge for $10.00","mistake":"Passing 10.00 instead of 1000 — Stripe uses cents","gotcha":"Stripe API expects amounts in cents","prevention":"Before passing monetary amounts to payment APIs, check the unit.","tags":["self-learning","gotcha","stripe","payments"]},
    {"id":"t04","task":"Write a Dockerfile for a Node.js application","mistake":"COPY . /app before npm install — breaks layer cache","gotcha":"COPY before install invalidates Docker cache","prevention":"Copy dependency manifests and install before copying code.","tags":["self-learning","gotcha","docker","build-cache"]},
    {"id":"t07","task":"Write a database migration to add email column","mistake":"Writing raw SQL — project uses Prisma ORM","gotcha":"Project uses Prisma not raw SQL","prevention":"Check if project uses an ORM migration tool first.","tags":["self-learning","correction","prisma","database"]},
    {"id":"t09","task":"Set Redis cache TTL to 1 hour","mistake":"Setting 3600000 (ms) instead of 3600 (s)","gotcha":"Redis EXPIRE uses seconds not milliseconds","prevention":"Check TTL unit. EXPIRE uses seconds.","tags":["self-learning","correction","redis","cache"]},
    {"id":"t10","task":"Store refresh tokens in frontend","mistake":"Using localStorage — XSS vulnerable","gotcha":"Refresh tokens need httpOnly cookies","prevention":"Refresh tokens use httpOnly cookies. Access tokens use memory only.","tags":["self-learning","gotcha","auth","jwt"]},
    {"id":"t11","task":"Set up Express with JWT auth and CORS","mistake":"JWT before CORS — OPTIONS preflight fails","gotcha":"CORS must come before JWT auth","prevention":"Register CORS before auth middleware.","tags":["self-learning","gotcha","auth","cors"]},
    {"id":"t14","task":"Install dependencies in CI pipeline","mistake":"npm install when project uses pnpm","gotcha":"Project uses pnpm not npm","prevention":"Check for pnpm-lock.yaml before running install.","tags":["self-learning","correction","pnpm"]},
    {"id":"t17","task":"Add feature toggle for new billing","mistake":"Using env vars — project uses LaunchDarkly","gotcha":"Project uses LaunchDarkly not env vars","prevention":"Check for feature flag service before implementing toggles.","tags":["self-learning","convention","feature-flags"]},
    {"id":"t19","task":"Fetch all members of a 50K-member Redis set","mistake":"SMEMBERS blocks on large sets","gotcha":"Use SSCAN not SMEMBERS for large sets","prevention":"Check set size. Over 1000, use SSCAN.","tags":["self-learning","gotcha","redis","performance"]},
]

TRANSFER = [
    {"id":"x01","task":"Implement GitHub webhook handler","expected":"Check if SDK requires raw body for signature verification"},
    {"id":"x02","task":"Create a Square payment for €25.50","expected":"Check payment API unit — cents not euros"},
    {"id":"x03","task":"Write a Dockerfile for Python with requirements.txt","expected":"Copy requirements.txt and install before copying code"},
    {"id":"x04","task":"Set Memcached expiry to 30 minutes","expected":"Check TTL unit — seconds"},
    {"id":"x05","task":"Set up CI for project with bun.lockb","expected":"Check lockfile — bun.lockb means use bun"},
]

QA = [
    {"q":"What is the default timeout for httpx?","a":"5 seconds all phases (connect, read, write, pool)."},
    {"q":"Does httpx follow redirects by default?","a":"No, follow_redirects defaults to False."},
    {"q":"How does httpx handle authentication?","a":"Generator-based auth flow — yield requests, receive responses. Supports multi-step like Digest."},
    {"q":"What happens to Authorization header on cross-origin redirect?","a":"Stripped unless HTTP→HTTPS upgrade on same host."},
    {"q":"What is httpx connection pool default config?","a":"max_connections=100, max_keepalive=20, expiry=5.0s."},
    {"q":"How does httpx handle proxy config?","a":"proxy param or HTTP_PROXY env vars. HTTP, HTTPS, SOCKS5."},
    {"q":"What is UseClientDefault?","a":"Sentinel distinguishing 'not provided' from 'explicitly None'."},
    {"q":"How are cookies handled during redirects?","a":"Cookie header stripped, rebuilt from client cookie store."},
    {"q":"How does transport selection work?","a":"_mounts maps URLPattern to transports. First match wins."},
    {"q":"How is response elapsed time tracked?","a":"BoundStream wraps transport. elapsed set when stream closed."},
]

KNOWLEDGE = [
    {"t":"httpx timeout per-phase","c":"Timeout class: connect read write pool. Default 5s all. UseClientDefault distinguishes omitted from None.","g":["timeout"]},
    {"t":"httpx redirect security","c":"follow_redirects default False. Authorization stripped on different origin unless HTTP→HTTPS same host. Cookies stripped rebuilt.","g":["redirect"]},
    {"t":"httpx auth generator flow","c":"Auth generator yields Request receives Response. BasicAuth DigestAuth FunctionAuth NetRCAuth. Per-request > client-level > URL-embedded.","g":["auth"]},
    {"t":"httpx connection pool","c":"Limits: max_connections=100 max_keepalive=20 expiry=5.0s. PoolTimeout when full.","g":["pool"]},
    {"t":"httpx proxy SOCKS","c":"proxy param or HTTP_PROXY HTTPS_PROXY env vars. http https socks5 socks5h supported.","g":["proxy"]},
    {"t":"httpx cookie redirects","c":"Cookies via CookieJar. On redirect Cookie header stripped rebuilt from client store preventing leakage.","g":["cookie"]},
    {"t":"httpx transport mounts","c":"_mounts maps URLPattern to transport sorted by specificity. _transport_for_url returns first match.","g":["transport"]},
    {"t":"httpx BoundStream elapsed","c":"BoundSyncStream wraps transport. response.elapsed set when stream closed not on headers.","g":["streaming"]},
]

def run_eval1():
    print("╔══════════════════════════════════════════════════════╗")
    print("║  Eval 1: Self-Learning Effectiveness (LIVE)         ║")
    print("╚══════════════════════════════════════════════════════╝")
    setup_project("e1"); t0 = time.time()

    print(f"\n  Phase 1: Baseline ({len(TASKS)} tasks, no memory)")
    p1 = {}
    for i, t in enumerate(TASKS):
        print(f"    [{i+1:2d}] {t['id']}...", end=" ", flush=True)
        resp = llm("Senior developer. Be specific.", f"Task: {t['task']}\nYour approach (2-3 sentences):")
        m = judge_yn(f"Task:{t['task']}\nMistake:{t['mistake']}\nResponse:{resp}\nDid agent make mistake?")
        p1[t["id"]] = {"mistake": m, "r": resp[:120]}; print("❌ MISTAKE" if m else "✅ correct")
    base = sum(v["mistake"] for v in p1.values()) / len(p1)
    print(f"  → Baseline mistake rate: {base:.0%}")

    print(f"\n  Phase 2: Storing {sum(v['mistake'] for v in p1.values())} prevention rules")
    for t in TASKS:
        if p1[t["id"]]["mistake"]:
            store(content=f"Gotcha: {t['gotcha']}\nMistake: {t['mistake']}\nPrevention: {t['prevention']}",
                  title=f"[LRN:gotcha] {t['gotcha'][:70]}", tags=t["tags"], scope="project")

    print(f"\n  Phase 3: Re-test with memory")
    p3 = {}; hits = 0
    for i, t in enumerate(TASKS):
        print(f"    [{i+1:2d}] {t['id']}...", end=" ", flush=True)
        r = search(query=t["task"], filter_tags=["self-learning"], limit=5)
        if r["results"]: hits += 1
        ctx = "\n".join(f"- {x['title']}: {x['content'][:100]}" for x in r["results"][:3])
        mem = f"\nPast learnings:\n{ctx}" if ctx else ""
        resp = llm("Senior dev with past learnings. Apply prevention rules.",
                    f"Task: {t['task']}{mem}\nApproach with precautions:")
        m = judge_yn(f"Task:{t['task']}\nMistake:{t['mistake']}\nResponse:{resp}\nDid agent make mistake?")
        p3[t["id"]] = {"mistake": m, "r": resp[:120]}; print("❌" if m else "✅ avoided")
    post = sum(v["mistake"] for v in p3.values()) / len(p3)
    avoid = 1 - (post / max(base, 0.001)); prev_rec = hits / len(TASKS)
    print(f"  → Post: {post:.0%}  Avoidance: {avoid:.0%}  Recall: {prev_rec:.0%}")

    print(f"\n  Phase 4: Transfer ({len(TRANSFER)} new tasks)")
    p4 = {}
    for i, tt in enumerate(TRANSFER):
        print(f"    [{i+1}] {tt['id']}...", end=" ", flush=True)
        r = search(query=tt["task"], filter_tags=["self-learning"], limit=5)
        ctx = "\n".join(f"- {x['title']}: {x['content'][:100]}" for x in r["results"][:3])
        mem = f"\nLearnings:\n{ctx}" if ctx else ""
        resp = llm("Dev with past learnings.", f"Task: {tt['task']}{mem}\nApproach:")
        ok = judge_yn(f"Task:{tt['task']}\nExpected:{tt['expected']}\nResponse:{resp}\nDid agent apply learning?")
        p4[tt["id"]] = {"ok": ok, "r": resp[:120]}; print("✅" if ok else "❌")
    xfer = sum(v["ok"] for v in p4.values()) / len(p4)
    el = time.time() - t0
    print(f"  → Transfer: {xfer:.0%}  ({el:.0f}s)")

    return {"eval":1,"base":round(base,3),"post":round(post,3),"avoidance":round(avoid,3),
            "recall":round(prev_rec,3),"transfer":round(xfer,3),"elapsed":round(el),
            "p1":p1,"p3":p3,"p4":p4}

def run_eval2():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Eval 2: Knowledge Accumulation (LIVE)              ║")
    print("╚══════════════════════════════════════════════════════╝")
    setup_project("e2"); t0 = time.time()

    print(f"\n  Phase 1: No memory ({len(QA)} questions)")
    p1 = []
    for i, qa in enumerate(QA):
        print(f"    [Q{i+1:2d}]", end=" ", flush=True)
        resp = llm("Python developer. Answer about httpx specifically.", f"Question: {qa['q']}")
        s = judge_score(qa["q"], qa["a"], resp)
        p1.append({"q":qa["q"],"score":s,"r":resp[:120]}); print(f"score={s}")

    print(f"\n  Phase 2: Storing {len(KNOWLEDGE)} memories")
    for k in KNOWLEDGE:
        store(content=k["c"], title=k["t"], tags=k["g"], scope="project")

    print(f"\n  Phase 3: Memory-assisted")
    p3 = []; hits = 0
    for i, qa in enumerate(QA):
        print(f"    [Q{i+1:2d}]", end=" ", flush=True)
        r = search(query=qa["q"], limit=5)
        if r["results"]: hits += 1
        ctx = "\n".join(f"- {x['title']}: {x['content'][:120]}" for x in r["results"][:3])
        resp = llm("Developer with httpx knowledge. Use context.", f"Context:\n{ctx}\n\nQ: {qa['q']}")
        s = judge_score(qa["q"], qa["a"], resp)
        p3.append({"q":qa["q"],"score":s,"r":resp[:120],"mems":len(r["results"])}); print(f"score={s} mems={len(r['results'])}")

    s1 = statistics.mean([r["score"] for r in p1]); s3 = statistics.mean([r["score"] for r in p3])
    a1 = sum(1 for r in p1 if r["score"]>=3)/len(p1); a3 = sum(1 for r in p3 if r["score"]>=3)/len(p3)
    el = time.time() - t0
    print(f"\n  No-mem: {s1:.1f}/5 ({a1:.0%})  Memory: {s3:.1f}/5 ({a3:.0%})  Lift: {s3-s1:+.1f}  ({el:.0f}s)")

    return {"eval":2,"no_mem":round(s1,2),"mem":round(s3,2),"lift":round(s3-s1,2),
            "no_acc":round(a1,3),"mem_acc":round(a3,3),"acc_lift":round(a3-a1,3),
            "coverage":round(hits/len(QA),3),"elapsed":round(el),"p1":p1,"p3":p3}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--eval", default="all"); a = p.parse_args()
    results = []
    if a.eval in ("1","all"): results.append(run_eval1())
    if a.eval in ("2","all"): results.append(run_eval2())
    with open(OUT_DIR / "results_live.json", "w") as f: json.dump(results, f, indent=2)
    print(f"\nResults saved to {OUT_DIR / 'results_live.json'}")
    close_all(); shutil.rmtree(TMPDIR)
