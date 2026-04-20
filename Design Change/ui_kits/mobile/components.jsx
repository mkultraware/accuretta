/* global React */
const { useState } = React;

function MIcon({ name, weight = "", size = 18, style = {} }) {
  const cls = weight ? `ph-${weight} ph-${name}` : `ph ph-${name}`;
  return <i className={cls} style={{ fontSize: size, lineHeight: 1, ...style }} />;
}

function StatusBar() {
  return (
    <div className="mb-status">
      <span>9:41</span>
      <span className="mb-status-right">
        <MIcon name="cell-signal-high" size={12} />
        <MIcon name="wifi-high" size={12} />
        <MIcon name="battery-high" size={14} />
      </span>
    </div>
  );
}

function AppBar({ title, subtitle, right }) {
  return (
    <div className="mb-appbar">
      <button className="mb-iconbtn"><MIcon name="list" size={20} /></button>
      <div className="mb-appbar-title">
        <div className="t">{title}</div>
        {subtitle && <div className="s">{subtitle}</div>}
      </div>
      {right || <button className="mb-iconbtn"><MIcon name="dots-three" size={20} /></button>}
    </div>
  );
}

function BottomNav({ active, setActive }) {
  const items = [
    { id: "sessions", icon: "chats-circle", label: "Sessions" },
    { id: "runs", icon: "clock-counter-clockwise", label: "Runs" },
    { id: "plan", icon: "list-checks", label: "Plan" },
    { id: "settings", icon: "gear-six", label: "Settings" },
  ];
  return (
    <div className="mb-bottomnav">
      {items.map(i => (
        <button
          key={i.id}
          className={`mb-tab ${active === i.id ? "active" : ""}`}
          onClick={() => setActive(i.id)}
        >
          <MIcon name={i.icon} size={20} weight={active === i.id ? "fill" : ""} />
          <span>{i.label}</span>
        </button>
      ))}
    </div>
  );
}

function SessionRow({ title, meta, status, model }) {
  const statusMap = {
    live: { cls: "mint", label: "live" },
    done: { cls: "slate", label: "done" },
    paused: { cls: "lemon", label: "paused" },
    failed: { cls: "safety", label: "failed" },
  };
  const st = statusMap[status];
  return (
    <div className="mb-session">
      <div className="mb-session-top">
        <span className="mb-session-title">{title}</span>
        <span className={`mb-badge ${st.cls}`}>{status === "live" && <span className="dot" />}{st.label}</span>
      </div>
      <div className="mb-session-meta">
        <span>{meta}</span>
        <span className="sep">·</span>
        <span>{model}</span>
      </div>
    </div>
  );
}

function SessionsScreen() {
  return (
    <>
      <AppBar title="Sessions" subtitle="3 active · 12 total" right={
        <button className="mb-iconbtn"><MIcon name="plus" size={20} /></button>
      }/>
      <div className="mb-scroll">
        <div className="mb-search">
          <MIcon name="magnifying-glass" size={14} />
          <input placeholder="Search sessions…" />
        </div>
        <div className="mb-section-label">Today</div>
        <SessionRow title="pricing page prerender" meta="00:12:44" status="live" model="claude-3-7" />
        <SessionRow title="refactor agent.ts" meta="fi 2h ago" status="paused" model="gpt-5" />
        <SessionRow title="write README" meta="fi 4h ago" status="done" model="claude-3-7" />
        <div className="mb-section-label">Yesterday</div>
        <SessionRow title="debug streaming bug" meta="fi 1d ago" status="done" model="deepseek-r1" />
        <SessionRow title="onboarding copy" meta="fi 1d ago" status="failed" model="gpt-5" />
      </div>
    </>
  );
}

function PlanScreen() {
  return (
    <>
      <AppBar title="Plan" subtitle="pricing page · step 3 of 14" />
      <div className="mb-scroll">
        <div className="mb-plancard">
          <div className="mb-plancard-head">
            <span className="mb-badge mint"><span className="dot" />running</span>
            <span className="mb-plancard-meta">1,840 tok/s</span>
          </div>
          <div className="mb-plancard-title">Write hero section</div>
          <div className="mb-plancard-body">
            Streaming HTML into prerender pane. Model is composing the headline and sub-copy with a pricing-focused framing.
          </div>
          <div className="mb-plancard-progress">
            <div className="bar" style={{ width: "42%" }} />
          </div>
        </div>

        <div className="mb-section-label">Queue</div>
        <div className="mb-stepmini done">
          <MIcon name="check" size={12} weight="bold" />
          <span>Read brand notes</span>
          <span className="meta">212t</span>
        </div>
        <div className="mb-stepmini done">
          <MIcon name="check" size={12} weight="bold" />
          <span>Scaffold layout grid</span>
          <span className="meta">1.2k t</span>
        </div>
        <div className="mb-stepmini run">
          <MIcon name="circle-notch" size={12} />
          <span>Write hero section</span>
          <span className="meta">0.9k t</span>
        </div>
        <div className="mb-stepmini queued">
          <MIcon name="circle" size={12} />
          <span>Compose pricing tiers</span>
          <span className="meta">–</span>
        </div>
        <div className="mb-stepmini queued">
          <MIcon name="circle" size={12} />
          <span>Wire CTA → waitlist</span>
          <span className="meta">–</span>
        </div>

        <div className="mb-approve">
          <div>
            <div className="mb-approve-t">Approve remaining 11 steps?</div>
            <div className="mb-approve-s">Auto-runs when approved. You can pause anytime.</div>
          </div>
          <div className="mb-approve-btns">
            <button className="mb-btn ghost">Review</button>
            <button className="mb-btn accent">Approve</button>
          </div>
        </div>
      </div>
    </>
  );
}

function RunsScreen() {
  return (
    <>
      <AppBar title="Runs" subtitle="last 7 days" />
      <div className="mb-scroll">
        <div className="mb-stats">
          <div className="mb-stat">
            <span className="n">47</span><span className="l">runs</span>
          </div>
          <div className="mb-stat">
            <span className="n">2.4M</span><span className="l">tokens</span>
          </div>
          <div className="mb-stat mint">
            <span className="n">94%</span><span className="l">success</span>
          </div>
        </div>
        <div className="mb-section-label">Recent</div>
        {[
          { t: "pricing page prerender", s: "live", d: "just now" },
          { t: "refactor agent.ts", s: "done", d: "2h ago" },
          { t: "write README", s: "done", d: "4h ago" },
          { t: "debug streaming bug", s: "done", d: "1d ago" },
          { t: "onboarding copy", s: "failed", d: "1d ago" },
        ].map((r, i) => (
          <div key={i} className="mb-runrow">
            <div className={`mb-runrow-dot ${r.s}`} />
            <span className="t">{r.t}</span>
            <span className="d">{r.d}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function SettingsScreen() {
  return (
    <>
      <AppBar title="Settings" />
      <div className="mb-scroll">
        <div className="mb-profile">
          <div className="mb-avatar">mb</div>
          <div>
            <div className="mb-profile-n">Local user</div>
            <div className="mb-profile-s">~/.accuretta</div>
          </div>
        </div>
        <div className="mb-section-label">Models</div>
        <div className="mb-setting">
          <MIcon name="cpu" size={16} /><span>claude-3-7-sonnet</span><span className="mb-badge mint">default</span>
        </div>
        <div className="mb-setting">
          <MIcon name="cpu" size={16} /><span>gpt-5</span>
        </div>
        <div className="mb-setting">
          <MIcon name="plus-circle" size={16} /><span>Add model</span>
        </div>
        <div className="mb-section-label">Preferences</div>
        <div className="mb-setting">
          <MIcon name="moon" size={16} /><span>Dark mode</span>
          <div className="mb-sw" />
        </div>
        <div className="mb-setting">
          <MIcon name="lock-key" size={16} /><span>Local only</span>
          <div className="mb-sw on" />
        </div>
        <div className="mb-setting">
          <MIcon name="bell" size={16} /><span>Notify on completion</span>
          <div className="mb-sw on" />
        </div>
      </div>
    </>
  );
}

Object.assign(window, { StatusBar, AppBar, BottomNav, SessionRow, SessionsScreen, PlanScreen, RunsScreen, SettingsScreen, MIcon });
