#!/usr/bin/env python3
"""在隔离测试数据库记录已确认事务，并与恢复后的数据库核对。"""

import argparse
import csv
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4


FIELDS = ("run_id", "expected_count", "seq", "started_utc", "finished_utc", "outcome")
REPOSITORY = Path(__file__).resolve().parent.parent


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def check_connection(expected_db):
    for key in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSFILE", "PGSSLROOTCERT"):
        if not os.environ.get(key):
            raise ValueError(f"缺少连接配置 {key}")
    if os.environ.get("PGPASSWORD"):
        raise ValueError("请使用受限 PGPASSFILE，不要设置 PGPASSWORD")
    if os.environ.get("PGSERVICE"):
        raise ValueError("请显式配置连接参数，不使用可能覆盖 TLS 设置的 PGSERVICE")
    if os.environ["PGHOST"].startswith(("/", "@")) or "," in os.environ["PGHOST"]:
        raise ValueError("PGHOST 必须是单个 TLS 服务名，不能使用本地套接字或主机列表")
    if os.environ.get("PGSSLMODE") != "verify-full":
        raise ValueError("PGSSLMODE 必须为 verify-full")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", expected_db) or os.environ["PGDATABASE"] != expected_db:
        raise ValueError("PGDATABASE 与确认的测试数据库不一致")
    passfile = Path(os.environ["PGPASSFILE"])
    if not passfile.is_file() or stat.S_IMODE(passfile.stat().st_mode) & 0o077:
        raise ValueError("PGPASSFILE 不存在或权限不是仅所有者可读写")
    if not Path(os.environ["PGSSLROOTCERT"]).is_file():
        raise ValueError("PGSSLROOTCERT 不存在")


def psql(sql, timeout):
    return subprocess.run(
        ["psql", "-X", "-q", "-t", "-A", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "PGGSSENCMODE": "disable"},
    )


def preflight(expected_db, expected_cluster, timeout, require_synchronous):
    sql = (
        "SELECT current_database(), current_setting('cluster_name'), pg_is_in_recovery(), "
        "to_regclass('public.dba_ha_probe') IS NOT NULL, current_setting('fsync'), "
        "current_setting('full_page_writes'), current_setting('synchronous_standby_names') <> ''"
    )
    result = psql(sql, timeout)
    fields = result.stdout.strip().split("|")
    if result.returncode != 0 or len(fields) != 7 or fields[0] != expected_db or fields[1] != expected_cluster or fields[3] != "t":
        raise ValueError("数据库/集群身份或测试表检查失败")
    if fields[2] != "f":
        raise ValueError("目标不是当前主库")
    if require_synchronous and fields[4:] != ["on", "on", "t"]:
        raise ValueError("测试主库未启用所需持久化参数或同步副本配置")


def external_path(name):
    source = Path(name).expanduser().absolute()
    path = source.parent.resolve() / source.name
    if path == REPOSITORY or REPOSITORY in path.parents:
        raise ValueError("证据文件必须位于仓库外")
    return path


def write(args):
    if not args.test_only:
        raise ValueError("写入仅允许隔离测试环境，必须指定 --test-only")
    if not 1 <= args.count <= 5000 or not 0 <= args.interval <= 60 or not 1 <= args.timeout <= 60:
        raise ValueError("count、interval 或 timeout 超出允许范围")
    check_connection(args.expected_db)
    output = external_path(args.evidence)
    preflight(args.expected_db, args.expected_cluster, args.timeout, require_synchronous=True)
    run_id = str(uuid4())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(output, flags, 0o600)
    ack = unknown = 0
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(FIELDS)
        stream.flush()
        os.fsync(stream.fileno())
        for seq in range(1, args.count + 1):
            started = utc_now()
            sql = (
                "BEGIN; SET LOCAL synchronous_commit = 'on'; "
                f"INSERT INTO public.dba_ha_probe(run_id, seq) VALUES ('{run_id}', {seq}); COMMIT;"
            )
            try:
                result = psql(sql, args.timeout)
                outcome = "ack" if result.returncode == 0 else "unknown"
            except subprocess.TimeoutExpired:
                outcome = "unknown"
            finished = utc_now()
            writer.writerow((run_id, args.count, seq, started, finished, outcome))
            stream.flush()
            os.fsync(stream.fileno())
            if outcome == "ack":
                ack += 1
            else:
                unknown += 1
            if seq < args.count:
                time.sleep(args.interval)
    print(f"run_id={run_id} ack={ack} unknown={unknown} evidence={output}")
    if not ack:
        print("没有已确认事务，不能证明 RPO", file=sys.stderr)
        return 1
    return 0


def verify(args):
    if not 1 <= args.timeout <= 60:
        raise ValueError("timeout 超出允许范围")
    check_connection(args.expected_db)
    source = external_path(args.evidence)
    if source.stat().st_size > 1024 * 1024:
        raise ValueError("证据文件过大")
    with source.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError("证据文件表头无效")
        rows = list(reader)
    if not rows or len(rows) > 5000:
        raise ValueError("证据文件为空或超过允许条数")
    run_id = str(UUID(rows[0]["run_id"]))
    expected_count = int(rows[0]["expected_count"])
    if not 1 <= expected_count <= 5000:
        raise ValueError("证据文件的预期条数无效")
    ack = set()
    unknown = set()
    seen = set()
    for row in rows:
        seq = int(row["seq"])
        if str(UUID(row["run_id"])) != run_id or int(row["expected_count"]) != expected_count or seq < 1 or seq in seen or row["outcome"] not in ("ack", "unknown"):
            raise ValueError("证据文件包含非法或重复记录")
        seen.add(seq)
        (ack if row["outcome"] == "ack" else unknown).add(seq)
    if seen != set(range(1, expected_count + 1)):
        raise ValueError("证据不完整：实际序号与预期条数不一致")
    preflight(args.expected_db, args.expected_cluster, args.timeout, require_synchronous=False)
    result = psql(f"SELECT seq FROM public.dba_ha_probe WHERE run_id = '{run_id}' ORDER BY seq", args.timeout)
    if result.returncode != 0:
        raise ValueError("查询恢复目标失败")
    actual = {int(line) for line in result.stdout.splitlines() if line.strip()}
    missing = sorted(ack - actual)
    extra = sorted(actual - seen)
    print(f"run_id={run_id} ack={len(ack)} missing_ack={len(missing)} unknown_present={len(unknown & actual)} unknown_absent={len(unknown - actual)} unexpected={len(extra)}")
    if missing:
        print("缺失已确认事务序号: " + ",".join(map(str, missing)), file=sys.stderr)
        return 1
    if not ack:
        print("没有已确认事务，不能证明 RPO", file=sys.stderr)
        return 1
    if extra:
        print("数据库包含证据中不存在的序号: " + ",".join(map(str, extra)), file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("write", "verify"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--expected-db", required=True)
        sub.add_argument("--expected-cluster", required=True)
        sub.add_argument("--evidence", required=True)
        sub.add_argument("--timeout", type=int, default=10)
        if name == "write":
            sub.add_argument("--test-only", action="store_true")
            sub.add_argument("--count", type=int, default=120)
            sub.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    try:
        if args.action == "write":
            return write(args)
        return verify(args)
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"检查未完成: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
