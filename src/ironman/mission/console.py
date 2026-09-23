"""A read-only HTML projection of Registry-owned mission state."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
from urllib.parse import quote


def _text(value):
    return escape(str(value) if value is not None else "未记录", quote=True)


def _detail(value):
    return '<pre>' + _text(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'


def _artifact(directory, reference):
    path = Path(reference)
    if not path.is_absolute():
        path = directory / path
    path = path.resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        return '<span class="muted">工件不可用或不在本项目目录内</span>'
    relative = path.relative_to(directory).as_posix()
    url = quote(relative, safe="/")
    label = _text(relative)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return f'<a class="image" href="{url}"><img src="{url}" alt="{label}" loading="lazy"><span>{label}</span></a>'
    return f'<a href="{url}">{label}</a>'


def _workers(directory):
    """Export public execution records; never worker inputs or transcripts."""
    records = []
    public = ("name", "status", "process_status", "worker_pid", "returncode",
              "started_at", "finished_at", "elapsed_seconds", "error", "usage", "usage_reported")
    configuration = ("adapter", "route", "model", "reasoning_effort", "selection_source",
                     "configuration_source", "model_access_verified", "sandbox", "tools",
                     "ephemeral", "timeout_seconds", "max_calls")
    for path in sorted((directory / "workers").glob("*.json")):
        if path.name.endswith((".input.json", ".schema.json")):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A running worker may still be writing its record at export time.
            records.append({"name": path.stem, "status": "记录读取未完成"})
            continue
        if not isinstance(record, dict) or "status" not in record:
            continue
        visible = {key: record[key] for key in public if key in record}
        visible["configuration"] = {key: record["configuration"][key] for key in configuration
                                    if key in record.get("configuration", {})}
        records.append(visible)
    return records


def export_project(store, project_id, state, version):
    """Write status.html and state.json; never write back into the Registry."""
    directory = Path(state["directory"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat()
    workers = _workers(directory)
    decisions = []
    for reference in state.get("decision_refs", []):
        body, revision = store.load(reference)
        plan = body.get("plan", {})
        decisions.append({"id": reference, "version": revision, "reason": plan.get("reason"),
                          "alternatives": plan.get("alternatives", []), "changed": body.get("changed", []),
                          "preserved": body.get("preserved", []), "observed_versions": body.get("observed_versions", {})})
    projection = {"project_id": project_id, "version": version, "exported_at": exported_at,
                  "authority": "Registry; this file is a read-only projection",
                  "state": state, "decisions": decisions, "workers": workers}
    state_path = directory / "state.json"
    state_path.write_text(json.dumps(projection, ensure_ascii=False, indent=2), encoding="utf-8")

    nodes = []
    artifacts = []
    for node in state.get("nodes", []):
        dependencies = []
        for key, label in (("requires_all", "全部依赖"), ("requires_any", "任选依赖"),
                           ("joint_with", "共同观察"), ("conflicts_with", "冲突分支")):
            if node.get(key):
                dependencies.append(label + "：" + ", ".join(node[key]))
        nodes.append('<tr><td><strong>' + _text(node.get("title", node["id"])) + '</strong><small>' + _text(node["id"]) +
                     '</small></td><td>' + _text(node.get("status")) + '</td><td>' + _text(node.get("tool")) +
                     '<small>版本 ' + _text(node.get("version")) + ' · 尝试 ' + _text(node.get("attempt", 0)) +
                     '</small></td><td>' + _text("；".join(dependencies) or "独立分支") +
                     ('<small class="error">' + _text(node["error"]) + '</small>' if node.get("error") else '') + '</td></tr>')
        for reference in node.get("result", {}).get("artifacts", []):
            artifacts.append('<li><span class="artifact-owner">' + _text(node.get("title", node["id"])) +
                             ' · ' + _text(node.get("status")) + '</span>' + _artifact(directory, reference) + '</li>')

    designer = state.get("designer", {})
    guidance = '<p class="muted">首次能力检索的适用理由（历史记录）：</p><p>' + _text(designer.get("selection_reason") or "尚未记录 Designer 选择理由。") + '</p>'
    guidance += '<p class="muted">检索状态：' + _text(designer.get("status") or "未调用") + ' · 建议权威：' + _text(designer.get("authority") or "无") + '</p>'
    for item in designer.get("capabilities", []):
        guidance += '<details><summary>' + _text(item["id"]) + '</summary>' + _detail({
            key: item[key] for key in ("applicability", "applies_when", "limitations", "source_refs") if key in item}) + '</details>'
    if designer.get("missing_evidence"):
        guidance += '<p class="muted">' + _text("；".join(designer["missing_evidence"])) + '</p>'

    decision_html = ''.join('<details><summary>' + _text(decision.get("reason") or decision["id"]) +
                            '</summary>' + _detail(decision) + '</details>' for decision in decisions) or '<p class="muted">尚无决策记录。</p>'
    feedback_html = ''.join('<article class="entry"><small>' + _text(item.get("source_kind")) + ' · ' +
                            _text(", ".join(item.get("scopes", []))) + '</small><p>' + _text(item.get("text")) +
                            '</p><small>' + _text(item.get("source_ref")) + '</small></article>' for item in state.get("feedback", [])) or '<p class="muted">尚无反馈。没有把模型判断记为人类偏好。</p>'
    worker_html = ''.join('<details><summary>' + _text(worker.get("name")) + ' · ' + _text(worker.get("status")) +
                         ' · ' + _text(worker.get("process_status", "未记录进程状态")) + '</summary>' + _detail(worker) +
                         '</details>' for worker in workers) or '<p class="muted">尚无模型调用记录。</p>'
    local_id = project_id.removeprefix("mission:")
    commands = '\n'.join(f'.\\scripts\\jervis.ps1 {action} --project {local_id}' for action in ("resume", "pause", "status"))
    stop = state.get("stop_reason")
    if not stop:
        stop = "未记录停止或等待原因。"
    content = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_text(state.get('title', local_id))} · Jervis</title>
<style>
:root{{color-scheme:light;--ink:#192b37;--muted:#65737d;--line:#dbe2e5;--blue:#205b85}}
*{{box-sizing:border-box}}body{{margin:0;background:#f5f7f7;color:var(--ink);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}}
main{{max-width:1120px;margin:auto;padding:38px 28px 80px}}header{{border-bottom:2px solid var(--ink);padding-bottom:25px}}
.eyebrow,small,.muted{{color:var(--muted)}}.eyebrow{{font-size:12px;letter-spacing:.18em}}h1{{font-size:34px;line-height:1.25;margin:12px 0}}h2{{font-size:21px;margin:0 0 18px}}p{{margin:8px 0 14px;white-space:pre-wrap}}
nav{{display:flex;gap:20px;flex-wrap:wrap;margin-top:22px}}a{{color:var(--blue);text-underline-offset:3px;overflow-wrap:anywhere}}button{{font:inherit;background:white;border:1px solid var(--line);border-radius:4px;padding:5px 12px;cursor:pointer}}
.status{{font-size:17px;font-weight:650}}.facts{{display:flex;gap:28px;flex-wrap:wrap;margin-top:20px}}.facts strong{{display:block}}section{{padding:30px 0;border-bottom:1px solid var(--line)}}.notice{{border-left:3px solid var(--blue);padding:8px 18px;margin-top:22px;background:#ebf1f4}}
.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;text-align:left}}th{{font-size:12px;color:var(--muted);font-weight:500}}th,td{{padding:12px 12px 12px 0;border-bottom:1px solid var(--line);vertical-align:top}}td small{{display:block}}details{{padding:12px 0;border-bottom:1px solid var(--line)}}summary{{cursor:pointer;overflow-wrap:anywhere}}pre{{overflow:auto;background:#edf1f3;padding:16px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}}
.artifacts{{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:22px}}.artifacts li{{min-width:0}}.artifact-owner{{display:block;font-size:12px;color:var(--muted);margin-bottom:7px}}.image{{display:block}}.image img{{width:100%;max-height:260px;object-fit:contain;object-position:left;background:#e9eef0;border:1px solid var(--line)}}.image span{{font-size:12px;display:block}}.entry{{padding:15px 0;border-bottom:1px solid var(--line)}}.error{{color:#a13f32}}footer{{margin-top:25px;font-size:12px;color:var(--muted)}}
@media(max-width:620px){{main{{padding:24px 18px 50px}}h1{{font-size:28px}}.facts{{gap:16px}}th,td{{min-width:110px}}nav{{gap:14px}}}}
</style></head><body><main>
<header><div class="eyebrow">JERVIS · PROJECT RECORD</div><h1>{_text(state.get('title', local_id))}</h1>
<p>{_text(state.get('brief'))}</p><div class="status">当前状态：{_text(state.get('status'))}</div>
<div class="facts"><div>状态版本<strong>{_text(version)}</strong></div><div>模型调用<strong>{_text(state.get('calls',0))} / {_text(state.get('budget',{}).get('max_calls'))}</strong></div><div>执行步数<strong>{_text(state.get('steps',0))} / {_text(state.get('budget',{}).get('max_steps'))}</strong></div></div>
<nav><a href="#artifacts">查看作品</a><a href="#branches">工作分支</a><a href="#decisions">决策与反馈</a><a href="#workers">模型调用</a><a href="state.json">读取状态数据</a><button onclick="location.reload()">刷新页面</button></nav>
<div class="notice"><strong>停止 / 等待原因</strong><p>{_text(stop)}</p></div></header>
<section id="artifacts"><h2>作品与观察证据</h2><ul class="artifacts">{''.join(artifacts) or '<li class="muted">尚无已登记工件。</li>'}</ul></section>
<section id="branches"><h2>工作分支与依赖</h2><div class="table-wrap"><table><thead><tr><th>分支</th><th>状态</th><th>操作 / 版本</th><th>依赖与结果</th></tr></thead><tbody>{''.join(nodes) or '<tr><td colspan="4">尚未形成工作图。</td></tr>'}</tbody></table></div></section>
<section><h2>领域与能力</h2><p>领域：{_text(', '.join(state.get('domains',[])) or '尚未选择')}</p><details><summary>作品所用领域模型及后续更新状态</summary>{_detail({'used_versions':state.get('domain_versions',{}),'models':state.get('domain_models',{}),'subsequent_revisions':state.get('subsequent_domain_revisions',{})})}</details>{guidance}</section>
<section id="decisions"><h2>决策与反馈</h2>{decision_html}<h3>反馈来源</h3>{feedback_html}<details><summary>实际观察、偏好、假设与未知</summary>{_detail({key:state.get(key,[]) for key in ('observations','preferences','hypotheses','unknowns','questions')})}</details></section>
<section id="workers"><h2>模型调用记录</h2><p class="muted">以下为导出时读取的调用配置与进程记录。记录中的 RUNNING 不单独证明进程目前仍存活。</p>{worker_html}</section>
<section><h2>继续操作</h2><p>在项目仓库环境中使用以下命令。此页面只读；刷新会读取最近一次导出的状态。</p><pre>{_text(commands)}</pre></section>
<footer>项目 {_text(project_id)} · 导出 {_text(exported_at)}<br>Registry 是状态权威。本页与 state.json 是可重新生成的展示文件；未提供进度评分或人类偏好推断。</footer>
</main></body></html>'''
    html_path = directory / "status.html"
    html_path.write_text(content, encoding="utf-8")
    return {"html": str(html_path), "state": str(state_path)}
