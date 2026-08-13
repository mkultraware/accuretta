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
        r"\.effort-popover\s*\{[^}]*width:\s*min\(300px,\s*calc\(100vw\s*-\s*28px\)\)",
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
