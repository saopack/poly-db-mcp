"""并发测试脚本 - Vastbase 2.2.15 @ http://172.16.105.102:8000"""
import time
import json
import statistics
import concurrent.futures
import urllib.request
import urllib.error
import sys

BASE_URL = "http://172.16.105.102:8000"
API_KEY = "mcp-qZAb0hg1vpSjbE84ANsWC7WJqX2AO3-DurKUFcSqPCE"
DB_TYPE = "vastbase"
VERSION = "2.2.15"

def execute_sql(query, explain=False):
    """Single request to execute_sql."""
    url = f"{BASE_URL}/api/execute_sql"
    payload = json.dumps({
        "db_type": DB_TYPE,
        "version": VERSION,
        "query": query,
        "explain": explain,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
            elapsed = round((time.time() - start) * 1000)
            return elapsed, body.get("status", "error"), body
    except urllib.error.HTTPError as e:
        elapsed = round((time.time() - start) * 1000)
        body = json.loads(e.read())
        return elapsed, f"HTTP_{e.code}", body
    except Exception as e:
        elapsed = round((time.time() - start) * 1000)
        return elapsed, "error", {"message": str(e)}


def run_concurrent(concurrency, query, label, rounds=1, delay_between=0):
    """Run N concurrent requests and return stats."""
    results = []
    for rnd in range(rounds):
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(execute_sql, query) for _ in range(concurrency)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())
        if rnd < rounds - 1 and delay_between:
            time.sleep(delay_between)

    latencies = [r[0] for r in results]
    statuses = [r[1] for r in results]
    success = sum(1 for s in statuses if s == "success")
    errors = sum(1 for s in statuses if s != "success")

    sorted_lat = sorted(latencies)
    p50 = sorted_lat[len(sorted_lat) // 2]
    p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
    p99 = sorted_lat[int(len(sorted_lat) * 0.99)]

    return {
        "label": label,
        "concurrency": concurrency,
        "total": len(results),
        "success": success,
        "errors": errors,
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "avg_ms": round(statistics.mean(latencies)),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "throughput_rps": round(success / (max(latencies) / 1000), 1) if success > 1 else 0,
        "status_breakdown": {s: statuses.count(s) for s in set(statuses)},
    }


def print_report(results):
    print(f"\n{'='*80}")
    print(f"并发测试报告 — Vastbase {VERSION} @ {BASE_URL}")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}")
    print(f"{'场景':<28} {'并发':>5} {'总数':>5} {'成功':>5} {'失败':>5} "
          f"{'Min(ms)':>8} {'Avg(ms)':>8} {'P50(ms)':>8} {'P95(ms)':>8} {'P99(ms)':>8}")
    print(f"{'-'*100}")
    for r in results:
        print(f"{r['label']:<28} {r['concurrency']:>5} {r['total']:>5} {r['success']:>5} {r['errors']:>5} "
              f"{r['min_ms']:>8} {r['avg_ms']:>8} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['p99_ms']:>8}")

    print(f"\n{'='*80}")
    print("错误详情")
    print(f"{'='*80}")
    for r in results:
        if r["errors"] > 0:
            print(f"\n[{r['label']}] errors={r['errors']}, breakdown={r['status_breakdown']}")


if __name__ == "__main__":
    all_results = []

    # Warmup
    print("预热...", end=" ", flush=True)
    execute_sql("SELECT 1")
    execute_sql("SELECT 1")
    print("done\n")

    SELECT_QUERY = "SELECT generate_series(1, 100)"
    DDL_QUERY = "CREATE TABLE IF NOT EXISTS test_concurrency (id int, name text)"
    COMPLEX_QUERY = """
    WITH t AS (SELECT generate_series(1, 50) AS n)
    SELECT t1.n, t2.n FROM t t1 CROSS JOIN t t2
    """

    # === Test Suite ===
    test_plan = [
        # (concurrency, query, label, rounds)
        (1,  SELECT_QUERY, "SELECT (baseline)", 1),
        (3,  SELECT_QUERY, "SELECT x3", 1),
        (5,  SELECT_QUERY, "SELECT x5", 1),
        (8,  SELECT_QUERY, "SELECT x8", 1),
        (10, SELECT_QUERY, "SELECT x10 (=max)", 1),
        (12, SELECT_QUERY, "SELECT x12 (>max)", 1),
        (15, SELECT_QUERY, "SELECT x15 (>max)", 1),
        (3,  COMPLEX_QUERY, "CROSS JOIN x3", 1),
        (5,  COMPLEX_QUERY, "CROSS JOIN x5", 1),
        (3,  DDL_QUERY,    "DDL CREATE TABLE x3", 1),
        (5,  DDL_QUERY,    "DDL CREATE TABLE x5", 1),
    ]

    for concurrency, query, label, rounds in test_plan:
        print(f"测试 {label} (concurrency={concurrency})...", end=" ", flush=True)
        result = run_concurrent(concurrency, query, label, rounds=rounds)
        all_results.append(result)
        print(f"avg={result['avg_ms']}ms, success={result['success']}/{result['total']}")
        time.sleep(1)  # brief pause between test groups

    # Sustained load test
    print(f"\n持续负载测试 (5x rounds of 5 concurrent)...", end=" ", flush=True)
    result = run_concurrent(5, SELECT_QUERY, "Sustained 5x5 rnds", rounds=5, delay_between=2)
    all_results.append(result)
    print(f"avg={result['avg_ms']}ms, success={result['success']}/{result['total']}")

    print_report(all_results)
