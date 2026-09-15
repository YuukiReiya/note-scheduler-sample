#!/usr/bin/env python3
# Copyright 2026 Yuuki Reiya
# Licensed under the PolyForm Internal Use License 1.0.0 (see LICENSE.md).
# Redistribution is not permitted.

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

IS_WINDOWS = os.name == "nt"

STATE_HOME = os.environ.get(
    "CLAUDE_SCHEDULER_HOME",
    os.path.join(os.path.expanduser("~"), ".claude", "scheduler"),
)

LOG_RETENTION_DAYS = int(os.environ.get("CLAUDE_SCHEDULER_LOG_RETENTION_DAYS", "14"))
CATCHUP_MINUTES = int(os.environ.get("CLAUDE_SCHEDULER_CATCHUP_MINUTES", "360"))

RAN_STATUSES = ("success", "failure", "timeout")
NOT_RAN_STATUSES = ("skipped", "paused", "disabled", "locked", "error")



FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
FIELD_NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]


class CronError(ValueError):
    pass


def _parse_field(spec, lo, hi, name):
    values = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError("%s: 空の要素があります (%r)" % (name, spec))

        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError("%s: ステップ値が不正です (%r)" % (name, spec))
            step = int(step_s)

        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            start_s, _, end_s = part.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                raise CronError("%s: 範囲指定が不正です (%r)" % (name, spec))
            start, end = int(start_s), int(end_s)
            if start > end:
                raise CronError("%s: 範囲の開始が終了より大きいです (%r)" % (name, spec))
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError("%s: 解釈できない値です (%r)" % (name, spec))

        if start < lo or end > hi:
            raise CronError(
                "%s: 値が範囲外です（%d-%d のはずが %r）" % (name, lo, hi, spec)
            )
        values.update(range(start, end + 1, step))

    if not values:
        raise CronError("%s: マッチする値がありません (%r)" % (name, spec))
    return values


class CronExpr(object):

    def __init__(self, expr):
        self.raw = expr.strip()
        fields = self.raw.split()
        if len(fields) != 5:
            raise CronError(
                "cron式は5フィールド（分 時 日 月 曜日）で書いてください: %r" % expr
            )
        self.minute, self.hour, self.dom, self.month, self.dow = [
            _parse_field(f, lo, hi, name)
            for f, (lo, hi), name in zip(fields, FIELD_RANGES, FIELD_NAMES)
        ]
        if 7 in self.dow:
            self.dow.add(0)
        self._dom_restricted = fields[2] != "*"
        self._dow_restricted = fields[4] != "*"

    def matches(self, dt):
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False

        dom_ok = dt.day in self.dom
        dow_ok = ((dt.weekday() + 1) % 7) in self.dow

        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True

    def __repr__(self):
        return "CronExpr(%r)" % self.raw


def missed_fire_time(crons, since, now, window_minutes):
    if not crons or since is None or window_minutes <= 0:
        return None
    start = since + timedelta(minutes=1)
    earliest = now - timedelta(minutes=window_minutes)
    if start < earliest:
        start = earliest
    cursor = now - timedelta(minutes=1)
    while cursor >= start:
        if any(cron.matches(cursor) for cron in crons):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


def next_fire_time(crons, after, horizon_days=40):
    if not crons:
        return None
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + timedelta(days=horizon_days)
    while cursor < limit:
        for cron in crons:
            if cron.matches(cursor):
                return cursor
        cursor += timedelta(minutes=1)
    return None




class JobError(ValueError):
    pass


class Job(object):
    def __init__(self, job_id, scope, path, data):
        self.id = job_id
        self.scope = scope
        self.path = path
        self.raw = data

        if not isinstance(data, dict):
            raise JobError("定義のトップレベルはJSONオブジェクトである必要があります")

        self.description = data.get("description", "")
        self.enabled = bool(data.get("enabled", True))
        self.timeout = data.get("timeout")
        if self.timeout is not None and (
            not isinstance(self.timeout, (int, float)) or self.timeout <= 0
        ):
            raise JobError("timeout は正の秒数で指定してください")
        self.overlap = data.get("overlap", "skip")
        if self.overlap != "skip":
            raise JobError("overlap は現状 'skip' のみ対応しています")

        trigger = data.get("trigger") or {}
        if not isinstance(trigger, dict):
            raise JobError("trigger はJSONオブジェクトで書いてください")
        cron_spec = trigger.get("cron")
        if cron_spec is None:
            cron_list = []
        elif isinstance(cron_spec, str):
            cron_list = [cron_spec]
        elif isinstance(cron_spec, list):
            cron_list = cron_spec
        else:
            raise JobError("trigger.cron は文字列か、文字列の配列で書いてください")
        self.crons = [CronExpr(c) for c in cron_list]

        self.after = []
        after_spec = data.get("after") or []
        if isinstance(after_spec, (str, dict)):
            after_spec = [after_spec]
        if not isinstance(after_spec, list):
            raise JobError("after は配列で書いてください")
        for entry in after_spec:
            if isinstance(entry, str):
                entry = {"job": entry}
            if not isinstance(entry, dict) or "job" not in entry:
                raise JobError("after の要素は文字列か {\"job\": ..., \"on\": ...} です")
            on = entry.get("on", "success")
            if on not in ("success", "failure", "always"):
                raise JobError("after[].on は success / failure / always のいずれかです")
            self.after.append({"job": entry["job"], "on": on})

        if self.crons and self.after:
            raise JobError(
                "trigger.cron と after の同時指定はできません"
                "（発火の起点は時刻か依存かのどちらか一方に限定しています）"
            )
        if not self.crons and not self.after:
            raise JobError("trigger.cron か after のどちらかが必要です")

        run = data.get("run")
        if not isinstance(run, dict):
            raise JobError("run はJSONオブジェクトで書いてください")
        self.run_type = run.get("type")
        if self.run_type not in ("command", "claude"):
            raise JobError("run.type は 'command' か 'claude' です")
        self.run = run

        if self.run_type == "command":
            if not run.get("command"):
                raise JobError("run.type=command には run.command が必要です")
        else:
            if not run.get("prompt"):
                raise JobError("run.type=claude には run.prompt が必要です")
            tools = run.get("allowedTools")
            if tools is not None and not isinstance(tools, list):
                raise JobError("run.allowedTools は文字列の配列で書いてください")
            for key in run:
                if key.replace("-", "").lower().startswith("dangerously"):
                    raise JobError(
                        "権限チェックのバイパスは定義から指定できません（%s）" % key
                    )

    @property
    def safe_id(self):
        return self.id.replace("/", "__")

    def next_fire(self, after=None):
        return next_fire_time(self.crons, after or datetime.now())


CONF_PROJECTS_RE = re.compile(r"^\s*PROJECTS=\((.*?)\)", re.S | re.M)


def parse_conf_projects(config_dir):
    path = os.path.join(config_dir, "sync-links.conf")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            match = CONF_PROJECTS_RE.search(fh.read())
    except OSError:
        return {}
    if not match:
        return {}

    mapping = {}
    for line in match.group(1).splitlines():
        line = line.split("#", 1)[0].strip().strip('"').strip("'")
        if not line:
            continue
        name, sep, target = line.partition(":")
        if sep and name and target:
            mapping[name.strip()] = os.path.expanduser(target.strip())
    return mapping


def _job_files(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, n)
        for n in os.listdir(directory)
        if n.endswith(".json") and not n.startswith(".")
    )


def load_jobs(config_dir, projects):
    jobs = {}
    errors = []
    scopes = [("global", os.path.join(config_dir, "global", "schedules"))]
    for name in sorted(projects):
        scopes.append((name, os.path.join(config_dir, "projects", name, "schedules")))

    for scope, directory in scopes:
        for path in _job_files(directory):
            job_id = "%s/%s" % (scope, os.path.splitext(os.path.basename(path))[0])
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                jobs[job_id] = Job(job_id, scope, path, data)
            except Exception as exc:
                errors.append((job_id, "%s: %s" % (path, exc)))
    return jobs, errors


def validate_jobs(jobs):
    problems = []
    for job in jobs.values():
        for dep in job.after:
            if dep["job"] not in jobs:
                problems.append(
                    ("error", job.id, "依存先のジョブが存在しません: %s" % dep["job"])
                )
        if job.run_type == "claude" and "allowedTools" not in job.run:
            problems.append(
                (
                    "warn",
                    job.id,
                    "run.allowedTools が未指定です。非対話実行では権限プロンプトに"
                    "答えられないため、ツールを使うプロンプトは失敗します",
                )
            )

    for cycle in _find_cycles(jobs):
        problems.append(("error", cycle[0], "依存が循環しています: %s" % " -> ".join(cycle)))
    return problems


def _find_cycles(jobs):
    cycles = []
    state = {}

    def visit(job_id, stack):
        if state.get(job_id) == 1:
            start = stack.index(job_id)
            cycles.append(stack[start:] + [job_id])
            return
        if state.get(job_id) == 2 or job_id not in jobs:
            return
        state[job_id] = 1
        for dep in jobs[job_id].after:
            visit(dep["job"], stack + [dep["job"]])
        state[job_id] = 2

    for job_id in sorted(jobs):
        visit(job_id, [job_id])
    return cycles


def children_of(jobs):
    children = {}
    for job in jobs.values():
        for dep in job.after:
            children.setdefault(dep["job"], []).append(job.id)
    return children




def state_path(*parts):
    return os.path.join(STATE_HOME, *parts)


def ensure_state_dirs():
    for sub in ("lock", "logs", "runs"):
        try:
            os.makedirs(state_path(sub))
        except OSError:
            pass


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path))
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)



PAUSE_ALL = "__all__"


def load_pauses():
    data = _read_json(state_path("paused.json"), {})
    if not isinstance(data, dict):
        return {}
    now = datetime.now()
    alive = {}
    changed = False
    for key, entry in data.items():
        until = (entry or {}).get("until")
        if until:
            try:
                if datetime.fromisoformat(until) <= now:
                    changed = True
                    continue
            except ValueError:
                pass
        alive[key] = entry or {}
    if changed:
        _write_json(state_path("paused.json"), alive)
    return alive


def pause_reason(job_id, pauses):
    for key, label in ((PAUSE_ALL, "全体を一時停止中"), (job_id, "一時停止中")):
        if key in pauses:
            until = pauses[key].get("until")
            return "%s（%s）" % (label, "%s まで" % until if until else "無期限")
    return None


def parse_until(text):
    if not text:
        return None
    match = re.match(r"^(\d+)([mhd])$", text.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return datetime.now() + delta
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(
            "Error: --until は '30m' / '2h' / '3d' か ISO形式"
            "（2026-08-28T09:00）で指定してください: %r" % text
        )




def _pid_alive(pid):
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid],
                capture_output=True, text=True,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_lock(safe_id):
    path = state_path("lock", safe_id)
    for _ in range(2):
        try:
            os.makedirs(path)
        except OSError:
            pid = _read_json(os.path.join(path, "info.json"), {}).get("pid")
            if pid and _pid_alive(int(pid)):
                return False
            shutil.rmtree(path, ignore_errors=True)
            continue
        _write_json(
            os.path.join(path, "info.json"),
            {"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds")},
        )
        return True
    return False


def release_lock(safe_id):
    shutil.rmtree(state_path("lock", safe_id), ignore_errors=True)


def lock_info_path(safe_id):
    return state_path("lock", safe_id, "info.json")


def running_info(safe_id):
    info = _read_json(lock_info_path(safe_id), {})
    pid = info.get("pid")
    if pid and _pid_alive(int(pid)):
        return info
    return None


def running_pid(safe_id):
    info = running_info(safe_id)
    return int(info["pid"]) if info else None




def log_dir(safe_id):
    return state_path("logs", safe_id)


def cleanup_logs():
    root = state_path("logs")
    if not os.path.isdir(root):
        return
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for name in os.listdir(root):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            path = os.path.join(directory, entry)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass



DEFAULT_TIMEOUT = 3600
MAX_PARALLEL = int(os.environ.get("CLAUDE_SCHEDULER_MAX_PARALLEL", "8"))


class Context(object):

    def __init__(self, config_dir, projects, jobs):
        self.config_dir = config_dir
        self.projects = projects
        self.jobs = jobs
        self.children = children_of(jobs)


def _which(name, fallbacks=()):
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.path.exists(candidate):
            return candidate
    return None


def resolve_cwd(job, ctx):
    spec = job.run.get("cwd")
    if spec:
        if spec in ctx.projects:
            return ctx.projects[spec]
        if os.path.isabs(spec):
            return spec
        return os.path.join(ctx.config_dir, spec)
    if job.scope in ctx.projects:
        return ctx.projects[job.scope]
    return ctx.config_dir


def build_command(job, ctx):
    if job.run_type == "command":
        bash = _which("bash", (
            "/bin/bash", "/usr/bin/bash",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ))
        if not bash:
            raise RuntimeError(
                "bash が見つかりません（run.type=command は bash -c で実行します）"
            )
        return [bash, "-c", job.run["command"]]

    claude = _which(
        "claude", (os.path.join(os.path.expanduser("~"), ".local", "bin", "claude"),)
    )
    if not claude:
        raise RuntimeError(
            "claude コマンドが見つかりません（run.type=claude には Claude Code CLI が必要です）"
        )
    cmd = [claude, "-p", job.run["prompt"], "--output-format", "json"]
    tools = job.run.get("allowedTools")
    if tools:
        cmd.append("--allowedTools")
        cmd.extend(tools)
    if job.run.get("model"):
        cmd.extend(["--model", job.run["model"]])
    for extra in job.run.get("addDirs", []) or []:
        cmd.extend(["--add-dir", extra])
    return cmd


def _terminate(proc):
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if IS_WINDOWS:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()


def run_job(job, ctx, forced=False):
    pauses = load_pauses()
    reason = pause_reason(job.id, pauses)
    if reason and not forced:
        return "paused"
    if not job.enabled and not forced:
        return "disabled"
    if not acquire_lock(job.safe_id):
        return "locked"

    started = datetime.now()
    directory = log_dir(job.safe_id)
    try:
        os.makedirs(directory)
    except OSError:
        pass
    log_file = os.path.join(directory, started.strftime("%Y%m%d-%H%M%S") + ".log")
    timeout = job.timeout or DEFAULT_TIMEOUT
    status = "failure"

    try:
        with open(log_file, "w", encoding="utf-8") as log:
            try:
                cmd = build_command(job, ctx)
                cwd = resolve_cwd(job, ctx)
            except RuntimeError as exc:
                log.write("[scheduler] 起動できません: %s\n" % exc)
                return "error"

            log.write("[scheduler] job     : %s\n" % job.id)
            log.write("[scheduler] started : %s\n" % started.isoformat(timespec="seconds"))
            log.write("[scheduler] cwd     : %s\n" % cwd)
            log.write("[scheduler] command : %s\n" % " ".join(shlex.quote(c) for c in cmd))
            if reason:
                log.write("[scheduler] 警告: %s だが手動実行のため走らせます\n" % reason)
            log.write("[scheduler] %s\n" % ("-" * 60))
            log.flush()

            popen_kwargs = {
                "cwd": cwd,
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "env": dict(os.environ, CLAUDE_SCHEDULER_JOB_ID=job.id),
            }
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **popen_kwargs)
            _write_json(
                lock_info_path(job.safe_id),
                {
                    "pid": os.getpid(),
                    "child_pid": proc.pid,
                    "started": started.isoformat(timespec="seconds"),
                },
            )
            try:
                code = proc.wait(timeout=timeout)
                status = "success" if code == 0 else "failure"
            except subprocess.TimeoutExpired:
                _terminate(proc)
                code = None
                status = "timeout"

            elapsed = (datetime.now() - started).total_seconds()
            log.write("[scheduler] %s\n" % ("-" * 60))
            log.write(
                "[scheduler] status  : %s (exit=%s, %.1f秒)\n"
                % (status, "timeout" if code is None else code, elapsed)
            )
    finally:
        release_lock(job.safe_id)

    _write_json(
        state_path("logs", job.safe_id, "last.json"),
        {
            "job": job.id,
            "status": status,
            "started": started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "log": log_file,
        },
    )
    return status


def last_result(job):
    return _read_json(state_path("logs", job.safe_id, "last.json"), None)




def subgraph_from(root_id, ctx):
    seen = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in seen or current not in ctx.jobs:
            continue
        seen.add(current)
        stack.extend(ctx.children.get(current, []))
    return seen


def _decide(job, statuses, members):
    parents = [dep for dep in job.after if dep["job"] in members]
    for dep in parents:
        status = statuses.get(dep["job"])
        if status not in RAN_STATUSES:
            return False
        if dep["on"] == "success" and status != "success":
            return False
        if dep["on"] == "failure" and status == "success":
            return False
    return True


def run_group(root_id, ctx, with_deps=True, forced=False):
    ensure_state_dirs()
    members = subgraph_from(root_id, ctx) if with_deps else {root_id}
    run_id = "%s-%s" % (
        ctx.jobs[root_id].safe_id,
        datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    statuses = {}
    pending = set(members)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, max(1, len(members)))) as pool:
        futures = {}
        while pending or futures:
            ready = [
                job_id
                for job_id in sorted(pending)
                if all(
                    dep["job"] in statuses
                    for dep in ctx.jobs[job_id].after
                    if dep["job"] in members
                )
            ]
            for job_id in ready:
                pending.discard(job_id)
                job = ctx.jobs[job_id]
                if not _decide(job, statuses, members):
                    statuses[job_id] = "skipped"
                    continue
                futures[pool.submit(run_job, job, ctx, forced and job_id == root_id)] = job_id

            if not futures:
                for job_id in pending:
                    statuses[job_id] = "skipped"
                break

            done = next(iter(_wait_any(futures)))
            job_id = futures.pop(done)
            try:
                statuses[job_id] = done.result()
            except Exception as exc:
                statuses[job_id] = "error"
                sys.stderr.write("ジョブ %s の実行で例外: %s\n" % (job_id, exc))

    _write_json(
        state_path("runs", run_id, "summary.json"),
        {
            "run_id": run_id,
            "root": root_id,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "results": statuses,
        },
    )
    return statuses


def _wait_any(futures):
    from concurrent.futures import wait, FIRST_COMPLETED

    done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
    return done




def spawn_run_group(root_id, args):
    cmd = [sys.executable, os.path.abspath(__file__), "--config-dir", args.config_dir]
    if args.projects is not None:
        cmd += ["--projects", args.projects]
    cmd += ["_rungroup", root_id]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def read_last_tick():
    try:
        with open(state_path("last-tick"), encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    try:
        return datetime.fromisoformat(text).replace(second=0, microsecond=0)
    except ValueError:
        return None


def cmd_tick(ctx, args, load_errors):
    ensure_state_dirs()
    now = datetime.now().replace(second=0, microsecond=0)
    last_tick = read_last_tick()
    with open(state_path("last-tick"), "w", encoding="utf-8") as fh:
        fh.write(now.isoformat(timespec="seconds") + "\n")

    for job_id, message in load_errors:
        sys.stderr.write("定義エラー %s: %s\n" % (job_id, message))

    broken = set()
    for level, job_id, message in validate_jobs(ctx.jobs):
        if level == "error":
            broken.add(job_id)
            sys.stderr.write("定義エラー %s: %s\n" % (job_id, message))

    pauses = load_pauses()
    if PAUSE_ALL in pauses:
        cleanup_logs()
        return 0

    if last_tick is not None and CATCHUP_MINUTES > 0:
        gap = int((now - last_tick).total_seconds() // 60)
        if gap > CATCHUP_MINUTES:
            sys.stderr.write(
                "%s tickが%d分止まっていました（スリープ等）。"
                "%d分より前に過ぎた回は拾いません\n"
                % (now.isoformat(timespec="minutes"), gap, CATCHUP_MINUTES)
            )

    fired = _read_json(state_path("last-fire.json"), {})
    stamp = now.isoformat(timespec="minutes")
    launched = []

    for job_id in sorted(ctx.jobs):
        job = ctx.jobs[job_id]
        if job_id in broken or not job.crons or not job.enabled:
            continue
        if pause_reason(job_id, pauses):
            continue
        if any(cron.matches(now) for cron in job.crons):
            fire_at = now
        else:
            fire_at = missed_fire_time(job.crons, last_tick, now, CATCHUP_MINUTES)
            if fire_at is None:
                continue
        fire_stamp = fire_at.isoformat(timespec="minutes")
        if fired.get(job_id) == fire_stamp:
            continue
        fired[job_id] = fire_stamp
        launched.append(
            job_id if fire_at == now else "%s（%s の取りこぼし）" % (job_id, fire_stamp)
        )
        spawn_run_group(job_id, args)

    if launched:
        _write_json(state_path("last-fire.json"), fired)
        sys.stderr.write("%s 起動: %s\n" % (stamp, ", ".join(launched)))

    cleanup_logs()
    return 0




def display_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


def job_state_label(job, pauses):
    reason = pause_reason(job.id, pauses)
    if reason:
        return reason
    if not job.enabled:
        return "無効(enabled=false)"
    if running_pid(job.safe_id):
        return "実行中"
    return "有効"


def emit_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))


def job_to_dict(job, ctx, pauses):
    pid = running_pid(job.safe_id)
    if pid:
        state = "running"
    elif pause_reason(job.id, pauses):
        state = "paused"
    elif not job.enabled:
        state = "disabled"
    else:
        state = "idle"

    nxt = job.next_fire() if job.crons else None
    return {
        "id": job.id,
        "scope": job.scope,
        "path": job.path,
        "description": job.description or "",
        "enabled": job.enabled,
        "state": state,
        "state_label": job_state_label(job, pauses),
        "running": pid is not None,
        "pid": pid,
        "crons": [c.raw for c in job.crons],
        "after": [{"job": d["job"], "on": d["on"]} for d in job.after],
        "children": sorted(ctx.children.get(job.id, [])),
        "next_fire": nxt.isoformat(timespec="seconds") if nxt else None,
        "run_type": job.run_type,
        "timeout": job.timeout or DEFAULT_TIMEOUT,
        "last": last_result(job),
    }


def status_data(ctx):
    data = {
        "config_dir": ctx.config_dir,
        "state_home": STATE_HOME,
        "job_count": len(ctx.jobs),
        "cron_job_count": sum(1 for j in ctx.jobs.values() if j.crons),
        "last_tick": None,
        "last_tick_age_seconds": None,
        "paused_all": False,
        "paused": [],
        "healthy": False,
        "problems": [],
    }

    tick_file = state_path("last-tick")
    if not os.path.exists(tick_file):
        data["problems"].append("まだ一度も動いていません（bash setup-scheduler.sh install）")
        return data

    with open(tick_file, encoding="utf-8") as fh:
        stamp = fh.read().strip()
    data["last_tick"] = stamp
    age = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    data["last_tick_age_seconds"] = int(age)
    if age > 180:
        data["problems"].append(
            "3分以上tickが来ていません（bash setup-scheduler.sh status で確認してください）"
        )
        return data

    pauses = load_pauses()
    if PAUSE_ALL in pauses:
        data["paused_all"] = True
    else:
        data["paused"] = sorted(pauses)
    data["healthy"] = True
    return data


def cmd_list(ctx, args, load_errors):
    if getattr(args, "json", False):
        pauses = load_pauses()
        emit_json({
            "jobs": [job_to_dict(ctx.jobs[i], ctx, pauses) for i in sorted(ctx.jobs)],
            "errors": [{"job": j, "message": m} for j, m in load_errors],
        })
        return 0

    if not ctx.jobs:
        print("ジョブ定義がありません（global/schedules/ か projects/<name>/schedules/ に置きます）")
    pauses = load_pauses()
    rows = []
    for job_id in sorted(ctx.jobs):
        job = ctx.jobs[job_id]
        if job.crons:
            nxt = job.next_fire()
            when = nxt.strftime("%Y-%m-%d %H:%M") if nxt else "（40日以内に無し）"
        else:
            when = "依存: %s" % ", ".join(d["job"] for d in job.after)
        last = last_result(job)
        last_label = (
            "%s (%s)" % (last["status"], last["started"][5:16].replace("T", " "))
            if last else "-"
        )
        rows.append((job_id, job_state_label(job, pauses), when, last_label))

    header = ("ジョブID", "状態", "次回", "最終結果")
    widths = [
        max([display_width(row[i]) for row in rows] + [display_width(header[i])])
        for i in range(4)
    ]
    print("  ".join(pad(header[i], widths[i]) for i in range(4)).rstrip())
    print("-" * (sum(widths) + 6))
    for row in rows:
        print("  ".join(pad(row[i], widths[i]) for i in range(4)).rstrip())

    for job_id, message in load_errors:
        print("  定義エラー %s: %s" % (job_id, message))
    return 0


def cmd_show(ctx, args, load_errors):
    job = _require_job(ctx, args.job_id)
    pauses = load_pauses()
    print("ジョブID   : %s" % job.id)
    print("定義       : %s" % job.path)
    print("説明       : %s" % (job.description or "-"))
    print("状態       : %s" % job_state_label(job, pauses))
    if job.crons:
        print("cron       : %s" % ", ".join(c.raw for c in job.crons))
        nxt = job.next_fire()
        print("次回       : %s" % (nxt.strftime("%Y-%m-%d %H:%M") if nxt else "（40日以内に無し）"))
    for dep in job.after:
        print("依存       : %s の完了後（on=%s）" % (dep["job"], dep["on"]))
    for child in sorted(ctx.children.get(job.id, [])):
        print("後続       : %s" % child)
    print("実行       : type=%s, cwd=%s" % (job.run_type, resolve_cwd(job, ctx)))
    print("timeout    : %s秒" % (job.timeout or DEFAULT_TIMEOUT))
    last = last_result(job)
    if last:
        print("最終結果   : %s (%s)" % (last["status"], last["started"]))
        print("最終ログ   : %s" % last["log"])
    return 0


def cmd_validate(ctx, args, load_errors):
    failed = bool(load_errors)
    for job_id, message in load_errors:
        print("ERROR %s: %s" % (job_id, message))
    for level, job_id, message in validate_jobs(ctx.jobs):
        print("%s %s: %s" % (level.upper(), job_id, message))
        failed = failed or level == "error"
    if not failed:
        print("OK: %d件のジョブ定義に問題はありません" % len(ctx.jobs))
    return 1 if failed else 0


def cmd_run(ctx, args, load_errors):
    job = _require_job(ctx, args.job_id)
    statuses = run_group(job.id, ctx, with_deps=args.with_deps, forced=True)
    for job_id in sorted(statuses):
        print("%-30s %s" % (job_id, statuses[job_id]))
    return 0 if all(s == "success" for s in statuses.values()) else 1


def cmd_logs(ctx, args, load_errors):
    job = _require_job(ctx, args.job_id)
    directory = log_dir(job.safe_id)
    files = sorted(
        (f for f in os.listdir(directory) if f.endswith(".log")), reverse=True
    ) if os.path.isdir(directory) else []
    if getattr(args, "json", False):
        logs = []
        for name in files[: args.number]:
            path = os.path.join(directory, name)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                logs.append({"name": name, "path": path, "content": fh.read()})
        emit_json({"job": job.id, "logs": logs})
        return 0

    if not files:
        print("ログがまだありません: %s" % job.id)
        return 0
    for name in files[: args.number]:
        path = os.path.join(directory, name)
        print("===== %s =====" % path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            sys.stdout.write(fh.read())
    return 0


def _set_enabled(ctx, job_id, value):
    job = _require_job(ctx, job_id)
    data = dict(job.raw)
    data["enabled"] = value
    _write_json(job.path, data)
    print("%s の enabled を %s にしました: %s" % (job_id, value, job.path))
    print("（この変更は定義ファイルを書き換えます。コミットすると全PCに波及します。"
          "一時的に止めたいだけなら pause を使ってください）")
    return 0


def cmd_enable(ctx, args, load_errors):
    return _set_enabled(ctx, args.job_id, True)


def cmd_disable(ctx, args, load_errors):
    return _set_enabled(ctx, args.job_id, False)


def cmd_pause(ctx, args, load_errors):
    key = PAUSE_ALL if args.all else args.job_id
    if not key:
        raise SystemExit("Error: ジョブIDか --all を指定してください")
    if not args.all:
        _require_job(ctx, key)
    until = parse_until(args.until)
    pauses = load_pauses()
    pauses[key] = {"until": until.isoformat(timespec="seconds") if until else None,
                   "paused_at": datetime.now().isoformat(timespec="seconds")}
    _write_json(state_path("paused.json"), pauses)
    print("一時停止しました: %s（%s）"
          % ("全ジョブ" if args.all else key,
             "%s まで" % until.isoformat(timespec="minutes") if until else "無期限"))
    print("定義ファイルは変更していません（この状態はこのPCのみに残ります）")
    return 0


def cmd_resume(ctx, args, load_errors):
    key = PAUSE_ALL if args.all else args.job_id
    if not key:
        raise SystemExit("Error: ジョブIDか --all を指定してください")
    pauses = load_pauses()
    if key not in pauses:
        print("一時停止されていません: %s" % ("全ジョブ" if args.all else key))
        return 0
    del pauses[key]
    _write_json(state_path("paused.json"), pauses)
    print("再開しました: %s" % ("全ジョブ" if args.all else key))
    return 0


def cmd_kill(ctx, args, load_errors):
    job = _require_job(ctx, args.job_id)
    info = running_info(job.safe_id)
    if not info:
        print("実行中ではありません: %s" % job.id)
        return 0
    pid = info.get("child_pid")
    if not pid:
        print("まだジョブのプロセスが起動していません（数秒待って再実行してください）: %s" % job.id)
        return 1
    pid = int(pid)
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True)
        else:
            os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("既に終了していました: %s" % job.id)
        return 0
    except Exception as exc:
        raise SystemExit("Error: 停止できませんでした (pid=%s): %s" % (pid, exc))
    print("停止シグナルを送りました: %s (pid=%s)" % (job.id, pid))
    return 0


def cmd_status(ctx, args, load_errors):
    data = status_data(ctx)

    if getattr(args, "json", False):
        emit_json(data)
        return 0 if data["healthy"] else 1

    print("定義ディレクトリ : %s" % data["config_dir"])
    print("実行時データ     : %s" % data["state_home"])
    print("ジョブ数         : %d件（うちcron起点 %d件）"
          % (data["job_count"], data["cron_job_count"]))

    if data["last_tick"] is None:
        print("最終tick         : なし（まだ一度も動いていません）")
        print("→ セットアップが必要です: bash setup-scheduler.sh install")
        return 1
    print("最終tick         : %s（%d秒前）"
          % (data["last_tick"], data["last_tick_age_seconds"]))
    if not data["healthy"]:
        print("→ 3分以上tickが来ていません。登録が外れている可能性があります"
              "（bash setup-scheduler.sh status で確認してください）")
        return 1

    if data["paused_all"]:
        print("→ 全ジョブが一時停止中です（resume --all で再開）")
    elif data["paused"]:
        print("一時停止中       : %s" % ", ".join(data["paused"]))
    print("正常に稼働しています")
    return 0


def cmd_add(ctx, args, load_errors):
    scope = args.scope
    if scope != "global" and scope not in ctx.projects:
        raise SystemExit(
            "Error: 不明なスコープ %r です。'global' か sync-links.conf の PROJECTS 名"
            "（%s）を指定してください" % (scope, ", ".join(sorted(ctx.projects)) or "なし")
        )
    directory = (
        os.path.join(ctx.config_dir, "global", "schedules")
        if scope == "global"
        else os.path.join(ctx.config_dir, "projects", scope, "schedules")
    )
    path = os.path.join(directory, args.name + ".json")
    if os.path.exists(path) and not args.force:
        raise SystemExit("Error: 既に存在します（上書きするなら --force）: %s" % path)

    data = {"description": args.description or "", "enabled": True}
    if args.cron:
        data["trigger"] = {"cron": args.cron if len(args.cron) > 1 else args.cron[0]}
    if args.after:
        data["after"] = [{"job": a, "on": args.on} for a in args.after]
    run = {"type": args.type}
    if args.type == "command":
        if not args.command:
            raise SystemExit("Error: --type command には --command が必要です")
        run["command"] = args.command
    else:
        if not args.prompt:
            raise SystemExit("Error: --type claude には --prompt が必要です")
        run["prompt"] = args.prompt
        if args.allowed_tools:
            run["allowedTools"] = args.allowed_tools
    if args.cwd:
        run["cwd"] = args.cwd
    data["run"] = run
    if args.timeout:
        data["timeout"] = args.timeout

    try:
        Job("%s/%s" % (scope, args.name), scope, path, data)
    except (JobError, CronError) as exc:
        raise SystemExit("Error: 定義が不正です: %s" % exc)

    _write_json(path, data)
    print("作成しました: %s" % path)
    print("ジョブID: %s/%s" % (scope, args.name))
    print("動作確認: bash global/scheduler/scheduler.sh run %s/%s" % (scope, args.name))
    return 0


def cmd_remove(ctx, args, load_errors):
    job = _require_job(ctx, args.job_id)
    dependents = sorted(ctx.children.get(job.id, []))
    if dependents and not args.force:
        raise SystemExit(
            "Error: このジョブに依存している後続ジョブがあります: %s\n"
            "先に依存を外すか、--force を付けてください" % ", ".join(dependents)
        )
    if not args.yes:
        answer = input("%s (%s) を削除しますか? [y/N]: " % (job.id, job.path))
        if answer.strip().lower() not in ("y", "yes"):
            print("中止しました")
            return 1
    os.remove(job.path)
    print("削除しました: %s" % job.path)
    return 0


def cmd_rungroup(ctx, args, load_errors):
    if args.job_id not in ctx.jobs:
        sys.stderr.write("Error: 不明なジョブです: %s\n" % args.job_id)
        return 1
    run_group(args.job_id, ctx, with_deps=True, forced=False)
    return 0


def _require_job(ctx, job_id):
    if job_id not in ctx.jobs:
        known = "\n  ".join(sorted(ctx.jobs)) or "（定義なし）"
        raise SystemExit("Error: 不明なジョブID %r です。既存のジョブ:\n  %s" % (job_id, known))
    return ctx.jobs[job_id]




def self_test():
    failures = []

    def check(label, actual, expected):
        if actual != expected:
            failures.append("%s: expected=%r actual=%r" % (label, expected, actual))

    def at(text):
        return datetime.strptime(text, "%Y-%m-%d %H:%M")

    check("毎分", CronExpr("* * * * *").matches(at("2026-08-27 03:04")), True)
    check("分一致", CronExpr("17 9 * * *").matches(at("2026-08-27 09:17")), True)
    check("分不一致", CronExpr("17 9 * * *").matches(at("2026-08-27 09:18")), False)

    check("*/5 hit", CronExpr("*/5 * * * *").matches(at("2026-08-27 10:35")), True)
    check("*/5 miss", CronExpr("*/5 * * * *").matches(at("2026-08-27 10:36")), False)
    check("範囲内", CronExpr("0 9-18 * * *").matches(at("2026-08-27 18:00")), True)
    check("範囲外", CronExpr("0 9-18 * * *").matches(at("2026-08-27 19:00")), False)
    check("範囲+step", CronExpr("*/30 9-18 * * *").matches(at("2026-08-27 09:30")), True)

    triple = CronExpr("0 9,13,18 * * *")
    for hour, expected in ((9, True), (13, True), (18, True), (12, False)):
        check("1日複数回 %d時" % hour, triple.matches(at("2026-08-27 %02d:00" % hour)), expected)

    weekday = CronExpr("17 9 * * 1-5")
    check("平日(木)", weekday.matches(at("2026-08-27 09:17")), True)
    check("週末(土)", weekday.matches(at("2026-08-29 09:17")), False)
    check("日曜=0", CronExpr("0 0 * * 0").matches(at("2026-08-30 00:00")), True)
    check("日曜=7", CronExpr("0 0 * * 7").matches(at("2026-08-30 00:00")), True)

    both = CronExpr("0 0 1 * 0")
    check("dom一致のみ", both.matches(at("2026-08-01 00:00")), True)
    check("dow一致のみ", both.matches(at("2026-08-30 00:00")), True)
    check("どちらも不一致", both.matches(at("2026-08-27 00:00")), False)

    check("月末31日", CronExpr("0 0 31 * *").matches(at("2026-08-31 00:00")), True)

    triple_cron = [CronExpr("13 7,13,19 * * *")]
    check(
        "取りこぼし: スリープ中に過ぎた回を拾う",
        missed_fire_time(triple_cron, at("2026-08-28 07:04"), at("2026-08-28 07:16"), 360),
        at("2026-08-28 07:13"),
    )
    check(
        "取りこぼし: 通過した回が無ければNone",
        missed_fire_time(triple_cron, at("2026-08-28 07:14"), at("2026-08-28 07:16"), 360),
        None,
    )
    check(
        "取りこぼし: 拾う範囲より前は拾わない",
        missed_fire_time(triple_cron, at("2026-08-27 18:00"), at("2026-08-28 07:00"), 60),
        None,
    )
    check(
        "取りこぼし: 長時間止まっていても範囲内なら拾う",
        missed_fire_time(triple_cron, at("2026-08-27 18:00"), at("2026-08-28 07:16"), 360),
        at("2026-08-28 07:13"),
    )
    check(
        "取りこぼし: 複数溜まっていても最後の1回だけ",
        missed_fire_time(triple_cron, at("2026-08-28 06:00"), at("2026-08-28 14:00"), 600),
        at("2026-08-28 13:13"),
    )
    check(
        "取りこぼし: 0分指定で無効化",
        missed_fire_time(triple_cron, at("2026-08-28 07:04"), at("2026-08-28 07:16"), 0),
        None,
    )
    check(
        "取りこぼし: 前回tick不明なら拾わない",
        missed_fire_time(triple_cron, None, at("2026-08-28 07:16"), 360),
        None,
    )
    check("月不一致", CronExpr("0 0 1 1 *").matches(at("2026-08-01 00:00")), False)

    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "5-1 * * * *", "*/0 * * * *", "a * * * *"):
        try:
            CronExpr(bad)
            failures.append("不正な式が通ってしまった: %r" % bad)
        except CronError:
            pass

    crons = [CronExpr("0 9 * * 1-5"), CronExpr("0 23 * * 6")]
    check(
        "配列OR: 金曜9時の次は土曜23時",
        next_fire_time(crons, at("2026-08-28 09:00")),
        at("2026-08-29 23:00"),
    )
    check("次回無し", next_fire_time([CronExpr("0 0 30 2 *")], at("2026-08-27 00:00"), horizon_days=5), None)

    def make(data):
        return Job("global/x", "global", "x.json", data)

    check("cron配列受理", len(make(
        {"trigger": {"cron": ["0 9 * * 1-5", "0 23 * * 6"]},
         "run": {"type": "command", "command": "true"}}).crons), 2)
    check("after短縮形", make(
        {"after": "global/a", "run": {"type": "command", "command": "true"}}).after,
        [{"job": "global/a", "on": "success"}])

    job = make({"trigger": {"cron": "0 9 * * *"},
                "run": {"type": "command", "command": "true"}})
    ctx = Context("/tmp", {}, {job.id: job})
    payload = job_to_dict(job, ctx, {})
    for key in ("id", "scope", "path", "description", "enabled", "state", "state_label",
                "running", "pid", "crons", "after", "children", "next_fire",
                "run_type", "timeout", "last"):
        check("job_to_dict に %s がある" % key, key in payload, True)
    check("job_to_dict の state", payload["state"], "idle")
    check("job_to_dict の crons", payload["crons"], ["0 9 * * *"])
    try:
        json.dumps(payload, ensure_ascii=False)
        serializable = True
    except TypeError:
        serializable = False
    check("job_to_dict がJSON化できる", serializable, True)

    disabled = make({"enabled": False, "trigger": {"cron": "0 9 * * *"},
                     "run": {"type": "command", "command": "true"}})
    check("無効ジョブのstate", job_to_dict(disabled, ctx, {})["state"], "disabled")
    check("一時停止中のstate",
          job_to_dict(job, ctx, {job.id: {}})["state"], "paused")

    for label, data in (
        ("cronとafterの同時指定", {"trigger": {"cron": "0 9 * * *"}, "after": ["global/a"],
                                   "run": {"type": "command", "command": "true"}}),
        ("トリガー無し", {"run": {"type": "command", "command": "true"}}),
        ("不明なtype", {"trigger": {"cron": "* * * * *"}, "run": {"type": "python"}}),
        ("commandなし", {"trigger": {"cron": "* * * * *"}, "run": {"type": "command"}}),
        ("promptなし", {"trigger": {"cron": "* * * * *"}, "run": {"type": "claude"}}),
        ("権限バイパス指定", {"trigger": {"cron": "* * * * *"},
                              "run": {"type": "claude", "prompt": "x",
                                      "dangerouslySkipPermissions": True}}),
    ):
        try:
            make(data)
            failures.append("不正な定義が通ってしまった: %s" % label)
        except (JobError, CronError):
            pass

    def graph(edges):
        jobs = {}
        for job_id, parents in edges.items():
            data = {"run": {"type": "command", "command": "true"}}
            if parents:
                data["after"] = list(parents)
            else:
                data["trigger"] = {"cron": "* * * * *"}
            jobs[job_id] = Job(job_id, "global", "x.json", data)
        return jobs

    check("循環なし", _find_cycles(graph({"a": [], "b": ["a"], "c": ["a"]})), [])
    check("循環あり", len(_find_cycles(graph({"a": ["c"], "b": ["a"], "c": ["b"]}))) > 0, True)

    jobs = graph({"a": [], "b": ["a"], "c": ["a"]})
    jobs["d"] = Job("d", "global", "x.json",
                    {"after": [{"job": "a", "on": "failure"}],
                     "run": {"type": "command", "command": "true"}})
    jobs["e"] = Job("e", "global", "x.json",
                    {"after": [{"job": "b", "on": "always"}],
                     "run": {"type": "command", "command": "true"}})
    members = set(jobs)
    check("親成功→on=success は走る", _decide(jobs["b"], {"a": "success"}, members), True)
    check("親失敗→on=success は走らない", _decide(jobs["b"], {"a": "failure"}, members), False)
    check("親失敗→on=failure は走る", _decide(jobs["d"], {"a": "failure"}, members), True)
    check("親成功→on=failure は走らない", _decide(jobs["d"], {"a": "success"}, members), False)
    check("親タイムアウト→on=always は走る", _decide(jobs["e"], {"b": "timeout"}, members), True)
    for status in NOT_RAN_STATUSES:
        check("親が%s→on=alwaysでも走らない" % status,
              _decide(jobs["e"], {"b": status}, members), False)

    ctx = Context("/tmp", {}, graph({"a": [], "b": ["a"], "c": ["b"], "z": []}))
    check("subgraph", subgraph_from("a", ctx), {"a", "b", "c"})

    base = datetime.now()
    check("until 2h", (parse_until("2h") - base).total_seconds() > 7100, True)
    check("until 省略", parse_until(None), None)
    check("until ISO", parse_until("2026-08-28T09:00"), at("2026-08-28 09:00"))

    if failures:
        print("セルフテスト失敗 (%d件):" % len(failures))
        for line in failures:
            print("  - %s" % line)
        return 1
    print("セルフテスト: すべて成功")
    return 0




def build_parser():
    parser = argparse.ArgumentParser(
        prog="dispatch.py",
        description="定期実行スケジューラのディスパッチャ（通常は scheduler.sh 経由で呼ぶ）",
    )
    parser.add_argument("--config-dir", default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    parser.add_argument("--projects", default=None,
                        help="プロジェクト名→パスのJSON。省略時は sync-links.conf から読む")
    parser.add_argument("--self-test", action="store_true", help="単体テストを実行して終了する")
    sub = parser.add_subparsers(dest="subcommand")

    JSON_HELP = "機械可読なJSONで出力する（claude-scheduler-app 用）"

    sub.add_parser("tick", help="発火すべきジョブを起動する（OSのスケジューラから呼ばれる）")
    p = sub.add_parser("list", help="ジョブ一覧を表示する")
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    sub.add_parser("validate", help="全定義を検査する")
    p = sub.add_parser("status", help="スケジューラが稼働しているか確認する")
    p.add_argument("--json", action="store_true", help=JSON_HELP)

    p = sub.add_parser("show", help="ジョブの詳細を表示する")
    p.add_argument("job_id")

    p = sub.add_parser("run", help="スケジュールを無視して即時実行する")
    p.add_argument("job_id")
    p.add_argument("--with-deps", action="store_true",
                   help="このジョブに依存する後続ジョブ（子孫）も続けて実行する")

    p = sub.add_parser("logs", help="直近の実行ログを表示する")
    p.add_argument("job_id")
    p.add_argument("-n", "--number", type=int, default=1, help="表示する件数（新しい順）")
    p.add_argument("--json", action="store_true", help=JSON_HELP)

    for name, help_text in (("enable", "有効化する"), ("disable", "無効化する（定義ファイルを書き換える）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("job_id")

    p = sub.add_parser("pause", help="一時停止する（このPCのみ・定義ファイルは変更しない）")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--all", action="store_true", help="全ジョブを止める")
    p.add_argument("--until", help="自動復帰する時刻。'30m' / '2h' / '3d' / 2026-08-28T09:00")

    p = sub.add_parser("resume", help="一時停止を解除する")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("kill", help="実行中のジョブを停止する")
    p.add_argument("job_id")

    p = sub.add_parser("add", help="ジョブ定義を新規作成する")
    p.add_argument("name", help="ジョブ名（ファイル名になる）")
    p.add_argument("--scope", default="global", help="global またはプロジェクト名")
    p.add_argument("--cron", action="append", help="cron式（複数指定でOR）")
    p.add_argument("--after", action="append", help="依存する親ジョブID（複数指定でjoin）")
    p.add_argument("--on", default="success", choices=["success", "failure", "always"])
    p.add_argument("--type", default="command", choices=["command", "claude"])
    p.add_argument("--command", help="type=command のときに実行するシェルコマンド")
    p.add_argument("--prompt", help="type=claude のときに渡すプロンプト")
    p.add_argument("--allowed-tools", nargs="*", help="type=claude で許可するツール")
    p.add_argument("--cwd", help="作業ディレクトリ（プロジェクト名か絶対パス）")
    p.add_argument("--timeout", type=int, help="タイムアウト秒数")
    p.add_argument("--description", help="説明")
    p.add_argument("--force", action="store_true", help="既存の定義を上書きする")

    p = sub.add_parser("remove", help="ジョブ定義を削除する")
    p.add_argument("job_id")
    p.add_argument("--yes", action="store_true", help="確認せずに削除する")
    p.add_argument("--force", action="store_true", help="後続ジョブがあっても削除する")

    p = sub.add_parser("_rungroup", help=argparse.SUPPRESS)
    p.add_argument("job_id")

    return parser


HANDLERS = {
    "tick": cmd_tick, "list": cmd_list, "show": cmd_show, "validate": cmd_validate,
    "run": cmd_run, "logs": cmd_logs, "enable": cmd_enable, "disable": cmd_disable,
    "pause": cmd_pause, "resume": cmd_resume, "kill": cmd_kill, "status": cmd_status,
    "add": cmd_add, "remove": cmd_remove, "_rungroup": cmd_rungroup,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.subcommand:
        parser.print_help()
        return 1

    if args.projects is None:
        projects = parse_conf_projects(args.config_dir)
    else:
        try:
            projects = json.loads(args.projects)
        except ValueError:
            sys.stderr.write("Error: --projects がJSONとして読めません\n")
            return 1

    jobs, load_errors = load_jobs(args.config_dir, projects)
    ctx = Context(args.config_dir, projects, jobs)
    return HANDLERS[args.subcommand](ctx, args, load_errors)


if __name__ == "__main__":
    sys.exit(main())
