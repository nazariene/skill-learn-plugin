from datetime import datetime
from html import escape
import json
import re
import shlex
from urllib.parse import quote

from .usage import call_usage as _usage_of, sum_usage as _sum_usage
from .settings import DEFAULT_HOME


STATE_LABELS = {
    "unchanged": "No changes", "applied": "Applied", "approved": "Approved",
    "staged": "Changes proposed", "pending": "Awaiting approval", "rejected": "Rejected",
    "failed": "Failed", "cancelled": "Cancelled", "running": "Reviewing", "queued": "Queued",
    "completed": "Completed", "attempted": "Unfinished", "legacy": "Historical record",
    "interrupted": "Interrupted", "not-reviewed": "Delegate — not reviewed",
}
MODE_LABELS = {"fork": "Full active context", "digest": "Summarized context", "diagnostic": "Cache diagnostic"}
ACTION_LABELS = {"create": "Create skill", "edit": "Rewrite skill", "patch": "Update skill", "write_file": "Write support file", "remove_file": "Remove support file"}
PUBLISHED = {"applied", "approved"}


def render_home(store, skills=(), section="summary"):
    reviews = store.list_reviews()
    proposals = store.list_proposals(include_payload=section == "skills")
    learned = _learned_skills(skills, proposals)
    sections = {"summary": ("Learning overview", "What was learned, what changed, and what needs your attention."),
                "sessions": ("Review sessions", "Follow each session from review outcome to model-call evidence."),
                "skills": ("Learned skills & proposals", "Browse published skills and inspect changes awaiting approval.")}
    section = section if section in sections else "summary"
    title, subtitle = sections[section]
    search = '' if section == "summary" else f'''<label class="search">Filter
      <input type="search" placeholder="{'Search sessions, skills or models' if section == 'sessions' else 'Search skills or changes'}" data-filter="{section}">
      </label>'''
    header = f'''<header class="top"><div><p class="eyebrow">Skill learning</p><h1>{title}</h1>
      <p class="subtitle">{subtitle}</p></div>{search}</header>'''
    session_count = len({(review["harness"], review["session_id"]) for review in reviews})
    nav = _navigation(section, session_count, len(learned))
    home = getattr(store, "home", DEFAULT_HOME)
    if section == "summary":
        content = _summary_tab(reviews, proposals, learned, _sum_usage(store.model_call_usage()))
    elif section == "sessions":
        content = _sessions_tab(reviews, proposals, store.model_call_usage(), home)
    else:
        content = _skills_tab(learned, proposals, reviews)
    note = '<p class="meta">Offline snapshot. Counts are host-normalized; raw provider usage and wire bodies are unavailable. Refresh: <code>' + escape('skill-learn --home ' + shlex.quote(str(home)) + ' report') + '</code></p>'
    return _page(title, header + nav + f'<section data-section="{section}">{content}</section>' + note)


def _navigation(section, session_count=None, skill_count=None):
    links = []
    for key, label, path, count in (("summary", "Summary", "index.html", None), ("sessions", "Review sessions", "sessions.html", session_count),
                                  ("skills", "Skills & proposals", "skills.html", skill_count)):
        active = ' aria-current="page"' if key == section else ''
        amount = f'<span class="count">{count}</span>' if count is not None else ''
        links.append(f'<a class="tab-link" href="{path}"{active}>{escape(label)}{amount}</a>')
    return '<nav class="tabs" aria-label="Dashboard sections">' + "".join(links) + '</nav>'


def _summary_tab(reviews, proposals, learned, totals):
    stats = _stats(reviews, proposals)
    return f"""
      <section class="stats" aria-label="Statistics">
        {_stat("All reviews", len(reviews), "all")}
        {_stat("Failed reviews", stats["failed"], "failed")}
        {_stat("Awaiting approval", stats["proposals"].get("pending", 0), "pending")}
        {_stat("Changes applied", sum(proposal['status'] in PUBLISHED for proposal in proposals), "published")}
      </section>
      <section class="stats" aria-label="Tokens">
        {_token_stat("Input tokens", totals, "input")}
        {_token_stat("Cached input", totals, "hit", _share(totals, "hit"))}
        {_token_stat("Uncached input", totals, "miss", _share(totals, "miss"))}
      </section>
      {_panel("Outcomes & token details", _review_breakdown(stats, totals), "outcomes")}
      {_panel("Recent reviews", _recent_reviews(reviews, proposals), "recent-reviews", open=True)}
      <p class="meta">{_count(len(learned), 'learned or improved skill')} · <a href="skills.html">Browse skills &amp; proposals</a></p>
    """


def _sessions_tab(reviews, proposals, calls, home):
    by_review = {}
    for call in calls:
        by_review.setdefault(call["review_id"], []).append(call)
    return f"""
      {_filters('sessions')}
      {_panel("Reviews", _toolbar("reviews") + _review_sessions(reviews, by_review, proposals, home) + '<p class="empty" id="review-filter-empty" hidden>No sessions match this filter.</p>', "reviews", count=len(reviews), open=True)}
    """


def _skills_tab(learned, proposals, reviews):
    ordered = sorted(proposals, key=lambda proposal: proposal.get("created_at") or "", reverse=True)
    ordered.sort(key=lambda proposal: proposal["status"] != "pending")
    return f"""
      {_filters('skills')}
      {_panel("Learned & improved skills", _skill_cards(learned, reviews) + '<p class="empty" id="skill-filter-empty" hidden>No learned skills match this filter.</p>', "learned-skills", count=len(learned), open=True)}
      {_panel("Proposals & change history", _proposal_blocks(ordered, reviews) + '<p class="empty" id="proposal-filter-empty" hidden>No changes match this filter.</p>', "proposals", count=len(proposals), open=True)}
    """


def _filters(section):
    options = [("All", "all"), ("Applied", "published"), ("Awaiting approval", "pending"), ("Manually approved", "approved"), ("Rejected", "rejected")]
    if section == "sessions":
        options = [("All", "all"), ("With skill changes", "changes"), ("Awaiting approval", "pending"), ("Failed", "failed"),
                   ("Full conversation", "fork"), ("Summarized context", "digest")]
    return '<div class="filter-bar" aria-label="Filter results">' + "".join(
        f'<button type="button" class="filter-chip" data-stat="{kind}" aria-pressed="false">{escape(label)}</button>' for label, kind in options
    ) + '</div>'


def _recent_reviews(reviews, proposals):
    if not reviews:
        return '<p class="empty">No reviews yet. Finished sessions will appear here.</p>'
    latest = sorted(reviews, key=lambda review: review.get("started_at") or review.get("received_at") or "", reverse=True)[:6]
    return '<div class="recent-list">' + "".join(
        f'''<article class="recent-review"><div class="review-heading"><a href="review-{escape(review['id'])}.html">{escape(_session_name(review))}</a>{_pill(review.get('status') or '')}</div>
        <span class="identifier">[{escape(review['session_id'])}]</span>
        <p class="meta">{_time(review.get('started_at') or review.get('received_at'))} · {escape(_harness_name(review.get('harness')))}</p>
        {_skill_change_summary([proposal for proposal in proposals if proposal['review_id'] == review['id']], compact=True)}</article>'''
        for review in latest
    ) + '</div><p><a href="sessions.html">View all review sessions</a></p>'


def render_review(store, review_id, *, lazy_context=False):
    service_review = next((review for review in store.list_reviews() if review["id"] == review_id), None)
    if service_review is None:
        return None
    evidence = store.evidence(review_id)
    proposals = store.proposals_for_review(review_id)
    calls = store.model_calls_for_review(review_id)
    usage = _sum_usage(calls)
    error = service_review.get("error") or ""
    body = f"""
      <header class="top">
        <div>
          <p class="eyebrow"><a href="sessions.html">Review sessions</a></p>
          <h1>{escape(_session_name(service_review))}</h1>
          <p class="identifier">[{escape(service_review.get('session_id') or '')}]</p>
          <p class="meta">
            {_pill(service_review.get("outcome") or service_review.get("status") or "")}
            <span>{escape(MODE_LABELS.get(service_review.get("context_mode"), ""))}</span>
            <span>{escape(_model_name(service_review.get("called_model")))}</span>
            {_time(service_review.get("started_at"))}
            <span>{escape(_duration_text(service_review))}</span>
          </p>
          <p class="identifier">Review [{escape(review_id)}]</p>
        </div>
      </header>
      {_navigation('sessions')}
      {_skill_change_summary(proposals)}
      {_usage_facts(usage)}
      {_panel("Context decision", _context_decision(service_review), "context-decision")}
      {_panel("Requests", _request_compare(store.review_context(review_id), calls, service_review, lazy_context=lazy_context), "requests")}
      {_panel("Error", f"<pre>{escape(error)}</pre>" if error else "<p class='empty'>No error.</p>", "error", open=bool(error))}
      {_panel("Model calls", _toolbar("review-calls") + _call_list(calls), "review-calls", count=len(calls), open=True)}
      {_panel("Evidence", _evidence_blocks(evidence), "evidence", count=len(evidence))}
      {_panel("Proposals", _proposal_blocks(proposals), "review-proposals", count=len(proposals), open=True)}
    """
    return _page(_session_name(service_review), body)


def _token_stat(label, totals, key, share=""):
    note = f"<em>{escape(share)}</em>" if share else ""
    return f"<div class=\"stat static\"><span>{escape(label)}</span><strong>{escape(_amount(totals, key))}</strong>{note}</div>"


def _stat(label, value, kind):
    path = "skills.html" if kind in {"pending", "published", "applied", "approved", "rejected"} else "sessions.html"
    return f'<a class="stat" href="{path}?status={escape(kind)}"><span>{escape(label)}</span><strong>{value}</strong></a>'


def _panel(title, content, panel_id, count=None, open=False):
    count_html = f"<span class=\"count\">{count}</span>" if count is not None else ""
    opened = " open" if open else ""
    return f"""
      <section class="panel" id="{escape(panel_id)}">
        <details{opened}>
          <summary><span class="panel-title"><span class="chevron" aria-hidden="true">›</span>{escape(title)}</span>{count_html}</summary>
          <div class="panel-body">{content}</div>
        </details>
      </section>
    """


def _toolbar(target):
    noun, children = ("sessions", ".session-calls") if target == "reviews" else ("calls", ".model-call")
    return f"""
      <div class="toolbar">
        <button type="button" data-expand="#{escape(target)}" data-children="{children}" data-mode="open">Expand {noun}</button>
        <button type="button" data-expand="#{escape(target)}" data-children="{children}" data-mode="closed">Collapse {noun}</button>
      </div>
    """


def _review_breakdown(stats, totals):
    cards = [
        _stat("Full conversation", stats["fork"], "fork"),
        _stat("Summarized context", stats["digest"], "digest"),
        _stat("Approved", stats["proposals"].get("approved", 0), "approved"),
        _stat("Rejected", stats["proposals"].get("rejected", 0), "rejected"),
        _token_stat("Output tokens", totals, "output"),
        _token_stat("Cache write tokens", totals, "write"),
    ]
    return _outcome_chips(stats["outcomes"]) + '<div class="stats breakdown">' + "".join(cards) + '</div>'


def _table(headers, rows):
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "\n".join(rows) or "<tr><td class=\"empty\" colspan=\"9\">Nothing here.</td></tr>"
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _review_sessions(reviews, by_review, proposals=(), home=DEFAULT_HOME):
    if not reviews:
        return "<p class=\"empty\">No reviews yet. Finished sessions will appear here.</p>"
    grouped = {}
    for review in reviews:
        grouped.setdefault((review.get("harness") or "", review.get("session_id") or ""), []).append(review)
    blocks = []
    sessions = sorted(grouped.items(), key=lambda item: _newest_stamp(item[1]), reverse=True)
    for (harness, session_id), session_reviews in sessions:
        session_reviews = sorted(session_reviews, key=lambda review: review.get("started_at") or review.get("received_at") or "", reverse=True)
        session_calls = [call for review in session_reviews for call in by_review.get(review["id"], [])]
        latest = session_reviews[0]
        review_ids = {review["id"] for review in session_reviews}
        session_proposals = [proposal for proposal in proposals if proposal["review_id"] in review_ids]
        name = _session_name(next((review for review in session_reviews if review.get("session_name")), latest))
        label = f"{name} {harness} / {session_id}"
        summary = f'''<span class="chevron" aria-hidden="true">›</span>
          <span class="session-heading"><strong class="session-name">{escape(name)}</strong>
          <span class="identifier">[{escape(session_id)}]</span>
          <span class="meta">{escape(_harness_name(harness))} · {_count(len(session_reviews), "review")} · {_time(latest.get("started_at") or latest.get("received_at"))}</span>
          {_skill_change_summary(session_proposals, compact=True)}</span>
          <span class="session-metrics">{_pill(latest.get("status") or "")}
          <span class="meta">{_count(len(session_calls), "call")}</span>
          <span class="meta">{escape(_usage_text(_sum_usage(session_calls)))}</span></span>'''
        frames = "\n".join(_review_frame(review, by_review.get(review["id"], []), [proposal for proposal in proposals if proposal["review_id"] == review["id"]]) for review in session_reviews)
        blocks.append(
            f'<details class="session-calls" data-search="{escape(label.lower())}"><summary>{summary}</summary><div class="session-body">{frames}<div class="session-actions">{_session_delete(harness, session_id, home)}</div></div></details>'
        )
    return "\n".join(blocks)


def _session_delete(harness, session_id, home):
    command = "skill-learn --home " + shlex.quote(str(home)) + " delete-session " + shlex.quote(harness) + " " + shlex.quote(session_id)
    return '<details class="fold"><summary>Delete learning records</summary><pre>' + escape(command) + '</pre><p class="meta">Skills and the coding session are preserved. Reconcile an active reviewer first.</p></details>'


def _review_frame(review, calls, proposals=()):
    outcome = review.get("outcome") or review.get("status") or ""
    usage = _sum_usage(calls)
    searchable = " ".join(str(review.get(key) or "") for key in ("id", "harness", "session_id", "session_name", "called_model", "status", "context_mode"))
    searchable += " " + STATE_LABELS.get(outcome, outcome) + " " + MODE_LABELS.get(review.get("context_mode"), "")
    searchable += " " + " ".join(f"{proposal['skill_name']} {proposal['gist']}" for proposal in proposals)
    states = " ".join(sorted({proposal["status"] for proposal in proposals}))
    return f"""
      <article class="review-frame" data-search="{escape(searchable.lower())}" data-review="{escape(outcome)}" data-mode="{escape(review.get('context_mode') or '')}" data-proposal-states="{escape(states)}">
        <header>
           <div class="review-heading"><a class="review-link" href="review-{escape(review['id'])}.html">Review · {_time(review.get('started_at') or review.get('received_at'))}</a>{_pill(outcome)}</div>
          <div class="meta"><span>{escape(_model_name(review.get('called_model')))}</span><span>{escape(MODE_LABELS.get(review.get('context_mode'), ''))}</span><span>{escape(_duration_text(review))}</span><span>{escape(_usage_text(usage))}</span></div>
          <span class="identifier">[{escape(review['id'])}]</span>
        </header>
        {_skill_change_summary(proposals)}
        {_call_list(calls, lazy=True)}
      </article>
    """


def _call_list(calls, lazy=False):
    if not calls:
        return "<p class=\"empty\">No model calls.</p>"
    return "\n".join(_model_call_block(call, lazy=lazy) for call in calls)


def _proposal_blocks(proposals, reviews=()):
    if not proposals:
        return "<p class=\"empty\">No proposals.</p>"
    by_review = {review["id"]: review for review in reviews}
    return "\n".join(_proposal_block(proposal, by_review.get(proposal["review_id"])) for proposal in proposals)


def _proposal_block(proposal, review=None):
    payload = proposal.get("payload") or {}
    searchable = " ".join(str(proposal.get(key) or "") for key in ("id", "skill_name", "action", "status", "gist", "review_id")).lower()
    if review:
        searchable += " " + " ".join(str(review.get(key) or "") for key in ("session_name", "session_id"))
    summary = f"{proposal['skill_name']} · {ACTION_LABELS.get(proposal['action'], proposal['action'])}"
    source = _session_name(review) if review else "Source review"
    return f"""
      <details class="proposal" id="proposal-{escape(proposal['id'])}" data-search="{escape(searchable.lower())}" data-proposal="{escape(proposal['status'])}">
        <summary><span>{escape(summary)} {_pill(proposal['status'])}</span><span class="count">{escape(proposal['gist'])}</span></summary>
        <p class="meta"><a href="review-{escape(proposal['review_id'])}.html">{escape(source)}</a> · {_time(proposal.get('decided_at') or proposal.get('created_at'))}</p>
        <span class="identifier">Review [{escape(proposal['review_id'])}] · Change [{escape(proposal['id'])}]</span>
        {_proposal_body(payload, proposal.get("base_hash"))}
      </details>
    """


def _skill_change_summary(proposals, compact=False):
    groups = {"Applied": {}, "Proposed": {}, "Rejected": {}}
    for proposal in proposals:
        if proposal["status"] in PUBLISHED:
            label = "Applied"
        elif proposal["status"] == "pending":
            label = "Proposed"
        elif proposal["status"] == "rejected":
            label = "Rejected"
        else:
            continue
        groups[label].setdefault(proposal["skill_name"], []).append(proposal)
    rows = []
    for label, skills in groups.items():
        if not skills:
            continue
        links = []
        for name, changes in sorted(skills.items()):
            target = "skill-" + name if label == "Applied" else "proposal-" + changes[-1]["id"]
            link = f'<a class="skill-link" href="skills.html#{quote(target, safe="")}" title="{escape(chr(10).join(change["gist"] for change in changes))}">{escape(name)}</a>'
            if not compact:
                gists = list(dict.fromkeys(change["gist"] for change in changes))
                link += f'<span class="change-gist">{escape("; ".join(gists))}</span>'
            links.append(f'<span class="changed-skill">{link}</span>')
        tag = "span" if compact else "div"
        rows.append(f'<{tag} class="change-group change-{label.lower()}"><strong class="change-label">{label}</strong>' + "".join(links) + f'</{tag}>')
    tag = "span" if compact else "div"
    return f'<{tag} class="skill-changes" aria-label="Skill changes">' + "".join(rows) + f'</{tag}>' if rows else ''


def _learned_skills(skills, proposals):
    catalog = {skill["name"]: skill for skill in skills}
    names = {proposal["skill_name"] for proposal in proposals if proposal["status"] in PUBLISHED}
    names.update(skill["name"] for skill in skills if not skill.get("userOwned", True))
    learned = []
    for name in sorted(names):
        changes = [proposal for proposal in proposals if proposal["skill_name"] == name]
        changes.sort(key=lambda proposal: proposal.get("decided_at") or proposal.get("created_at") or "", reverse=True)
        learned.append({"name": name, "library": catalog.get(name), "changes": changes})
    return learned


def _skill_cards(learned, reviews):
    if not learned:
        return '<p class="empty">No published skills yet. Proposed changes appear below until they are approved.</p>'
    by_review = {review["id"]: review for review in reviews}
    cards = []
    for skill in learned:
        library = skill["library"] or {}
        changes = skill["changes"]
        published = [proposal for proposal in changes if proposal["status"] in PUBLISHED]
        pending = [proposal for proposal in changes if proposal["status"] == "pending"]
        status = "In library" if skill["library"] else "History only"
        if library.get("pinned"):
            status = "Pinned"
        elif library.get("protection"):
            status = str(library["protection"]).capitalize()
        elif library.get("userOwned"):
            status = "User owned"
        description = library.get("description") or "Published skill changes recorded by the service."
        searchable = skill["name"] + " " + description + " " + " ".join(proposal["gist"] for proposal in changes)
        searchable += " " + " ".join(_session_name(by_review[proposal["review_id"]]) for proposal in changes if proposal["review_id"] in by_review)
        history = '<p class="empty">No retained review history for this library skill.</p>'
        if changes:
            history = '<ul class="skill-history">' + "".join(
                f'''<li>{_pill(proposal['status'])} <a href="#proposal-{escape(proposal['id'])}">{escape(proposal['gist'])}</a>
                <p class="meta"><a href="review-{escape(proposal['review_id'])}.html">{escape(_session_name(by_review[proposal['review_id']]) if proposal['review_id'] in by_review else 'Source review')}</a> · {_time(proposal.get('decided_at') or proposal.get('created_at'))}</p></li>'''
                for proposal in changes
            ) + '</ul>'
        states = {proposal["status"] for proposal in changes}
        if skill["library"] and not library.get("userOwned", True):
            states.add("applied")
        states = " ".join(sorted(states))
        cards.append(f'''<article class="learned-skill" id="skill-{escape(skill['name'])}" data-skill data-search="{escape(searchable.lower())}" data-skill-states="{escape(states)}">
          <header class="skill-card-heading"><h3>{escape(skill['name'])}</h3><span class="pill">{escape(status)}</span></header>
          <p class="skill-description">{escape(description)}</p>
          <p class="meta">{_count(len(published), 'published change')}{f' · {len(pending)} awaiting approval' if pending else ''}</p>
          {_fold('Review & change history', history)}</article>''')
    return '<div class="skill-grid">' + "".join(cards) + '</div>'


def _proposal_body(payload, base_hash):
    parts = []
    file_path = payload.get("file_path") or ("SKILL.md" if payload.get("action") in {"create", "edit", "patch"} else "")
    if file_path:
        parts.append(f"<p class=\"meta\">File: {escape(str(file_path))}</p>")
    if payload.get("old_string") is not None or payload.get("new_string") is not None:
        parts.append(_fold("Before", f"<pre>{escape(payload.get('old_string') or '')}</pre>"))
        parts.append(_fold("After", f"<pre>{escape(payload.get('new_string') or '')}</pre>"))
    content = payload.get("content")
    if isinstance(content, str):
        parts.append(_fold("Content", f"<pre>{escape(content)}</pre>"))
    file_content = payload.get("file_content")
    if isinstance(file_content, str):
        parts.append(_fold("File content", f"<pre>{escape(file_content)}</pre>"))
    if base_hash:
        parts.append(f"<p class=\"meta\">Base hash: {escape(base_hash)}</p>")
    if not parts:
        parts.append(f"<pre>{escape(_pretty(payload))}</pre>")
    return "\n".join(parts)


def _outcome_chips(outcomes):
    if not outcomes:
        return "<p class=\"empty\">No outcomes yet.</p>"
    return "<div class=\"chips\">" + "".join(
        f"<span class=\"chip\">{escape(STATE_LABELS.get(name, name))} <strong>{count}</strong></span>"
        for name, count in sorted(outcomes.items())
    ) + "</div>"


def _newest_stamp(reviews):
    return max((review.get("started_at") or review.get("received_at") or "" for review in reviews), default="")


def _session_name(review):
    return review.get("session_name") or ("Service cache diagnostic" if review.get("harness") == "cache-probe" else "Untitled session")


def _harness_name(harness):
    return {"opencode": "OpenCode", "cursor": "Cursor", "cache-probe": "Diagnostics"}.get(harness, harness or "Unknown client")


def _model_name(model):
    return (model or "Model not recorded").removeprefix("openai/")


def _count(amount, noun):
    return f"{amount:,} {noun}{'' if amount == 1 else 's'}"


def _time(stamp):
    if not stamp:
        return "Not started"
    try:
        date = datetime.fromisoformat(stamp).astimezone()
        label = f"{date.strftime('%b')} {date.day}, {date.year} · {date.strftime('%H:%M')}"
    except (ValueError, TypeError):
        return escape(str(stamp))
    return f'<time datetime="{escape(stamp)}" title="{escape(stamp)}" data-local-time>{escape(label)}</time>'


def _session_stamp(reviews):
    stamps = sorted(review.get("started_at") for review in reviews if review.get("started_at"))
    if not stamps:
        return ""
    if len(stamps) == 1:
        return stamps[0]
    return f"{stamps[0]} – {stamps[-1]}"


def _duration_text(review):
    started, finished = review.get("started_at"), review.get("finished_at")
    if not started or not finished:
        return ""
    return _duration(_seconds(started, finished))


def _duration(seconds):
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _evidence_blocks(evidence):
    if not evidence:
        return "<p class=\"empty\">No evidence.</p>"
    blocks = []
    for item in evidence:
        blocks.append(
            "<details class=\"evidence\">"
            f"<summary>{escape(item['kind'])}</summary>"
            f"<pre>{escape(_pretty(item['payload']))}</pre>"
            "</details>"
        )
    return "\n".join(blocks)


def _stats(reviews, proposals):
    outcomes = {}
    for review in reviews:
        outcome = review.get("outcome") or review.get("status") or "unknown"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    proposal_counts = {}
    for proposal in proposals:
        proposal_counts[proposal["status"]] = proposal_counts.get(proposal["status"], 0) + 1
    return {
        "outcomes": outcomes,
        "fork": sum(1 for review in reviews if review.get("context_mode") == "fork"),
        "digest": sum(1 for review in reviews if review.get("context_mode") == "digest"),
        "failed": sum(1 for review in reviews if review.get("status") == "failed"),
        "proposals": proposal_counts,
    }


def _seconds(started, finished):
    return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()


def _token_text(value):
    return "unavailable" if value is None else str(value)


def _pill(value):
    text = value or ""
    kind = escape(text.lower().replace(" ", "-"))
    return f"<span class=\"pill pill-{kind}\" title=\"{escape(text)}\">{escape(STATE_LABELS.get(text, text.replace('_', ' ').capitalize()))}</span>"


def _request_compare(context, calls, review, *, lazy_context=False):
    context = context or {}
    submitted = {"model": context.get("model"), "reasoning": context.get("reasoning"), "system": context.get("system_prompt"),
                 "messages": context.get("messages") or [], "tools": context.get("tools") or []}
    first_call = calls[0] if calls else {}
    wire_body = first_call.get("wire_body")
    sent = json.loads(wire_body) if wire_body is not None else first_call.get("payload")
    label = "Final wire request" if wire_body is not None else "Application arguments (wire request unavailable)"
    note = f"Submitted {len(submitted['messages'])} messages. First recorded request contains {len((sent or {}).get('input') or (sent or {}).get('messages') or [])} input items/messages. Evidence: {first_call.get('evidence_kind') or 'unavailable'}."
    sent_html = _request_view(sent) if sent else '<p class="empty">No model call was stored.</p>'
    comparison = f'<p class="meta">{escape(note)}</p><div class="compare"><section class="request-frame"><h3>Submitted</h3>{_request_view(submitted, lazy_context=lazy_context)}</section><section class="request-frame"><h3>{label}</h3>{sent_html}</section></div>'
    plan = json.loads(review.get("plan_json") or "null")
    parent = _context_fold("Parent submission (complete raw host parts)", "parent") if lazy_context else _fold("Parent submission (complete raw host parts)", '<pre>' + escape(_pretty(context)) + '</pre>')
    content = comparison + '<p class="meta">Host-requested/observed context; final HTTP wire bodies and provider response IDs are unavailable. Skill bodies are an instance snapshot until restart.</p>' + parent + _fold("Native review plan", '<pre>' + escape(_pretty(plan)) + '</pre>')
    if lazy_context:
        return f'<div data-context-id="{escape(review["id"])}" data-context-url="context-{escape(review["id"])}.js">{content}</div>'
    return content


def _context_decision(review):
    decision = json.loads(review.get("decision_json") or "null") or {}
    decision = {"fork_input_tokens": decision.get("forkInputTokens"), "fork_input_method": decision.get("method"), **decision}
    labels = (
        ("Decision reason", "reason"), ("Replay eligible (not a cache hit)", "replay_eligible"),
        ("Parent reconstruction fidelity", "fidelity"), ("Known limitations", "limitations"),
        ("Missing tool definitions", "missing_tools"), ("Capture version", "capture_version"), ("Capture turn", "capture_turn_id"),
        ("Assessed initial fork tokens", "fork_input_tokens"), ("Assessment method", "fork_input_method"),
    )
    facts = "".join(
        f"<div><dt>{label}</dt><dd>{escape(str(decision[key]) if decision.get(key) is not None else 'unavailable')}</dd></div>"
        for label, key in labels
    )
    return f'<dl class="facts">{facts}</dl>'


def _model_call_block(call, lazy=False):
    status = call.get("call_status") or "legacy"
    usage = _sum_usage([call])
    timing = _duration(call.get("duration_seconds"))
    summary = f'<span class="chevron" aria-hidden="true">›</span><span class="call-heading"><strong>Call {call["call_index"]}</strong>{_pill(status)}</span><span class="meta">{escape(timing + " · " if timing else "")}{escape(_usage_text(usage))}</span>'
    attributes = f' data-call-url="call-{call["id"]}.js"' if lazy else ''
    if lazy:
        content = f'''<div class="call-evidence" data-call-content aria-live="polite">
          <p class="meta">Request and response evidence loads when this call is expanded.</p>
          <a class="meta" href="review-{escape(call['review_id'])}.html#call-{call['id']}">View in review detail</a>
        </div>'''
    else:
        content = _model_call_evidence(call)
    return f'''<details class="model-call" id="call-{call['id']}"{attributes}>
      <summary>{summary}</summary>{content}</details>'''


def _model_call_evidence(call):
    wire_body = call.get("wire_body")
    payload = json.loads(wire_body) if wire_body is not None else call["payload"]
    response = call.get("response") or {}
    kind = call.get("evidence_kind") or "legacy_pre_serialization"
    usage = _sum_usage([call])
    request_label = "Final wire request" if wire_body is not None else "Application arguments (wire request unavailable)"
    metadata = {key: call.get(key) for key in ("endpoint", "account_scope", "request_headers", "response_headers", "http_status", "response_id", "duration_seconds")}
    return f"""
        {_usage_facts(usage)}
        <div class="call-grid">
          <section>
            <h3>{request_label}</h3>
             <p class="meta">Evidence: {escape(kind)}. Usage: host-normalized; raw provider counters/availability unavailable. Call {call['call_index']}: {'initial parent-to-review observation' if call['call_index'] == 1 else 'within-review continuation'}; cache counts alone do not attribute reused history.</p>
            {_request_view(payload)}
            {_fold("Serialized HTTP body", f"<pre>{escape(wire_body)}</pre>") if wire_body is not None else ""}
            {_fold("Transport metadata", f"<pre>{escape(_pretty(metadata))}</pre>")}
          </section>
          <section>
            <h3>Response</h3>
            {_response_view(response)}
            {_fold("Raw provider response", f"<pre>{escape(call['raw_response'])}</pre>") if call.get("raw_response") is not None else ""}
          </section>
        </div>
    """


def _request_view(payload, *, lazy_context=False):
    parts = ["<dl class=\"facts inline\">"]
    for label, key in (("Model", "model"), ("Reasoning", "reasoning"), ("Temperature", "temperature")):
        parts.append(f"<div><dt>{label}</dt><dd>{escape(_token_text(payload.get(key)) if key == 'temperature' else str(payload.get(key) or ''))}</dd></div>")
    parts.append("</dl>")
    system = payload.get("instructions") or payload.get("system")
    if system:
        parts.append(_fold("System / instructions", f"<pre>{escape(system)}</pre>"))
    for index, message in enumerate(payload.get("messages") or []):
        parts.append(_message_view(message, context_index=index if lazy_context else None))
    for item in payload.get("input") or []:
        parts.append(_fold(str(item.get("type") or item.get("role") or "input"), f"<pre>{escape(_pretty(item))}</pre>"))
    tools = payload.get("tools") or []
    if tools:
        names = ", ".join(_tool_name(tool) for tool in tools)
        parts.append(_fold(f"Tools ({len(tools)}) · {names}", f"<pre>{escape(_pretty(tools))}</pre>"))
    parts.append(_context_fold("Complete request fields", "request") if lazy_context else _fold("Complete request fields", f"<pre>{escape(_pretty(payload))}</pre>"))
    return "\n".join(parts)


def _fold(title, content):
    return f"<details class=\"fold\"><summary>{escape(title)}</summary>{content}</details>"


def _context_fold(title, field):
    return f'<details class="fold" data-context-field="{escape(field)}"><summary>{escape(title)}</summary><div data-context-content aria-live="polite"><p class="meta">Stored context loads when expanded.</p></div></details>'


def _message_view(message, *, context_index=None):
    if not isinstance(message, dict):
        if context_index is not None:
            return _context_fold(_preview(message), f"message:{context_index}")
        return f"<pre>{escape(str(message))}</pre>"
    role = str(message.get("role") or (message.get("info") or {}).get("role") or "message")
    content = message.get("content", message.get("parts"))
    marker = " · compaction" if message.get("compaction") else ""
    calls = f" · {len(message['tool_calls'])} tools" if message.get("tool_calls") else ""
    title = f"{role}{marker}{calls} · {_preview(content)}"
    if context_index is not None:
        return _context_fold(title, f"message:{context_index}")
    body = escape(content) if isinstance(content, str) else escape(_pretty(content))
    extra = f"<pre>{escape(_pretty(message['tool_calls']))}</pre>" if message.get("tool_calls") else ""
    return _fold(title, f"<pre>{body}</pre>{extra}")


def _preview(content):
    if isinstance(content, str):
        chunks = (content,)
    elif content is None:
        chunks = ()
    else:
        chunks = json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(content)
    line, space = "", False
    for chunk in chunks:
        for token in re.finditer(r"\s+|\S{1,73}", chunk):
            start, end = token.span()
            if chunk[start].isspace():
                space = bool(line)
                continue
            if space:
                line += " "
            space = False
            line += chunk[start:min(end, start + 73 - len(line))]
            if len(line) > 72:
                return line[:72].rstrip() + "…"
    return line or "(empty)"


def _tool_name(tool):
    if not isinstance(tool, dict):
        return "?"
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(tool.get("name") or "?")


def _response_view(response):
    if response.get("error"):
        return f"<pre class=\"error-text\">{escape(str(response['error']))}</pre>"
    if not response:
        return '<p class="empty">No response recorded yet.</p>'
    content = response.get("content")
    text = escape(content) if isinstance(content, str) else escape(_pretty(content))
    calls = _fold("Tool calls", f"<pre>{escape(_pretty(response['tool_calls']))}</pre>") if response.get("tool_calls") else ""
    usage = _fold("Provider usage details", f"<pre>{escape(_pretty(response['usage']))}</pre>") if response.get("usage") is not None else ""
    return f"<pre>{text}</pre>{calls}{usage}" + _fold("Complete host response fields", '<pre>' + escape(_pretty(response)) + '</pre>')


def _usage_text(total):
    parts = []
    if total["known"]["input"]:
        parts.append(f"{_amount(total, 'input')} input tokens")
    if total["known"]["hit"]:
        if total["partial"]["hit"] or total["partial"]["miss"]:
            parts.append("Cache usage partially reported")
        elif total["known"]["miss"]:
            parts.append(f"{_share(total, 'hit')} cached")
        else:
            parts.append(f"{_amount(total, 'hit')} cached tokens")
    return " · ".join(parts) or "Usage not reported"


def _usage_facts(usage):
    return '<section class="stats token-facts" aria-label="Token usage">' + "".join(
        _token_stat(label, usage, key, _share(usage, key) if key in {"hit", "miss"} else "")
         for label, key in (("Input tokens", "input"), ("Output tokens", "output"), ("Cache hit tokens", "hit"), ("Cache miss tokens", "miss"), ("Cache write tokens", "write"))
    ) + '</section>'


def _share(total, key):
    if not total["known"]["hit"] or not total["known"]["miss"]:
        return ""
    if total["partial"]["hit"] or total["partial"]["miss"]:
        return "unavailable (partial usage)"
    whole = total["hit"] + total["miss"]
    if whole == 0:
        return "0%"
    return f"{round(100 * total[key] / whole)}%"


def _amount(total, key):
    if not total["known"][key]:
        return "unavailable"
    suffix = " (partial)" if total["partial"][key] else ""
    return f"{total[key]:,}{suffix}"


def _pretty(value):
    # Indented JSON uses Python's recursive encoder; large transcripts need the fast C encoder.
    compact = json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, indent=2) if len(compact) <= 65536 else compact


def _page(title, body):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f4f6f9;
    --panel: #ffffff;
    --panel-2: #f0f3f8;
    --line: #dce3ec;
    --text: #1b293c;
    --muted: #5c6c80;
    --accent: #3456b5;
    --good: #167048;
    --warn: #805a09;
    --bad: #b73838;
    --code: #f4f6f9;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ color-scheme: dark; --bg: #111820; --panel: #1a2430; --panel-2: #222f3e; --line: #344355; --text: #edf2f8; --muted: #a4b3c5; --accent: #a0beff; --good: #71d6a9; --warn: #ebcd82; --bad: #ffa1a1; --code: #141d27; }}
  }}
  * {{ box-sizing: border-box; }}
  [hidden] {{ display: none !important; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  :focus-visible {{ outline: 3px solid var(--accent); outline-offset: 3px; }}
  .wrap {{ max-width: 1240px; margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }}
  .top {{ display: flex; justify-content: space-between; gap: 1.5rem; align-items: end; margin-bottom: 1.8rem; }}
  .eyebrow {{ margin: 0; color: var(--muted); font-size: 0.8rem; letter-spacing: 0.04em; text-transform: uppercase; }}
  h1 {{ margin: 0.15rem 0; font-size: clamp(1.6rem, 3vw, 2.2rem); line-height: 1.3; letter-spacing: -0.03em; overflow-wrap: anywhere; }}
  h3 {{ font-size: 0.95rem; }}
  .subtitle {{ margin: 0.5rem 0 0; color: var(--muted); max-width: 42rem; }}
  .tabs {{ display: flex; flex-wrap: wrap; gap: 0.4rem; border-bottom: 1px solid var(--line); padding-bottom: 0.65rem; margin-bottom: 1.5rem; }}
  .tab-link {{ display: inline-flex; gap: 0.6rem; align-items: center; padding: 0.55rem 0.9rem; border-radius: 9px; font-weight: 600; color: var(--muted); }}
  .tab-link:hover, .tab-link[aria-current="page"] {{ background: var(--panel); color: var(--accent); text-decoration: none; }}
  .tab-link[aria-current="page"] {{ box-shadow: inset 0 -2px var(--accent); }}
  .filter-bar {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }}
  .filter-chip.active {{ border-color: var(--accent); color: var(--accent); background: var(--panel); }}
  .meta {{ display: flex; gap: 0.25rem 0.8rem; align-items: center; color: var(--muted); flex-wrap: wrap; font-size: 0.82rem; font-weight: 400; }}
  .identifier {{ display: block; color: var(--muted); font: 0.73rem/1.6 ui-monospace, monospace; overflow-wrap: anywhere; font-weight: 400; }}
  .search {{ color: var(--muted); font-size: 0.85rem; display: grid; gap: 0.25rem; }}
  input[type="search"] {{ background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 10px; padding: 0.65rem 0.9rem; width: 280px; max-width: 100%; font: inherit; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0.8rem; margin-bottom: 1rem; }}
  .stat {{ text-align: left; background: var(--panel); color: inherit; border: 1px solid var(--line); border-radius: 12px; padding: 0.85rem 1rem; cursor: pointer; font-variant-numeric: tabular-nums; }}
  .stat span {{ display: block; color: var(--muted); font-size: 0.78rem; }}
  .stat strong {{ font-size: 1.5rem; display: block; line-height: 1.5; font-weight: 650; }}
  .stat em {{ color: var(--muted); font-style: normal; font-size: 0.8rem; }}
  .stat.static {{ cursor: default; }}
  a.stat:hover {{ text-decoration: none; border-color: var(--accent); }}
  .stat.active {{ border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }}
  .breakdown {{ margin: 1rem 0 0; }}
  .token-facts .stat {{ background: var(--panel-2); border: 0; }}
  .token-facts .stat strong {{ font-size: 1.1rem; }}
  .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; margin: 1rem 0; }}
  .panel > details > summary {{ list-style: none; cursor: pointer; display: flex; justify-content: space-between; align-items: center; padding: 0.85rem 1rem; font-weight: 650; }}
  .panel-title {{ display: inline-flex; gap: 0.6rem; align-items: center; }}
  .panel > details > summary::-webkit-details-marker, .session-calls > summary::-webkit-details-marker {{ display: none; }}
  .chevron {{ display: inline-block; color: var(--muted); font-size: 1.3rem; line-height: 1; flex: 0 0 auto; }}
  details[open] > summary > .chevron, details[open] > summary > .panel-title > .chevron {{ transform: rotate(90deg); }}
  .panel-body {{ padding: 0 1rem 1rem; }}
  .count {{ color: var(--muted); font-weight: 500; font-size: 0.85rem; }}
  .toolbar {{ display: flex; justify-content: flex-end; gap: 0.4rem; margin-bottom: 0.6rem; }}
  button {{ background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 0.4rem 0.75rem; cursor: pointer; font: inherit; font-size: 0.82rem; }}
  button:hover {{ border-color: var(--accent); }}
  .table-wrap {{ overflow: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
  th {{ text-align: left; color: var(--muted); font-weight: 600; font-size: 0.75rem; letter-spacing: 0.03em; text-transform: uppercase; }}
  th, td {{ padding: 0.55rem 0.4rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  tr[hidden] {{ display: none; }}
  .pill {{ display: inline-block; border-radius: 999px; padding: 0.15rem 0.65rem; background: var(--panel-2); border: 1px solid var(--line); font-size: 0.74rem; font-weight: 600; white-space: nowrap; }}
  .pill-applied, .pill-approved, .pill-completed {{ color: var(--good); }}
  .pill-unchanged, .pill-cancelled, .pill-legacy {{ color: var(--muted); }}
  .pill-failed, .pill-rejected {{ color: var(--bad); }}
  .pill-staged, .pill-pending, .pill-running {{ color: var(--warn); }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 0.4rem; }}
  .chip {{ background: var(--panel-2); border-radius: 999px; padding: 0.25rem 0.65rem; }}
  .empty {{ color: var(--muted); padding: 0.5rem 0; }}
  details.session-calls {{ border: 1px solid var(--line); border-radius: 12px; margin: 0.85rem 0; background: var(--panel); }}
  details.session-calls > summary {{ display: flex; list-style: none; align-items: center; gap: 1rem; padding: 1.05rem; }}
  details.session-calls > summary:hover {{ background: var(--panel-2); border-radius: 12px; }}
  .session-heading {{ display: grid; gap: 0.2rem; flex: 1; min-width: 0; }}
  .session-name {{ font-size: 1.05rem; line-height: 1.4; overflow-wrap: anywhere; }}
  .session-metrics {{ display: grid; justify-items: end; gap: 0.35rem; text-align: right; }}
  .session-actions {{ display: flex; justify-content: flex-end; margin-top: 1rem; }}
  .session-actions form {{ margin: 0; }}
  details.model-call > summary, details.fold > summary, details.evidence > summary {{ cursor: pointer; padding: 0.65rem 0.8rem; overflow-wrap: anywhere; }}
  details.session-calls button {{ color: var(--bad); }}
  .review-frame {{ border-left: 3px solid var(--line); margin: 1rem 0; padding: 0.2rem 0 0.2rem 1rem; min-width: 0; }}
  .review-frame header {{ display: grid; gap: 0.3rem; padding: 0.35rem 0 0.75rem; }}
  .review-heading, .call-heading {{ display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }}
  .call-heading {{ flex: 1; }}
  .review-link {{ font-weight: 600; }}
  .skill-changes {{ display: grid; gap: 0.5rem; margin: 0.6rem 0; min-width: 0; }}
  .change-group {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.4rem 0.75rem; min-width: 0; }}
  .change-label {{ font-size: 0.78rem; }}
  .change-applied .change-label {{ color: var(--good); }}
  .change-proposed .change-label {{ color: var(--warn); }}
  .change-rejected .change-label {{ color: var(--bad); }}
  .changed-skill {{ display: inline-flex; flex-direction: column; align-items: start; max-width: 100%; }}
  .skill-link {{ background: var(--panel-2); border: 1px solid var(--line); border-radius: 7px; padding: 0.1rem 0.45rem; font-size: 0.8rem; overflow-wrap: anywhere; }}
  .change-gist {{ color: var(--muted); font-size: 0.82rem; overflow-wrap: anywhere; }}
  .recent-list {{ display: grid; gap: 0.7rem; }}
  .recent-review {{ padding: 0.75rem 0; border-bottom: 1px solid var(--line); }}
  .recent-review .review-heading > a {{ font-weight: 600; overflow-wrap: anywhere; }}
  .recent-review .meta {{ margin: 0.2rem 0; }}
  .skill-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.8rem; }}
  .learned-skill {{ border: 1px solid var(--line); border-radius: 12px; padding: 1rem; min-width: 0; }}
  .skill-card-heading {{ display: flex; justify-content: space-between; flex-wrap: wrap; align-items: center; gap: 0.5rem; }}
  .skill-card-heading h3 {{ margin: 0; color: var(--accent); overflow-wrap: anywhere; }}
  .skill-description {{ font-size: 0.88rem; overflow-wrap: anywhere; }}
  .skill-history {{ list-style: none; padding: 0; font-size: 0.85rem; }}
  .skill-history li {{ padding: 0.6rem; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }}
  .learned-skill:target, .proposal:target {{ border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent); scroll-margin-top: 1rem; }}
  details.model-call {{ border: 1px solid var(--line); border-radius: 9px; margin: 0.6rem 0; background: var(--panel); }}
  details.model-call > summary {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 0.5rem 1rem; }}
  details.fold, details.evidence {{ background: var(--panel-2); border-radius: 10px; margin: 0.45rem 0; }}
  details.proposal {{ border: 1px solid var(--line); border-radius: 10px; margin: 0.75rem 0; }}
  details.proposal > summary {{ cursor: pointer; padding: 0.85rem; overflow-wrap: anywhere; }}
  details.proposal > :not(summary) {{ margin: 0.75rem; }}
  details.session-calls > :not(summary), details.model-call > :not(summary), details.fold > :not(summary), details.evidence > :not(summary) {{ margin: 0 0.8rem 0.8rem; }}
  .call-grid, .compare {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1rem; }}
  .call-grid > section {{ min-width: 0; }}
  .request-frame {{ border: 1px solid var(--line); border-radius: 10px; padding: 0.55rem 0.7rem 0.7rem; min-width: 0; }}
  .request-frame h3 {{ margin: 0.2rem 0 0.6rem; font-size: 0.95rem; }}
  @media (max-width: 900px) {{ .call-grid, .compare {{ grid-template-columns: 1fr; }} }}
  pre {{ white-space: pre-wrap; overflow-wrap: anywhere; overflow: auto; max-height: 28rem; background: var(--code); border-radius: 8px; padding: 0.9rem; margin: 0.4rem 0; font: 0.78rem/1.65 ui-monospace, monospace; tab-size: 2; }}
  .facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.6rem; margin-bottom: 0.8rem; }}
  .facts div, .facts.inline div {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 0.6rem 0.75rem; }}
  .facts span, .facts dt {{ color: var(--muted); font-size: 0.75rem; }}
   .facts strong, .facts dd {{ display: block; margin: 0.15rem 0 0; overflow-wrap: anywhere; }}
   code, .session-actions {{ overflow-wrap: anywhere; min-width: 0; }}
  .error-text {{ color: var(--bad); }}
  @media (max-width: 700px) {{
    .wrap {{ padding: 1.25rem 0.75rem 3rem; }}
    .top {{ display: grid; }}
    input[type="search"] {{ width: 100%; }}
    .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .stat {{ padding: 0.75rem; }}
    .stat strong {{ font-size: 1.2rem; overflow-wrap: anywhere; }}
    details.session-calls > summary {{ flex-wrap: wrap; gap: 0.6rem; }}
    .session-heading {{ flex-basis: calc(100% - 2rem); }}
    .session-metrics {{ width: 100%; justify-items: start; text-align: left; padding-left: 1.5rem; }}
    .review-frame {{ padding-left: 0.65rem; }}
    .panel-body {{ padding: 0 0.65rem 0.65rem; }}
    .skill-grid {{ grid-template-columns: minmax(0, 1fr); }}
    .tab-link {{ padding: 0.5rem 0.6rem; font-size: 0.88rem; }}
  }}
</style>
</head>
<body><main class="wrap">{body}</main>
<script>
  document.querySelectorAll("[data-local-time]").forEach((node) => {{
    const date = new Date(node.dateTime);
    if (!Number.isNaN(date.getTime())) node.textContent = new Intl.DateTimeFormat(undefined, {{ year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }}).format(date);
  }});
  const filter = document.querySelector("[data-filter]");
  const parameters = new URLSearchParams(window.location.search);
  const filterKinds = [...document.querySelectorAll("[data-stat]")].map(node => node.dataset.stat);
  let activeKind = filterKinds.includes(parameters.get("status")) ? parameters.get("status") : "all";
  if (filter) filter.value = parameters.get("q") || "";
  function matchesStatus(states) {{
    if (activeKind === "published") return states.includes("applied") || states.includes("approved");
    return activeKind === "all" || states.includes(activeKind);
  }}
  function updateFilterButtons() {{
    document.querySelectorAll("[data-stat]").forEach((button) => {{
      const selected = button.dataset.stat === activeKind;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    }});
  }}
  function saveFilters() {{
    const url = new URL(window.location.href);
    if (activeKind === "all") url.searchParams.delete("status");
    else url.searchParams.set("status", activeKind);
    if (filter && filter.value.trim()) url.searchParams.set("q", filter.value.trim());
    else url.searchParams.delete("q");
    window.history.replaceState(null, "", url);
  }}
  function applyFilters() {{
    const query = filter ? filter.value.trim().toLowerCase() : "";
    let visibleSessions = 0;
    document.querySelectorAll(".session-calls").forEach((session) => {{
      const sessionMatch = !query || session.dataset.search.includes(query);
      let visible = false;
      session.querySelectorAll("[data-review]").forEach((review) => {{
        let statusMatch = true;
        if (["fork", "digest"].includes(activeKind)) statusMatch = review.dataset.mode === activeKind;
        else if (activeKind === "failed") statusMatch = review.dataset.review === "failed";
        else if (activeKind === "changes") statusMatch = review.dataset.proposalStates.trim() !== "";
        else statusMatch = matchesStatus(review.dataset.proposalStates.split(" "));
        review.hidden = !((sessionMatch || review.dataset.search.includes(query)) && statusMatch);
        visible = visible || !review.hidden;
      }});
      session.hidden = !visible;
      if (visible) visibleSessions++;
      if (visible && (query || activeKind !== "all")) session.open = true;
    }});
    const empty = document.querySelector("#review-filter-empty");
    if (empty) empty.hidden = visibleSessions > 0 || (!query && activeKind === "all");
    let visibleSkills = 0;
    document.querySelectorAll("[data-skill]").forEach((skill) => {{
      const statusMatch = matchesStatus(skill.dataset.skillStates.split(" "));
      skill.hidden = !((!query || skill.dataset.search.includes(query)) && statusMatch);
      if (!skill.hidden) visibleSkills++;
    }});
    const skillEmpty = document.querySelector("#skill-filter-empty");
    if (skillEmpty) skillEmpty.hidden = visibleSkills > 0 || (!query && activeKind === "all");
    let visibleProposals = 0;
    document.querySelectorAll("[data-proposal]").forEach((proposal) => {{
      const statusMatch = matchesStatus([proposal.dataset.proposal]);
      proposal.hidden = !((!query || proposal.dataset.search.includes(query)) && statusMatch);
      if (!proposal.hidden) visibleProposals++;
      if (!proposal.hidden && (query || activeKind !== "all")) proposal.closest(".panel").querySelector("details").open = true;
    }});
    const proposalEmpty = document.querySelector("#proposal-filter-empty");
    if (proposalEmpty) proposalEmpty.hidden = visibleProposals > 0 || (!query && activeKind === "all");
    updateFilterButtons();
  }}
  if (filter) filter.addEventListener("input", () => {{ applyFilters(); saveFilters(); }});
  document.querySelectorAll("[data-expand]").forEach((button) => {{
    button.addEventListener("click", () => {{
      const root = document.querySelector(button.dataset.expand);
      if (!root) return;
      const groups = root.querySelectorAll(button.dataset.children);
      groups.forEach((node) => {{ if (!node.hidden) node.open = button.dataset.mode === "open"; }});
    }});
  }});
  document.querySelectorAll("[data-stat]").forEach((card) => {{
    card.addEventListener("click", () => {{
      activeKind = activeKind === card.dataset.stat ? "all" : card.dataset.stat;
      applyFilters();
      saveFilters();
    }});
  }});
  document.addEventListener("skill-learn-call-evidence", (event) => {{
    const call = document.getElementById(event.detail.id);
    if (!call || call.dataset.loading !== "true") return;
    call.querySelector("[data-call-content]").innerHTML = event.detail.html;
    call.dataset.loaded = "true";
  }});
  function loadCallEvidence(call) {{
    if (call.dataset.loaded === "true" || call.dataset.loading === "true") return;
    call.dataset.loading = "true";
    const content = call.querySelector("[data-call-content]");
    content.setAttribute("aria-busy", "true");
    const loading = document.createElement("p");
    loading.className = "meta";
    loading.textContent = "Loading call evidence…";
    content.replaceChildren(loading);
    const script = document.createElement("script");
    script.src = call.dataset.callUrl;
    const complete = () => {{
      if (call.dataset.loaded !== "true") {{
        const message = document.createElement("p");
        message.className = "error-text";
        message.textContent = "Could not load call evidence. Try again or open the review detail.";
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Retry";
        retry.addEventListener("click", () => loadCallEvidence(call));
        const link = document.createElement("a");
        link.href = call.closest("[data-review]").querySelector(".review-link").href + "#" + call.id;
        link.textContent = "View in review detail";
        content.replaceChildren(message, retry, link);
      }}
      delete call.dataset.loading;
      content.removeAttribute("aria-busy");
      script.remove();
    }};
    script.onload = complete;
    script.onerror = complete;
    document.head.append(script);
  }}
  document.querySelectorAll(".model-call[data-call-url]").forEach((call) => {{
    call.addEventListener("toggle", () => {{ if (call.open) loadCallEvidence(call); }});
  }});
  const contextRoot = document.querySelector("[data-context-url]");
  const contextFolds = [...document.querySelectorAll("[data-context-field]")];
  let reviewContext;
  function renderContextEvidence(fold) {{
    const field = fold.dataset.contextField;
    let values;
    if (field === "parent") {{
      values = [reviewContext];
    }} else if (field === "request") {{
      values = [{{model: reviewContext.model ?? null, reasoning: reviewContext.reasoning ?? null,
        system: reviewContext.system_prompt ?? null, messages: reviewContext.messages || [], tools: reviewContext.tools || []}}];
    }} else {{
      const message = reviewContext.messages[Number(field.split(":")[1])];
      if (message && typeof message === "object" && !Array.isArray(message)) {{
        values = [Object.hasOwn(message, "content") ? message.content : message.parts];
        if (message.tool_calls?.length) values.push(message.tool_calls);
      }} else {{
        values = [message];
      }}
    }}
    const content = fold.querySelector("[data-context-content]");
    content.replaceChildren(...values.map(value => {{
      const pre = document.createElement("pre");
      pre.textContent = typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2);
      return pre;
    }}));
    content.removeAttribute("aria-busy");
    fold.dataset.loaded = "true";
  }}
  document.addEventListener("skill-learn-context-evidence", event => {{
    if (!contextRoot || event.detail.id !== contextRoot.dataset.contextId || contextRoot.dataset.loading !== "true") return;
    reviewContext = event.detail.context || {{}};
    contextFolds.filter(fold => fold.open).forEach(renderContextEvidence);
  }});
  function loadContextEvidence(fold) {{
    if (fold.dataset.loaded === "true") return;
    if (reviewContext !== undefined) {{ renderContextEvidence(fold); return; }}
    const content = fold.querySelector("[data-context-content]");
    content.setAttribute("aria-busy", "true");
    const loading = document.createElement("p");
    loading.className = "meta";
    loading.textContent = "Loading stored context…";
    content.replaceChildren(loading);
    if (contextRoot.dataset.loading === "true") return;
    contextRoot.dataset.loading = "true";
    const script = document.createElement("script");
    script.src = contextRoot.dataset.contextUrl;
    const complete = () => {{
      for (const pending of contextFolds) {{
        const content = pending.querySelector("[data-context-content]");
        if (!content.hasAttribute("aria-busy")) continue;
        content.removeAttribute("aria-busy");
        if (reviewContext === undefined) {{
          const message = document.createElement("p");
          message.className = "error-text";
          message.textContent = "Could not load stored context.";
          const retry = document.createElement("button");
          retry.type = "button";
          retry.textContent = "Retry";
          retry.addEventListener("click", () => loadContextEvidence(pending));
          content.replaceChildren(message, retry);
        }}
      }}
      delete contextRoot.dataset.loading;
      script.remove();
    }};
    script.onload = complete;
    script.onerror = complete;
    document.head.append(script);
  }}
  contextFolds.forEach(fold => {{
    fold.addEventListener("toggle", () => {{ if (fold.open) loadContextEvidence(fold); }});
  }});
  if (filter) applyFilters();
  function revealAnchor() {{
    const target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    if (!target) return;
    if (target.hidden && filter) {{
      activeKind = "all";
      filter.value = "";
      applyFilters();
      saveFilters();
    }}
    for (let node = target; node; node = node.parentElement) {{
      if (node.tagName === "DETAILS") node.open = true;
    }}
    target.scrollIntoView({{ block: "start" }});
  }}
  window.addEventListener("hashchange", revealAnchor);
  revealAnchor();
</script>
</body></html>
"""
