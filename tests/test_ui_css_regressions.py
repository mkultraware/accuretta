import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _css() -> str:
    return (ROOT / "app.css").read_text(encoding="utf-8")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_command_history_rows_never_flex_shrink() -> None:
    css = _css()
    assert re.search(
        r"\.cmd-row\s*\{[^}]*\bflex:\s*0\s+0\s+auto\s*;",
        css,
        re.DOTALL,
    )


def test_shared_bubble_materialization_matches_cartograph() -> None:
    css = _css()
    shared = r"animation:\s*bubble-in\s+480ms\s+cubic-bezier\(0\.16,\s*1,\s*0\.3,\s*1\)\s+both"
    assert re.search(r"\.bubble\s*\{[^}]*" + shared, css, re.DOTALL)
    assert re.search(
        r'\[data-theme="cartograph"\]\s+\.bubble\s*\{[^}]*' + shared,
        css,
        re.DOTALL,
    )


def test_reasoning_effort_control_is_progressively_disclosed() -> None:
    html = _read("index.html")
    js = _read("app.js")
    assert 'id="reasoning-effort-control"' in html
    assert 'class="effort-control hidden"' in html
    assert 'type="range" min="0" max="3" step="1"' in html
    assert 'reasoning_effort: state.reasoningEffort || "auto"' in js
    assert "state.reasoningCapability" in js


def test_reasoning_effort_mobile_control_has_touch_sized_target() -> None:
    css = _css()
    assert re.search(
        r"@media\s*\(max-width:\s*600px\)\s*\{.*?\.effort-pill\s*\{[^}]*"
        r"min-height:\s*42px",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"\.effort-popover\s*\{[^}]*width:\s*min\(240px,\s*calc\(100vw\s*-\s*24px\)\)",
        css,
        re.DOTALL,
    )


def test_security_assessment_uses_server_owned_phases() -> None:
    js = _read("app.js")
    assert all(label in js for label in (
        'key: "recon", label: "Recon"',
        'key: "validate", label: "Validate"',
        'key: "exploit", label: "Exploit"',
        'key: "report", label: "Report"',
    ))
    assert 'evt.type === "rt_mission"' in js
    assert "attackRailToolResult(row, evt.name, evt.result)" in js
    assert 'result && result.scope_blocked' in js
    assert 'rail.dataset.status = "closed"' in js


def test_red_team_questionnaire_enforces_a_custom_user_agent() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _css()
    assert 'id="recon-user-agent"' in html
    assert 'maxlength="512"' in html
    assert "Anything not listed is blocked automatically" in html
    assert "Blank In scope is limited to the target above" in html
    assert "user_agent: userAgent" in js
    assert "Accuretta will enforce the required User-Agent" in js
    assert "code > 255" in js
    assert "userAgentTmpl" in js
    assert ".recon-field-hint" in css


def test_passive_osint_has_a_distinct_mode_and_progress_surface() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _css()
    assert 'id="quick-passive-osint"' in html
    assert '>Passive OSINT</button>' in html
    assert '>Red Team</button>' in html
    assert "passive_osint" in js
    assert "No direct target traffic" in js
    assert "Passive intelligence" in js
    assert ".osint-card .oc-boundary" in css


def test_security_assessment_is_responsive_and_motion_safe() -> None:
    css = _css()
    assert "Security assessment progress" in css
    assert re.search(
        r"@media\s*\(max-width:\s*600px\)\s*\{.*?\.attack-rail\s+\.ar-node\s*\{[^}]*"
        r"flex-basis:\s*49px",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?\.attack-rail",
        css,
        re.DOTALL,
    )


def test_frontend_secret_audit_uses_the_shared_authorized_tool_flow() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _css()

    assert 'id="quick-frontend-secrets"' in html
    assert 'id="recon-secret-thorough"' in html
    assert 'id="recon-secret-candidates"' in html
    assert 'reconOpen("frontend_secrets")' in js
    assert 'secretAudit ? "frontend_secrets"' in js
    assert "Call scan_js_secrets exactly once" in js
    assert "include_candidates=false" in js
    assert "copy its exact value into the report" in js
    assert "their exact returned values" in js
    assert "Never send or use a discovered value" in js
    assert "Do not run any other recon or exploit tool" in js
    assert "recon-secret-mode [data-recon-suite-only]" in css
    assert "function ensureSecretRail(row, mission = {})" in js
    assert "function secretRailToolResult(row, name, result)" in js
    assert 'class="sr-icon-code"' in js
    assert 'else if (evt.engagement === "frontend_secrets") secretRailMission(row, evt)' in js
    assert "secretRailFinalize(agentRow)" in js
    assert ".secret-rail .sr-spectrum" in css
    assert "justify-content: space-between" in css
    assert ".secret-rail .sr-sigil svg" in css
    assert "width: calc(100% - 16px)" in css
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.revealer-deck\s*\{[^}]*'
        r'width:\s*calc\(100% \+ 27px\)[^}]*margin:\s*-9px 0 8px -18px',
        css,
        re.DOTALL,
    )
    assert "@keyframes secret-sweep" in css
    assert re.search(
        r"@media\s*\(max-width:\s*600px\)\s*\{.*?\.secret-rail\s*\{[^}]*"
        r"grid-template-columns:\s*34px\s+minmax\(0,\s*1fr\)",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?\.secret-rail",
        css,
        re.DOTALL,
    )


def test_aperture_collapsed_sidebar_control_stays_inside_the_frame() -> None:
    css = _css()
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.app\.sidebar-collapsed\s+\.pull-tab-left\s*\{'
        r'[^}]*left:\s*20px[^}]*top:\s*20px[^}]*border-radius:\s*50%',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.app\.sidebar-collapsed\s+\.topbar\s*\{'
        r'[^}]*padding-left:\s*52px',
        css,
        re.DOTALL,
    )


def test_cartograph_accent_buttons_override_the_neutral_button_surface() -> None:
    css = _css()
    neutral_pos = css.index(
        '[data-theme="cartograph"] :is(.btn, .chip, .cascade-chip)'
    )
    accent_pos = css.index('[data-theme="cartograph"] .btn.accent', neutral_pos)
    assert accent_pos > neutral_pos
    assert re.search(
        r'\[data-theme="cartograph"\]\s+\.btn\.accent\s*\{[^}]*'
        r'background:\s*var\(--accent\)[^}]*color:\s*var\(--accent-fg\)',
        css,
        re.DOTALL,
    )


def test_working_notes_bypass_markdown_code_rendering() -> None:
    js = _read("app.js")
    assert "function renderWorkingNotes(text)" in js
    assert "function renderWorkingNotesBlock(text)" in js
    assert js.count("renderWorkingNotesBlock(interim") == 2
    working_notes = js[js.index("function renderWorkingNotes(text)"):
                       js.index("function renderWorkingNotesBlock(text)")]
    assert "renderMarkdown(" not in working_notes


def test_pending_response_boundary_hides_reasoning_before_next_delta() -> None:
    js = _read("app.js")
    assert "const pendingMarker" in js
    assert "lastMarkerEnd = buf.length" in js


def test_untagged_live_planning_uses_the_thought_surface() -> None:
    js = _read("app.js")
    assert "function isLikelyLiveWorkingNotes(text)" in js
    assert "state.reasoningCapability?.supported" in js
    assert "isLikelyLiveWorkingNotes(content)" in js
    assert 'thinking = content;\n        content = "";' in js


def test_clipboard_has_http_fallback_and_one_direct_api_call() -> None:
    js = _read("app.js")
    assert "async function copyText(text)" in js
    assert 'document.execCommand("copy")' in js
    assert "Browser clipboard access is blocked" in js
    assert js.count("navigator.clipboard.writeText") == 1


def test_remote_client_context_and_file_transfer_controls_are_present() -> None:
    html = _read("index.html")
    js = _read("app.js")
    assert 'id="execution-target-select"' in html
    assert 'id="btn-tailscale-serve"' in html
    assert 'id="remote-pair-command"' in html
    assert 'id="btn-attach-file"' in html
    assert 'client_context: currentClientHint()' in js
    assert 'fetch("/api/client-files/upload"' in js
    assert "?download=1" in js


def test_large_chat_renders_recent_messages_and_loads_history_on_demand() -> None:
    js = _read("app.js")
    assert "messageWindowSize: 40" in js
    assert "state.messages.slice(start)" in js
    assert 'className = "history-window"' in js
    assert "state.messageWindowStart - state.messageWindowSize" in js


def test_collapsed_reasoning_is_materialized_only_when_opened() -> None:
    js = _read("app.js")
    assert "savedThinkContent._fullText = thinkingText" in js
    assert 'if (willOpen) content.textContent = content._fullText || ""' in js
    assert 'if (isHidden) content.textContent = ""' in js


def test_remote_streaming_uses_a_lower_paint_rate_and_skips_old_animations() -> None:
    js = _read("app.js")
    css = _css()
    assert "const paintInterval = state.clientContext?.remote ? 90 : 50" in js
    assert 'document.body.classList.toggle("remote-client", remote)' in js
    assert re.search(
        r"\.bubble-row\s*\{[^}]*content-visibility:\s*auto",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"body\.remote-client\s+\.bubble-row\s*\{[^}]*animation:\s*none",
        css,
        re.DOTALL,
    )


def test_compaction_status_is_inline_asymmetric_and_lifecycle_bound() -> None:
    js = _read("app.js")
    css = _css()
    assert "compactingChats: new Set()" in js
    assert "function ensureCompactionRow(chatId)" in js
    assert 'Array.from({ length: 9 }' in js
    assert 'Compacting..</span>' in js
    assert "inner.insertBefore(row, liveRow)" in js
    assert 'evt.type === "summary_folding"' in js
    assert 'evt.type === "summary_fold_finished"' in js
    assert "finishCompactionRow(evt.chat_id || state.chatId" in js
    assert "_COMPACT_SVG" not in js
    assert re.search(
        r"\.compaction-dot-matrix\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*3px\)",
        css,
        re.DOTALL,
    )
    assert css.count(".compaction-dot-matrix span:nth-child(") >= 9
    assert "@keyframes compaction-dot-pulse" in css
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?"
        r"\.compaction-dot-matrix span\s*\{[^}]*animation:\s*none",
        css,
        re.DOTALL,
    )


def test_aperture_replaces_retired_themes_and_migrates_saved_settings() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _css()
    colors = _read("colors_and_type.css")

    assert '<option value="aperture">Aperture</option>' in html
    assert '<option value="neobrutalism-dark">' not in html
    assert '<option value="kinetic">' not in html
    assert "theme === 'neobrutalism-dark' || theme === 'kinetic'" in html
    assert "theme = 'aperture'" in html

    cycle = re.search(r"const THEME_CYCLE = \[(.*?)\];", js, re.DOTALL)
    assert cycle
    assert '"aperture"' in cycle.group(1)
    assert '"neobrutalism-dark"' not in cycle.group(1)
    assert '"kinetic"' not in cycle.group(1)
    assert '"neobrutalism-dark": "aperture"' in js
    assert 'kinetic: "aperture"' in js

    assert '[data-theme="aperture"]' in colors
    assert '[data-theme="neobrutalism-dark"]' not in colors
    assert '[data-theme="kinetic"]' not in colors
    assert '[data-theme="neobrutalism-dark"]' not in css
    assert '[data-theme="kinetic"]' not in css


def test_aperture_has_a_distinct_spatial_layout_and_motion_fallbacks() -> None:
    css = _css()
    colors = _read("colors_and_type.css")

    assert re.search(
        r'\[data-theme="aperture"\]\s*\{[^}]*--accent:\s*#4C59F2',
        colors,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.sidebar\s*\{[^}]*'
        r'--bg:\s*#F0F0EB[^}]*width:\s*264px[^}]*'
        r'background:\s*var\(--glass-bg\)\s*!important',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.app\s*\{[^}]*gap:\s*10px[^}]*padding:\s*10px',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.composer\s*\{[^}]*'
        r'display:\s*grid[^}]*grid-template-areas:[^}]*"tools foot"[^}]*'
        r'border-radius:\s*21px\s+5px\s+21px\s+21px',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.bubble\.agent:not\(\.quiet\):not\(\.bubble-code-only\)\s*\{'
        r'[^}]*border:\s*0[^}]*background:\s*transparent[^}]*box-shadow:\s*none',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.bubble\.agent:not\(\.quiet\):not\(\.bubble-code-only\)::before\s*\{'
        r'[^}]*display:\s*none',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.welcome-content\s*\{[^}]*'
        r'display:\s*grid[^}]*grid-template-columns:\s*76px',
        css,
        re.DOTALL,
    )
    assert 'content: "01 / CREATE"' in css
    assert "@keyframes aperture-plane-a" in css
    assert "@keyframes aperture-response-enter" in css
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?"
        r'\[data-theme="aperture"\].*?animation:\s*none\s*!important',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+body\.remote-client.*?'
        r'backdrop-filter:\s*none',
        css,
        re.DOTALL,
    )


def test_model_health_has_a_reversible_recommendation_workflow() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _css()
    bridge = _read("bridge.py")

    assert 'id="model-health-recommendation"' in html
    assert "Use recommended settings" in js
    assert 'data-model-health-action="undo"' in js
    assert "25" in bridge and "_MODEL_ADVISOR_BASELINE_TURNS" in bridge
    assert "_MODEL_ADVISOR_BASELINE_SESSIONS = 3" in bridge
    assert "_MODEL_ADVISOR_TRIAL_TURNS = 10" in bridge
    assert 'if p == "/api/model-health/action"' in bridge
    assert "notification_needed" in bridge
    assert "showModelAdvisorNotification" in js
    assert ".model-health-recommendation" in css
    assert ".advisor-toast-apply" in css


def test_aperture_releases_text_layers_after_entrance_motion() -> None:
    css = _css()
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.bubble\.agent:not\(\.quiet\):not\(\.bubble-code-only\)\s*\{'
        r'[^}]*animation:\s*aperture-response-enter\s+520ms\s+var\(--ease-fluid\)\s*;'
        r'[^}]*will-change:\s*auto',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.composer\s*\{[^}]*'
        r'backdrop-filter:\s*none[^}]*will-change:\s*auto',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'\[data-theme="aperture"\]\s+\.drawer\.open\s*\{[^}]*'
        r'transform:\s*none[^}]*will-change:\s*auto',
        css,
        re.DOTALL,
    )
    assert re.search(
        r'@media\s*\(max-width:\s*680px\)\s*\{.*?'
        r'\[data-theme="aperture"\]\s+\.drawer\s*\{[^}]*inset:\s*0\s*!important[^}]*'
        r'width:\s*100vw\s*!important[^}]*max-width:\s*none\s*!important[^}]*border-radius:\s*0',
        css,
        re.DOTALL,
    )
