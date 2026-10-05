import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_learn.core import Core
from skill_learn.reporting import generate
from skill_learn.report_view import render_home
from skill_learn.usage import sum_usage


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / "settings.yaml").write_text("library:\n  root: skills\nnotifications:\n  enabled: false\napproval:\n  generated: manual\n")
        self.core = Core(self.home)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.core.close)
        self.identity = 0

    def request(self, operation, **payload):
        self.identity += 1
        response = self.core.request(f"report-test-{self.identity}", operation, payload)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def review(self, session="session", watermark="one", name="Ordering investigation", result="Nothing to save."):
        answer = self.request("enqueue", harness="opencode", hostID="host", sessionID=session, watermark=watermark,
                              sessionName=name, messages=[{"info": {"role": "user", "id": "parent"}, "parts": [{"type": "text", "text": "Complete parent evidence " + "x" * 10000}]}])
        review_id = answer["reviewId"]
        self.request("claim", hostID="host", owner="owner")
        self.request("bind", reviewID=review_id, reviewerID="native_" + review_id, owner="owner", inheritedIDs=["old"])
        call = self.request("admit", reviewID=review_id, reviewerID="native_" + review_id, owner="owner")
        self.request("record", reviewID=review_id, callID=call["callID"], eventID="step", event={"messageID": "assistant", "part": {"id": "step", "type": "step-finish", "tokens": {"input": 2000, "output": 10, "reasoning": 5, "cache": {"read": 8000, "write": 0}}}})
        self.request("finish", reviewID=review_id, text=result, messages=[{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": result}]}])
        return review_id

    def generation(self):
        root = self.core.settings.reports_root
        return root / ".generations" / json.loads((root / ".skill-learn-report.json").read_text())["generation"]

    def cli(self, *arguments, ok=True):
        command = subprocess.run([sys.executable, "-m", "skill_learn", "--home", str(self.home), *arguments], capture_output=True, text=True)
        self.assertEqual(command.returncode, 0 if ok else 1, command.stderr)
        return json.loads(command.stdout) if ok else command.stderr

    def test_static_hierarchy_links_complete_evidence_and_escaping(self):
        hostile = '</script><img src=x onerror="window.attacked=true">'
        first = self.review(name=hostile, result="Nothing to save.")
        second = self.review(watermark="two", name=hostile, result='{"changes":[{"name":"ordering","action":"create","content":"Use for ordering work."}]}')
        self.request("enqueue", harness="opencode", hostID="host", sessionID="delegate", sessionName="Delegate", watermark="d", messages=[], delegateDepth=1)
        directory = self.generation()
        index = (self.core.settings.reports_root / "index.html").read_text()
        self.assertIn('data-section="summary"', index)
        self.assertIn('href="skills.html?status=published"', index)
        self.assertNotIn("<base", (directory / "index.html").read_text())
        index = (directory / "sessions.html").read_text()
        self.assertIn("&lt;/script&gt;", index)
        self.assertNotIn(hostile, index)
        self.assertLess(index.index("session-name" + '\">' + "&lt;/script&gt;"), index.index("[session]"))
        self.assertLess(index.index("review-" + second + ".html"), index.index("review-" + first + ".html"))
        self.assertIn("80% cached", index)
        self.assertIn("Delegate — not reviewed", index)
        self.assertNotIn("Complete parent evidence", index)
        self.assertNotIn("<form", index)
        self.assertNotIn("fetch(", index)
        for target in re.findall(r'href="(review-[^"]+\.html)(?:#[^"]+)?"', index):
            self.assertTrue((directory / target).is_file(), target)
        detail = (directory / f"review-{second}.html").read_text()
        for label in ("Complete parent evidence", "Complete request fields", "Complete host response fields", "within-review", "raw provider counters/availability unavailable", "ordering", "sessions.html"):
            if label == "within-review":
                continue
            self.assertIn(label, detail)
        self.assertIn("initial parent-to-review observation", detail)
        self.assertIn("10,000", detail)

    def test_overview_and_sessions_use_counters_only_and_partial_usage_is_retained(self):
        review = self.review()
        store = self.core.store
        store.add_model_call(review, "opencode", "reviewer", 2, "m", {"secret": "WIRE_BODY"}, {"content": "FULL_RESPONSE", "usage": {"input_tokens": 2000}})
        store.add_model_call(review, "opencode", "reviewer", 3, "m", {}, None)
        self.assertEqual(sum_usage(store.model_call_usage()), sum_usage(store.list_model_calls()))
        with patch.object(store, "list_model_calls", side_effect=AssertionError("Overview loaded full evidence")), \
             patch.object(store, "model_calls_for_review", side_effect=AssertionError("Overview loaded call evidence")):
            for section in ("summary", "sessions"):
                page = render_home(store, section=section)
                self.assertNotIn("WIRE_BODY", page)
                self.assertNotIn("FULL_RESPONSE", page)
                self.assertIn("12,000 (partial)", page)
                self.assertEqual(len(re.findall(r'<a\b[^>]*aria-current="page"', page)), 1)

    def test_published_pending_rejected_and_retained_library_views_match_service_semantics(self):
        first = self.review()
        second = self.review(watermark="two")
        store = self.core.store
        applied = store.add_proposal(first, "dns-checks", "create", "Create DNS checks", {}, None)
        store.mark_proposal(applied, "applied")
        approved = store.add_proposal(first, "dns-checks", "patch", "Improve DNS checks", {}, None)
        store.mark_proposal(approved, "approved")
        pending = store.add_proposal(second, "dns-checks", "patch", 'Add "fallback" & tracing', {}, None)
        pending_only = store.add_proposal(second, "pending-only", "create", "Draft skill", {}, None)
        rejected = store.add_proposal(first, "rejected-only", "create", "Rejected idea", {}, None)
        store.mark_proposal(rejected, "rejected")
        store._connection.execute("UPDATE reviews SET status='staged',outcome='staged' WHERE id=?", (first,))
        store._connection.commit()
        generate(store, self.core.settings)
        directory = self.generation()
        sessions = (directory / "sessions.html").read_text()
        summary = sessions.split('class="session-calls"', 1)[1].split('</summary>', 1)[0]
        self.assertEqual(summary.count('>dns-checks</a>'), 2)
        self.assertIn('href="skills.html#skill-dns-checks"', summary)
        self.assertIn(f'href="skills.html#proposal-{pending}"', summary)
        detail = (directory / f"review-{first}.html").read_text()
        self.assertIn("Changes proposed", detail)
        self.assertIn('class="change-label">Applied', detail)
        self.assertNotIn('class="change-label">Proposed', detail)
        skills = (directory / "skills.html").read_text()
        for proposal in (applied, approved, pending, pending_only, rejected):
            self.assertEqual(skills.count(f'id="proposal-{proposal}"'), 1)
        self.assertIn('id="skill-dns-checks"', skills)
        self.assertIn("History only", skills)
        self.assertIn("2 published changes", skills)
        self.assertNotIn('id="skill-pending-only"', skills)
        self.assertNotIn('id="skill-rejected-only"', skills)
        self.assertIn('Add &quot;fallback&quot; &amp; tracing', skills)
        self.assertIn('>Ordering investigation</a>', skills)
        self.assertIn('href="skills.html?status=published"><span>Changes applied</span><strong>2</strong>', (self.core.settings.reports_root / "index.html").read_text())

        staged = self.core.library.apply_feedback(second, {"name": "retained-skill", "action": "create", "description": "When investigating <DNS> & cache.", "content": "SECRET BODY"})
        self.assertTrue(self.core.library.approve(staged["proposalId"])["ok"])
        self.core.library.pin("retained-skill", True)
        store.delete_session("opencode", "session")
        generate(store, self.core.settings)
        skills = (self.generation() / "skills.html").read_text()
        for text in ('id="skill-retained-skill"', "Pinned", "When investigating &lt;DNS&gt; &amp; cache.", "No retained review history", 'data-skill-states="applied"'):
            self.assertIn(text, skills)
        self.assertNotIn("SECRET BODY", skills)

    def test_failed_generation_retains_all_previous_pages_and_foreign_files(self):
        review = self.review()
        root, old = self.core.settings.reports_root, self.generation()
        index = (root / "index.html").read_bytes()
        detail = (old / f"review-{review}.html").read_bytes()
        foreign = old / "operator-note.txt"
        foreign.write_text("keep")
        with patch("skill_learn.report_view.render_review", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                generate(self.core.store, self.core.settings)
        self.assertEqual((root / "index.html").read_bytes(), index)
        self.assertEqual((old / f"review-{review}.html").read_bytes(), detail)
        self.assertEqual(len(list((root / ".generations").iterdir())), 1)
        generate(self.core.store, self.core.settings)
        self.assertEqual(foreign.read_text(), "keep")
        self.assertFalse((old / f"review-{review}.html").exists())
        before_swap = (root / "index.html").read_bytes()
        real_replace = os.replace
        def fail_entrypoint(source, target):
            if Path(target) == root / "index.html":
                raise OSError("entrypoint failed")
            return real_replace(source, target)
        with patch("skill_learn.reporting.os.replace", fail_entrypoint), self.assertRaises(OSError):
            generate(self.core.store, self.core.settings)
        self.assertEqual((root / "index.html").read_bytes(), before_swap)

    def test_management_rename_deletion_and_active_reconciliation_without_host(self):
        review = self.review(result='{"changes":[{"name":"ordering","action":"create","content":"Use for ordering work."}]}')
        proposal = self.cli("pending")[0]["id"]
        self.assertEqual(self.cli("show", proposal)["status"], "pending")
        self.assertTrue(self.cli("approve", proposal)["ok"])
        self.assertTrue(self.cli("pin", "ordering")["pinned"])
        self.assertFalse(self.cli("unpin", "ordering")["pinned"])
        self.assertTrue(self.cli("adopt", "ordering")["ok"])
        self.assertEqual(self.cli("show", review)["outcome"], "staged")
        self.request("enqueue", harness="opencode", hostID="host", sessionID="session", watermark="one", sessionName="Renamed session", messages=[])
        self.assertIn("Renamed session", (self.core.settings.reports_root / "index.html").read_text())
        second = self.review(session="another", result='{"changes":[{"name":"rejected","action":"create","content":"Use for different work."}]}')
        pending = self.cli("pending")[0]["id"]
        self.cli("reject", pending)
        self.assertFalse((self.home / "skills/rejected").exists())
        old = self.generation()
        self.assertEqual(self.cli("delete-session", "opencode", "session")["deletedReviews"], 1)
        self.assertTrue((self.home / "skills/ordering/SKILL.md").is_file())
        self.assertIsNotNone(self.core.store.review_row(second))
        self.assertIsNone(self.core.store.review_row(review))
        self.assertFalse((old / f"review-{review}.html").exists())
        active = self.request("enqueue", harness="opencode", hostID="host", sessionID="active", watermark="a", messages=[])["reviewId"]
        self.request("claim", hostID="host", owner="owner")
        self.assertIn("reconciled", self.cli("delete-session", "opencode", "active", ok=False))
        self.assertTrue(self.core.store.cancel_requested(active))
        self.cli("reconcile", active, "--state", "abandoned")
        self.cli("delete-session", "opencode", "active")
        self.assertTrue(Path(self.cli("report")["index"]).is_file())

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for file-URL acceptance")
    def test_file_url_browser_filters_links_themes_and_narrow_layout(self):
        first = self.review()
        failed = self.review(watermark="two", result="invalid result")
        other = self.review(session="dns", name="DNS investigation", result='{"changes":[{"name":"dns-checks","action":"create","content":"Use for DNS checks."}]}')
        root, directory = self.core.settings.reports_root, self.generation()
        assertions = r"""
        <script>
        addEventListener('load', () => {
          try {
            const check = (ok, message) => { if (!ok) throw Error(message); };
            const groups = () => [...document.querySelectorAll('.session-calls')].filter(n => !n.hidden);
            const reviews = () => groups().flatMap(n => [...n.querySelectorAll('[data-review]')].filter(r => !r.hidden));
            const search = value => { const input = document.querySelector('[data-filter]'); input.value = value; input.dispatchEvent(new Event('input')); };
            for (const query of ['Ordering investigation', 'gpt-5.5', 'dns-checks', 'REVIEW_ID']) {
              search(query); check(groups().length > 0 && reviews().length > 0, 'query ' + query);
            }
            search('Ordering'); document.querySelector('[data-stat="failed"]').click();
            check(groups().length === 1 && reviews().length === 1, 'combined failed query');
            search('DNS'); check(groups().length === 0, 'combined nonmatching query');
            document.querySelector('[data-stat="failed"]').click();
            document.querySelector('[data-stat="pending"]').click();
            check(groups().length === 1 && reviews().length === 1, 'pending proposal query');
            document.querySelector('[data-stat="pending"]').click(); search('');
            document.querySelectorAll('details').forEach(n => n.open = true);
            check(document.documentElement.scrollWidth <= innerWidth + 1, 'horizontal overflow');
            check(!window.attacked, 'hostile HTML executed');
            check(getComputedStyle(document.documentElement).colorScheme === 'THEME', 'theme');
            const link = document.querySelector('.review-link');
            check(link.href.startsWith('file:') && link.href.includes('/.generations/'), 'static detail URL');
            check(/\d{4}/.test(document.querySelector('time').textContent), 'readable time');
            check(innerWidth === WIDTH, 'requested viewport');
            document.body.dataset.browserResult = 'passed';
          } catch (error) { document.body.dataset.browserResult = error.message; }
        });
        </script>
        """.replace("REVIEW_ID", first)
        sessions = directory / "sessions.html"
        index = sessions.read_text()
        for theme, width in (("light", 1280), ("dark", 1280), ("light", 390), ("dark", 390)):
            with self.subTest(theme=theme, width=width):
                sessions.write_text(index.replace("</body>", assertions.replace("THEME", theme).replace("WIDTH", str(width)) + "</body>"))
                result = self.chrome(sessions, theme, width)
                self.assertEqual(re.search(r'data-browser-result="([^"]+)"', result).group(1), "passed")
        detail = directory / f"review-{other}.html"
        detail_assertions = """<script>addEventListener('load', () => { document.querySelectorAll('details').forEach(n => n.open = true); document.body.dataset.detailResult = document.documentElement.scrollWidth <= innerWidth + 1 && document.querySelector('.eyebrow a').href === 'SESSIONS_URL' ? 'passed' : 'failed'; });</script>""".replace("SESSIONS_URL", sessions.as_uri())
        detail.write_text(detail.read_text().replace("</body>", detail_assertions + "</body>"))
        for width in (1280, 390):
            self.assertIn('data-detail-result="passed"', self.chrome(detail, "light", width))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for file-URL acceptance")
    def test_file_url_section_navigation_skill_filters_and_deep_links(self):
        review = self.review()
        store = self.core.store
        applied = store.add_proposal(review, "dns-checks", "create", "Create DNS checks", {}, None)
        store.mark_proposal(applied, "applied")
        approved = store.add_proposal(review, "dns-checks", "patch", "Improve DNS checks", {}, None)
        store.mark_proposal(approved, "approved")
        pending = store.add_proposal(review, "dns-checks", "patch", "Add fallback", {}, None)
        store.add_proposal(review, "pending-only", "create", "Draft skill", {}, None)
        rejected = store.add_proposal(review, "rejected-only", "create", "Rejected idea", {}, None)
        store.mark_proposal(rejected, "rejected")
        generate(store, self.core.settings)
        directory = self.generation()
        skills = directory / "skills.html"
        assertions = r"""<script>addEventListener('load', () => {
          try {
            const check = (ok, message) => { if (!ok) throw Error(message); };
            const visible = selector => [...document.querySelectorAll(selector)].filter(n => !n.hidden);
            check(document.querySelectorAll('.tabs [aria-current="page"]').length === 1, 'one current section');
            check(document.querySelector('.tabs [aria-current="page"]').textContent.includes('Skills & proposals'), 'Skills is current');
            check([...document.querySelectorAll('.tabs a')].every(n => n.href.startsWith('file:')), 'offline section links');
            check(document.querySelector('[data-stat="pending"]').getAttribute('aria-pressed') === 'true', 'URL status initialization');
            check(visible('[data-proposal]').length === 2 && visible('[data-skill]').length === 1, 'pending changes and published skills are distinct');
            check(!document.getElementById('skill-pending-only'), 'pending-only is not a learned skill');
            const input = document.querySelector('[data-filter]');
            input.value = 'pending-only'; input.dispatchEvent(new Event('input'));
            check(visible('[data-proposal]').length === 1 && visible('[data-skill]').length === 0, 'combined skill search');
            input.value = 'missing'; input.dispatchEvent(new Event('input'));
            check(!document.querySelector('#proposal-filter-empty').hidden && !document.querySelector('#skill-filter-empty').hidden, 'empty messages');
            input.value = ''; input.dispatchEvent(new Event('input'));
            document.querySelector('[data-stat="published"]').click();
            check(visible('[data-proposal]').length === 2, 'published includes approved');
            document.querySelector('[data-stat="approved"]').click();
            check(visible('[data-proposal]').length === 1, 'manual approval filter');
            document.querySelector('#proposals > details').open = false;
            history.replaceState(null, '', '#proposal-PENDING_ID');
            window.dispatchEvent(new HashChangeEvent('hashchange'));
            const target = document.getElementById('proposal-PENDING_ID');
            check(!target.hidden && target.open && document.querySelector('#proposals > details').open, 'deep link reveals collapsed target');
            check(document.querySelector('[data-stat="all"]').getAttribute('aria-pressed') === 'true', 'anchor clears incompatible filter');
            const ids = [...document.querySelectorAll('[id]')].map(n => n.id);
            check(ids.length === new Set(ids).size, 'unique anchors');
            check(document.documentElement.scrollWidth <= innerWidth + 1, 'no horizontal overflow');
            document.body.dataset.browserResult = 'passed';
          } catch (error) { document.body.dataset.browserResult = error.message; }
        });</script>""".replace("PENDING_ID", pending)
        skills.write_text(skills.read_text().replace("</body>", assertions + "</body>"))
        for theme, width in (("light", 1280), ("dark", 390)):
            result = self.chrome(skills.as_uri() + "?status=pending", theme, width)
            self.assertEqual(re.search(r'data-browser-result="([^"]+)"', result).group(1), "passed")
        index = self.core.settings.reports_root / "index.html"
        index.write_text(index.read_text().replace("</body>", """<script>addEventListener('load', () => {
          const nav = document.querySelector('.tabs [aria-current="page"]');
          document.body.dataset.browserResult = nav.textContent === 'Summary' && document.querySelector('[data-section="summary"]') && !document.querySelector('.model-call') && document.querySelector('.tabs a[href="sessions.html"]').href.includes('/.generations/') ? 'passed' : 'failed';
        });</script></body>"""))
        self.assertIn('data-browser-result="passed"', self.chrome(index, "light", 390))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for file-URL acceptance")
    def test_file_url_call_evidence_loads_on_expansion_retries_and_stays_escaped(self):
        review = self.review()
        hostile = '</script><img src=x onerror="window.attacked=true">'
        call = self.core.store.add_model_call(review, "opencode", "reviewer", 2, "m",
            {"messages": [{"role": "user", "content": "LAZY_REQUEST " + hostile}]},
            {"content": "FULL_RESPONSE " + hostile, "usage": {"input_tokens": 10000, "cache_read_tokens": 8000}},
            evidence_kind="host_requested", call_status="completed")
        generate(self.core.store, self.core.settings)
        sessions = self.generation() / "sessions.html"
        self.assertNotIn("LAZY_REQUEST", sessions.read_text())
        self.assertTrue((self.generation() / f"call-{call}.js").is_file())
        assertions = r"""<script>addEventListener('load', async () => {
          try {
            const check = (ok, message) => { if (!ok) throw Error(message); };
            const until = async predicate => {
              for (let i = 0; i < 200; i++) {
                if (predicate()) return;
                await new Promise(resolve => setTimeout(resolve, 10));
              }
              throw Error('evidence did not finish loading');
            };
            let deliveries = 0;
            document.addEventListener('skill-learn-call-evidence', () => deliveries++);
            const call = document.getElementById('call-CALL_ID');
            const content = call.querySelector('[data-call-content]');
            check(deliveries === 0 && !content.textContent.includes('FULL_RESPONSE'), 'no eager evidence load');
            document.querySelector('[data-expand="#reviews"][data-mode="open"]').click();
            await new Promise(resolve => setTimeout(resolve, 20));
            check(deliveries === 0, 'expanding sessions keeps evidence lazy');
            const source = call.dataset.callUrl;
            call.dataset.callUrl = 'missing-call.js';
            call.open = true;
            await until(() => content.querySelector('button'));
            check(content.textContent.includes('Could not load') && content.querySelector('a').href.startsWith('file:'), 'retry and offline fallback');
            call.dataset.callUrl = source;
            content.querySelector('button').click();
            await until(() => call.dataset.loaded === 'true' && !content.hasAttribute('aria-busy'));
            check(deliveries === 1 && content.textContent.includes('LAZY_REQUEST') && content.textContent.includes('FULL_RESPONSE'), 'expanded inline evidence');
            check(content.textContent.includes('Complete request fields') && content.textContent.includes('Complete host response fields'), 'full fields remain accessible');
            check([...content.querySelectorAll('details')].every(n => !n.open), 'nested evidence stays collapsed');
            check(!window.attacked && !content.querySelector('img'), 'stored HTML stays escaped');
            call.open = false;
            await new Promise(resolve => setTimeout(resolve, 20));
            call.open = true;
            await new Promise(resolve => setTimeout(resolve, 20));
            check(deliveries === 1, 'reopening reuses loaded evidence');
            check(document.documentElement.scrollWidth <= innerWidth + 1, 'expanded evidence does not overflow');
            document.body.dataset.browserResult = 'passed';
          } catch (error) { document.body.dataset.browserResult = error.message; }
        });</script>""".replace("CALL_ID", str(call))
        sessions.write_text(sessions.read_text().replace("</body>", assertions + "</body>"))
        for width in (1280, 390):
            result = self.chrome(sessions, "light", width, wait_for="document.body.dataset.browserResult")
            self.assertEqual(re.search(r'data-browser-result="([^"]+)"', result).group(1), "passed")

    def chrome(self, path, theme, width, wait_for=None):
        with tempfile.TemporaryDirectory(dir=self.home) as profile:
            # Chrome's desktop window has a 500px minimum. Its local CDP pipe
            # sets an exact 390px viewport and OS theme without a test server.
            command = ["bash", "-c", 'exec "$@" 3<&0 4>&1', "chrome", "google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-extensions", "--disable-background-networking", "--no-first-run", "--no-proxy-server", "--remote-debugging-pipe", "--user-data-dir=" + profile]
            browser = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            sequence, buffered = 0, b""
            def cdp(method, params=None, session_id=None):
                nonlocal sequence, buffered
                sequence += 1
                frame = {"id": sequence, "method": method, "params": params or {}}
                if session_id:
                    frame["sessionId"] = session_id
                browser.stdin.write(json.dumps(frame).encode() + b"\0")
                browser.stdin.flush()
                while True:
                    if b"\0" not in buffered:
                        self.assertTrue(select.select([browser.stdout], [], [], 30)[0], method)
                        chunk = os.read(browser.stdout.fileno(), 65536)
                        self.assertTrue(chunk, "Chrome debugging pipe closed")
                        buffered += chunk
                        continue
                    line, buffered = buffered.split(b"\0", 1)
                    response = json.loads(line)
                    if response.get("id") == sequence:
                        self.assertNotIn("error", response, response)
                        return response.get("result", {})
            try:
                target = cdp("Target.createTarget", {"url": "about:blank"})["targetId"]
                session = cdp("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
                cdp("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False}, session)
                cdp("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": theme}]}, session)
                cdp("Page.navigate", {"url": path.as_uri() if isinstance(path, Path) else path}, session)
                cdp("Runtime.evaluate", {"expression": "new Promise(resolve => document.readyState === 'complete' ? resolve() : addEventListener('load', resolve, {once:true}))", "awaitPromise": True}, session)
                if wait_for:
                    cdp("Runtime.evaluate", {"expression": "new Promise(resolve => { const deadline = Date.now() + 5000; const check = () => { if (" + wait_for + " || Date.now() >= deadline) resolve(); else setTimeout(check, 10); }; check(); })", "awaitPromise": True}, session)
                return cdp("Runtime.evaluate", {"expression": "document.documentElement.outerHTML", "returnByValue": True}, session)["result"]["value"]
            finally:
                browser.terminate()
                browser.wait(timeout=10)
                browser.stdin.close()
                browser.stdout.close()
