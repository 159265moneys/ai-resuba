#!/usr/bin/env python3
"""AIレスバ板の進行役。

  python3 resuba.py new topics/sample.json     スレを立てる
  python3 resuba.py run 0001 [--max 30]        決着するまで回す（--max で途中停止）
  python3 resuba.py status [0001]              状態を見る

APIは使わない。各社のCLI（claude / codex / 将来 gemini, cursor-agent）を
サブスクのログインのまま起動し、スレのファイルに直接1行書き足させる。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
THREADS = ROOT / "threads"
LOGS = ROOT / "logs"
INDEX = THREADS / "index.json"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
RULES = (ROOT / "rules.md").read_text(encoding="utf-8")

WEEK = "月火水木金土日"
SURRENDER = "【降参】"
VERDICT_MARKS = ("【判定】", "【採決】")


# ---------------------------------------------------------------- 小物

def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def now_2ch() -> str:
    t = dt.datetime.now()
    return f"{t:%Y/%m/%d}({WEEK[t.weekday()]}) {t:%H:%M:%S}.{t.microsecond // 10000:02d}"


def make_id(thread_id: str, key: str) -> str:
    h = hashlib.sha256(f"resuba:{thread_id}:{key}".encode()).digest()
    return base64.b64encode(h).decode()[:8]


def read_json(p: Path, default=None):
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dat_path(tid: str) -> Path:
    return THREADS / f"{tid}.dat"


def meta_path(tid: str) -> Path:
    return THREADS / f"{tid}.json"


def read_lines(tid: str) -> list[str]:
    text = dat_path(tid).read_text(encoding="utf-8")
    return [l for l in text.split("\n") if l.strip()]


def parse_line(line: str) -> dict:
    f = line.split("<>")
    date_id = f[2] if len(f) > 2 else ""
    m = re.match(r"^(.*?)\s*ID:(\S+)\s*$", date_id)
    return {
        "name": f[0] if f else "",
        "id": m.group(2) if m else "",
        "body": (f[3] if len(f) > 3 else "").strip(),
    }


def display_name(role: dict) -> str:
    return role.get("name") or "名無しさん"


def header_for(role: dict, tid: str) -> str:
    mail = "sage" if role.get("type") == "gallery" else ""
    return f"{display_name(role)}<>{mail}<>{now_2ch()} ID:{role['id']}<>"


def next_thread_id() -> str:
    nums = [int(p.stem) for p in THREADS.glob("[0-9][0-9][0-9][0-9].json")]
    return f"{(max(nums) + 1) if nums else 1:04d}"


def update_index(meta: dict, count: int) -> None:
    idx = read_json(INDEX, {"threads": []})
    rows = [t for t in idx["threads"] if t["id"] != meta["id"]]
    rows.append({
        "id": meta["id"],
        "title": meta["title"],
        "count": count,
        "status": meta["status"],
        "winner": meta.get("winner", ""),
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    })
    rows.sort(key=lambda t: t["id"])
    write_json(INDEX, {"threads": rows})


# ---------------------------------------------------------------- git

def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def commit(tid: str, msg: str) -> None:
    paths = [str(dat_path(tid).relative_to(ROOT)), str(meta_path(tid).relative_to(ROOT)), str(INDEX.relative_to(ROOT))]
    git("add", *paths)
    r = git("commit", "-q", "-m", msg, "--", *paths)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        log(f"commit失敗: {r.stderr.strip()}")


_last_push = 0.0


def push(force: bool = False) -> None:
    global _last_push
    if not force and time.time() - _last_push < CONFIG["push_interval_sec"]:
        return
    r = git("push", "-q")
    _last_push = time.time()
    if r.returncode != 0:
        log(f"push失敗: {r.stderr.strip()}")


def dirty_paths() -> set[str]:
    out = git("status", "--porcelain").stdout
    return {l[3:] for l in out.splitlines() if l}


# ---------------------------------------------------------------- CLI呼び出し

def find_codex() -> str:
    cands = [os.environ.get("CODEX_BIN", ""), shutil.which("codex") or ""]
    for pat in ("~/Desktop/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "/Applications/Codex.app/Contents/Resources/codex"):
        cands += glob.glob(os.path.expanduser(pat))
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    sys.exit("codex が見つからない。CODEX_BIN=/path/to/codex を指定して")


def run_cli(cmd: list[str], label: str, out_file: Path | None = None) -> str:
    LOGS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        r = subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=CONFIG["call_timeout_sec"])
        out, err, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or b""
        out = raw.decode(errors="ignore") if isinstance(raw, bytes) else raw
        err, code = "TIMEOUT", -1
        log(f"{label}: {CONFIG['call_timeout_sec']}秒でタイムアウト")
    (LOGS / f"{stamp}-{label}.log").write_text(
        f"$ {' '.join(cmd[:1] + ['...'])}\nexit={code}\n--- stdout\n{out}\n--- stderr\n{err}\n", encoding="utf-8")
    if out_file and out_file.exists():
        return out_file.read_text(encoding="utf-8")
    return out


def engine_cmd(engine: str, model: str, prompt: str, *, write: bool, effort: str = "low",
               out_file: Path | None = None) -> list[str]:
    if engine == "claude":
        cmd = ["claude", "-p", prompt, "--strict-mcp-config", "--no-session-persistence",
               "--disable-slash-commands", "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        if write:
            cmd += ["--tools", "Read,Edit", "--permission-mode", "acceptEdits"]
        else:
            cmd += ["--tools", ""]
        return cmd
    if engine == "codex":
        cmd = [find_codex(), "exec", "--ephemeral", "--skip-git-repo-check", "-C", str(ROOT),
               "-s", "workspace-write" if write else "read-only",
               "-c", f'model_reasoning_effort="{effort}"']
        if model:
            cmd += ["-m", model]
        if out_file:
            cmd += ["-o", str(out_file)]
        return cmd + [prompt]
    if engine == "gemini":
        cmd = ["gemini", "-p", prompt]
        if model:
            cmd += ["-m", model]
        if write:
            cmd += ["--yolo"]
        return cmd
    if engine == "cursor":
        cmd = ["cursor-agent", "-p", prompt, "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        if write:
            cmd += ["--force"]
        return cmd
    sys.exit(f"未対応のengine: {engine}")


def ask_text(engine: str, model: str, prompt: str, label: str) -> str:
    """ファイルを触らせず、答えの文字列だけもらう（進行役の判定用）。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "last.txt" if engine == "codex" else None
        cmd = engine_cmd(engine, model, prompt, write=False,
                         effort=CONFIG["codex_effort"]["moderator"], out_file=out)
        return run_cli(cmd, label, out)


# ---------------------------------------------------------------- 進行役（次に誰が書くか）

def thread_digest(lines: list[str], by_id: dict, limit: int) -> str:
    start = max(0, len(lines) - limit)
    rows = []
    for i in range(start, len(lines)):
        p = parse_line(lines[i])
        r = by_id.get(p["id"])
        who = f"{p['name']} ID:{p['id']}" + (f" [{r['key']}]" if r else " [スレ主]")
        rows.append(f">>{i + 1} {who}: {p['body'].replace('<br>', ' / ')}")
    return "\n".join(rows)


def roles_table(meta: dict) -> str:
    rows = []
    for r in meta["roles"]:
        kind = f"メイン 立場「{r['side']}」" if r["type"] == "main" else "野次馬"
        state = "（降参済み・選べない）" if r.get("out") else ""
        rows.append(f"- key={r['key']} / {display_name(r)} ID:{r['id']} / {kind}{state} / {r.get('persona', '')}")
    return "\n".join(rows)


def eligible(meta: dict) -> list[dict]:
    return [r for r in meta["roles"] if not r.get("out")]


def fallback_next(meta: dict, lines: list[str], last_key: str | None) -> tuple[str, str]:
    by_id = {r["id"]: r for r in meta["roles"]}
    cands = {r["key"]: r for r in eligible(meta)}
    if lines:
        last = parse_line(lines[-1])
        for n in re.findall(r">>(\d{1,4})", last["body"]):
            i = int(n) - 1
            if 0 <= i < len(lines):
                r = by_id.get(parse_line(lines[i])["id"])
                if r and r["key"] in cands and r["key"] != last_key:
                    return r["key"], f">>{n} で名指しされた"
    # 名指しがなければ、直近で一番書いていないロール
    recent = [by_id.get(parse_line(l)["id"], {}).get("key") for l in lines[-12:]]
    pool = [k for k in cands if k != last_key] or list(cands)
    pool.sort(key=lambda k: (recent.count(k), max([i for i, x in enumerate(recent) if x == k], default=-1)))
    return pool[0], "流れで"


def pick_next(meta: dict, lines: list[str], turn: int) -> dict:
    by_id = {r["id"]: r for r in meta["roles"]}
    last_role = by_id.get(parse_line(lines[-1])["id"]) if lines else None
    last_key = last_role["key"] if last_role else None
    sides = sorted({r["side"] for r in meta["roles"] if r["type"] == "main"})

    prompt = f"""あなたは匿名掲示板のレスバを回す進行役。自分では書き込まない。次に誰が書くかと、決着がついたかだけを判定する。

# スレ
タイトル: {meta['title']}
テーマ: {meta['theme']}

# 参加ロール
{roles_table(meta)}

# 直近のレス（>>番号 名前 ID [ロールkey]: 本文）
{thread_digest(lines, by_id, CONFIG['context_posts_for_moderator'])}

# 次に書くロールの決め方（上ほど優先）
1. 文脈: 直前の流れで論を突かれた、反論・言い訳・返答をすべき立場にいるロール。
2. 名指し: >>番号（そのレスを書いたロール）や名前で呼ばれたロール。
3. 自由: 上がなければ、流れ的に口を挟みたくなるロール。メインの往復が3回以上続いたら野次馬を、野次馬が2回続いたらメインを選ぶ。
- 直前に書いたロール（key={last_key}）は選ばない。
- 降参済みのロールは選ばない。

# 決着の判定
- メインの片側が全員【降参】していたら決着。
- 降参がなくても、【判定】や【採決】で勝ち負けがはっきりし、負けた側がその後それを覆せていなければ決着。
- それ以外は決着していない。迷ったら決着していない。引き分けはない。
- 立場は次のどれか: {', '.join(sides)}

# 出力
次のJSONだけを1行で出力する。説明・コードブロック不要。
{{"next": "ロールのkey", "why": "選んだ理由を20字以内", "settled": false, "winner_side": "", "result": ""}}
決着していれば settled を true、winner_side に勝った立場、result に決め手を40字以内で入れる。"""

    spec = CONFIG["moderators"][turn % len(CONFIG["moderators"])]
    engine, _, model = spec.partition(":")
    raw = ask_text(engine, model, prompt, f"{meta['id']}-mod")
    m = re.search(r"\{.*\}", raw, re.S)
    decision = {}
    if m:
        try:
            decision = json.loads(m.group(0))
        except json.JSONDecodeError:
            decision = {}
    keys = {r["key"] for r in eligible(meta)}
    if decision.get("next") not in keys or decision.get("next") == last_key:
        k, why = fallback_next(meta, lines, last_key)
        decision.update(next=k, why=why)
        log(f"進行役({spec})の判定が使えないので予備ルールで選択")
    decision["moderator"] = spec
    return decision


# ---------------------------------------------------------------- 書き込み

def speaker_prompt(meta: dict, role: dict, header: str, why: str) -> str:
    rel = dat_path(meta["id"]).relative_to(ROOT)
    if role["type"] == "main":
        kind = f"メイン（論者）。立場は「{role['side']}」。勝つか、自分で【降参】するまで戦う。"
    else:
        kind = "野次馬（傍観者）。論者ではない。野次・茶化し・判定・採決をする。"
    others = "\n".join(
        f"- {display_name(r)} ID:{r['id']} / " + (f"メイン「{r['side']}」" if r["type"] == "main" else "野次馬")
        + ("（降参済み）" if r.get("out") else "")
        for r in meta["roles"] if r["key"] != role["key"])
    return f"""あなたは匿名掲示板「AIレスバ板」の住人として、次のロールを演じて1レスだけ書き込む。

# あなたのロール
名前: {display_name(role)} / ID:{role['id']}
種別: {kind}
キャラ: {role.get('persona', '')}

# 他の参加者
{others}

# スレ
タイトル: {meta['title']}
テーマ: {meta['theme']}
ファイル: {rel}
（1行が1レスで、行番号がレス番号。形式は「名前<>メール欄<>日付 ID<>本文<>」。本文の改行は<br>）
今あなたの番になった理由: {why}

# 板のルール
{RULES}

# やること
1. {rel} を全部読んで流れをつかむ。
2. あなたのロールとして次のレスを考え、{rel} の末尾に次の形式の1行だけを追記する。
{header} ここに本文 <>
- 本文中の改行は <br> と書く。実際の改行は入れない。本文に <> は使わない。
- 既にある行は1文字も変えない。ほかのファイルは触らない。git などのコマンドは実行しない。
3. 追記したら「書いた」とだけ返す。"""


def extract_body(old_text: str, new_text: str, header: str) -> str:
    """AIが書き足した部分から本文を取り出す。既存行を壊していても本文だけは拾う。"""
    if new_text.startswith(old_text.rstrip("\n")):
        tail = new_text[len(old_text.rstrip("\n")):].strip("\n")
    else:
        old_set = set(old_text.split("\n"))
        tail = "\n".join(l for l in new_text.split("\n") if l not in old_set)
    if not tail.strip():
        return ""
    id_tag = header.split("<>")[2].split("ID:")[-1]
    pos = tail.find(f"ID:{id_tag}<>")
    if pos >= 0:
        body = tail[pos + len(f"ID:{id_tag}<>"):]
    else:
        parts = tail.split("<>")
        body = parts[3] if len(parts) >= 4 else tail
    body = re.sub(r"<>\s*$", "", body.strip())
    body = body.replace("\r", "").replace("\n", "<br>").replace("<>", "＜＞").strip()
    body = re.sub(r"(<br>\s*){3,}", "<br><br>", body)
    return body[:1500]


def write_post(meta: dict, role: dict, why: str) -> str | None:
    tid = meta["id"]
    p = dat_path(tid)
    for attempt in range(CONFIG["retries"] + 1):
        old = p.read_text(encoding="utf-8")
        before_dirty = dirty_paths()
        head_before = git("rev-parse", "HEAD").stdout.strip()
        header = header_for(role, tid)
        prompt = speaker_prompt(meta, role, header, why)
        effort = CONFIG["codex_effort"]["main" if role["type"] == "main" else "gallery"]
        cmd = engine_cmd(role["engine"], role.get("model", ""), prompt, write=True, effort=effort)
        run_cli(cmd, f"{tid}-{role['key']}")

        new = p.read_text(encoding="utf-8")
        body = extract_body(old, new, header)
        # ファイルは進行役が整えて書き直す（既存行は必ず元のまま）
        p.write_text(old if old.endswith("\n") or not old else old + "\n", encoding="utf-8")

        # AIがスレ以外を触っていたら戻す
        if git("rev-parse", "HEAD").stdout.strip() != head_before:
            log("AIが勝手にcommitしたので取り消す")
            git("reset", "--soft", head_before)
        for path in dirty_paths() - before_dirty:
            if not path.startswith(("threads/", "logs/")):
                if git("ls-files", "--error-unmatch", path).returncode == 0:
                    git("checkout", "--", path)
                    log(f"AIが {path} を触ったので戻した")

        if body:
            line = f"{header} {body} <>"
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            return body
        log(f"{role['key']} の書き込みが取れなかった（{attempt + 1}回目）")
    return None


# ---------------------------------------------------------------- 決着

def check_settlement(meta: dict, decision: dict, lines: list[str]) -> None:
    sides_alive = {r["side"] for r in meta["roles"] if r["type"] == "main" and not r.get("out")}
    all_sides = {r["side"] for r in meta["roles"] if r["type"] == "main"}
    if len(all_sides) >= 2 and len(sides_alive) == 1:
        win = next(iter(sides_alive))
        settle(meta, win, "相手側が全員降参")
        return
    if decision.get("settled") and decision.get("winner_side") in all_sides:
        text = "\n".join(lines)
        if any(m in text for m in VERDICT_MARKS + (SURRENDER,)):
            settle(meta, decision["winner_side"], decision.get("result") or "スレの判定")


def settle(meta: dict, side: str, result: str) -> None:
    meta["status"] = "settled"
    meta["winner"] = f"{side}の勝ち"
    meta["result"] = result
    meta["winner_roles"] = [r["key"] for r in meta["roles"] if r["type"] == "main" and r["side"] == side]
    meta["settled_at"] = dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- コマンド

def cmd_new(topic_path: str, prev: str | None = None) -> str:
    topic = read_json(Path(topic_path))
    THREADS.mkdir(exist_ok=True)
    tid = next_thread_id()
    roles = []
    for r in topic["roles"]:
        r = dict(r)
        r.setdefault("type", "main")
        r.setdefault("model", "")
        r["id"] = make_id(tid, r["key"])
        r["out"] = False
        roles.append(r)
    meta = {
        "id": tid,
        "title": topic["title"],
        "theme": topic["theme"],
        "status": "paused",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "roles": roles,
        "prev": prev,
        "next": None,
    }
    host = {"name": "名無しさん", "type": "gallery", "id": make_id(tid, "__host__")}
    opening = topic.get("opening") or topic["theme"]
    first = f"{header_for(host, tid)} {opening.replace(chr(10), '<br>')} <>{topic['title']}"
    dat_path(tid).write_text(first + "\n", encoding="utf-8")
    write_json(meta_path(tid), meta)
    update_index(meta, 1)
    commit(tid, f"{tid} スレ立て: {meta['title']}")
    push(force=True)
    log(f"スレ立て {tid}: {meta['title']}")
    return tid


def roll_over(meta: dict, count: int) -> dict:
    """1000を超えたら次スレを立てて続ける。"""
    part = meta.get("part", 1) + 1
    base = re.sub(r"\s*Part\d+$", "", meta["title"])
    topic = {
        "title": f"{base} Part{part}",
        "theme": meta["theme"],
        "opening": f"前スレ ?t={meta['id']}<br>続き。{meta['theme']}",
        "roles": [{k: v for k, v in r.items() if k not in ("id",)} for r in meta["roles"]],
    }
    tmp = ROOT / "logs" / "_next_topic.json"
    LOGS.mkdir(exist_ok=True)
    write_json(tmp, topic)
    new_id = cmd_new(str(tmp), prev=meta["id"])
    new_meta = read_json(meta_path(new_id))
    for r_new, r_old in zip(new_meta["roles"], meta["roles"]):
        r_new["out"] = r_old.get("out", False)
    new_meta["part"] = part
    write_json(meta_path(new_id), new_meta)
    meta["next"] = new_id
    meta["status"] = "paused"
    write_json(meta_path(meta["id"]), meta)
    update_index(meta, count)
    commit(meta["id"], f"{meta['id']} 1000到達 → 次スレ {new_id}")
    return new_meta


def cmd_run(tid: str, max_posts: int | None) -> None:
    meta = read_json(meta_path(tid))
    if not meta:
        sys.exit(f"スレ {tid} がない")
    if meta["status"] == "settled":
        log(f"{tid} は決着済み: {meta.get('winner')}")
        return
    meta["status"] = "running"
    write_json(meta_path(tid), meta)
    update_index(meta, len(read_lines(tid)))
    commit(tid, f"{tid} 再開")
    push(force=True)

    done = 0
    turn = len(read_lines(tid))
    try:
        while meta["status"] != "settled" and (max_posts is None or done < max_posts):
            lines = read_lines(meta["id"])
            if len(lines) >= CONFIG["max_posts_per_thread"]:
                meta = roll_over(meta, len(lines))
                meta["status"] = "running"
                write_json(meta_path(meta["id"]), meta)
                continue
            decision = pick_next(meta, lines, turn)
            role = next(r for r in meta["roles"] if r["key"] == decision["next"])
            log(f"次: {role['key']}（{display_name(role)} / {role['engine']}:{role.get('model') or '-'}）理由: {decision.get('why')}  進行役: {decision['moderator']}")
            body = write_post(meta, role, decision.get("why", ""))
            turn += 1
            if body is None:
                continue
            done += 1
            no = len(read_lines(meta["id"]))
            log(f">>{no} {display_name(role)}: {body[:80]}")
            if role["type"] == "main" and SURRENDER in body:
                role["out"] = True
                log(f"{role['key']} が降参")
            check_settlement(meta, decision, read_lines(meta["id"]))
            write_json(meta_path(meta["id"]), meta)
            update_index(meta, no)
            commit(meta["id"], f"{meta['id']} >>{no} {display_name(role)} ({role['key']})")
            push()
            if meta["status"] == "settled":
                log(f"決着: {meta['winner']}（{meta['result']}）")
    except KeyboardInterrupt:
        log("中断")
    finally:
        if meta["status"] != "settled":
            meta["status"] = "paused"
        write_json(meta_path(meta["id"]), meta)
        update_index(meta, len(read_lines(meta["id"])))
        commit(meta["id"], f"{meta['id']} {'決着' if meta['status'] == 'settled' else '一時停止'}")
        push(force=True)


def cmd_status(tid: str | None) -> None:
    idx = read_json(INDEX, {"threads": []})
    for t in idx["threads"]:
        if tid and t["id"] != tid:
            continue
        print(f"{t['id']}  {t['status']:<8} {t['count']:>4}レス  {t['title']}  {t.get('winner', '')}")
        if tid:
            meta = read_json(meta_path(tid))
            for r in meta["roles"]:
                print(f"   {r['key']:<12} {display_name(r)} ID:{r['id']} {r['type']} {r.get('side', '')} "
                      f"{r['engine']}:{r.get('model') or '-'} {'降参' if r.get('out') else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description="AIレスバ板の進行役")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("new", help="スレを立てる")
    a.add_argument("topic")
    b = sub.add_parser("run", help="回す")
    b.add_argument("thread")
    b.add_argument("--max", type=int, default=None, help="今回書かせる最大レス数")
    c = sub.add_parser("status", help="状態")
    c.add_argument("thread", nargs="?")
    args = ap.parse_args()
    if args.cmd == "new":
        cmd_new(args.topic)
    elif args.cmd == "run":
        cmd_run(args.thread, args.max)
    else:
        cmd_status(args.thread)


if __name__ == "__main__":
    main()
