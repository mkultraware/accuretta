/* global React */
const { useState } = React;

function Icon({ name, weight = "", size = 16, style = {} }) {
  const cls = weight ? `ph-${weight} ph-${name}` : `ph ph-${name}`;
  return <i className={cls} style={{ fontSize: size, lineHeight: 1, ...style }} />;
}

function Sidebar({ active, setActive }) {
  const items = [
    { id: "sessions", icon: "chats-circle", label: "Sessions" },
    { id: "files", icon: "folder-open", label: "Files" },
    { id: "agents", icon: "robot", label: "Agents" },
    { id: "runs", icon: "clock-counter-clockwise", label: "Runs" },
    { id: "models", icon: "cpu", label: "Models" },
  ];
  return (
    <aside className="acr-sidebar">
      <div className="acr-brand">
        <img src="../../assets/logo-mark.png" alt="" />
        <span>accuretta</span>
      </div>
      <div className="acr-nav">
        {items.map(i => (
          <button
            key={i.id}
            className={`acr-navitem ${active === i.id ? "active" : ""}`}
            onClick={() => setActive(i.id)}
          >
            <Icon name={i.icon} size={16} />
            <span>{i.label}</span>
          </button>
        ))}
      </div>
      <div className="acr-nav-foot">
        <div className="acr-status-pill">
          <span className="dot" />
          <span>local</span>
          <span className="sep">·</span>
          <span>v0.8.2</span>
        </div>
        <button className="acr-navitem sm">
          <Icon name="gear-six" size={14} />
          <span>Settings</span>
        </button>
      </div>
    </aside>
  );
}

function FileTree() {
  const files = [
    { n: "agent.ts", i: "file-ts", active: true },
    { n: "prerender.tsx", i: "file-tsx" },
    { n: "planner.ts", i: "file-ts" },
    { n: "config.yml", i: "file" },
    { n: "README.md", i: "file-text" },
  ];
  return (
    <div className="acr-tree">
      <div className="acr-tree-head">
        <Icon name="folder-open" size={13} />
        <span>accuretta</span>
        <span className="acr-tree-branch">main</span>
      </div>
      {files.map((f, idx) => (
        <div key={idx} className={`acr-tree-file ${f.active ? "active" : ""}`}>
          <Icon name={f.i} size={13} />
          <span>{f.n}</span>
        </div>
      ))}
    </div>
  );
}

function Composer() {
  const [val, setVal] = useState("prerender a pricing page for a local AI IDE");
  return (
    <div className="acr-composer-wrap">
      <div className="acr-composer-center">
        <div className="acr-composer-tools">
          <button className="acr-chip"><Icon name="paperclip" size={12} />Attach</button>
          <button className="acr-chip"><Icon name="image" size={12} />Screen</button>
          <button className="acr-chip accent"><Icon name="lightning" size={12} weight="bold" />Prerender on send</button>
          <span className="acr-composer-spacer" />
          <span className="acr-model-pill">claude-3-7-sonnet</span>
        </div>
        <textarea
          className="acr-composer-input"
          value={val}
          onChange={e => setVal(e.target.value)}
          rows={2}
        />
        <div className="acr-composer-foot">
          <span className="acr-composer-hint">⌘⏎ to run · ⌘K for commands</span>
          <button className="acr-btn accent">
            <Icon name="lightning" size={13} weight="bold" />
            Run agent
          </button>
        </div>
      </div>
    </div>
  );
}

function ChatBubble({ who, children, meta }) {
  return (
    <div className={`acr-bubble-row ${who}`}>
      {who === "agent" && <div className="acr-bubble-avatar"><Icon name="sparkle" size={12} weight="bold" /></div>}
      <div className="acr-bubble-col">
        <div className={`acr-bubble ${who}`}>{children}</div>
        {meta && <div className="acr-bubble-meta">{meta}</div>}
      </div>
      {who === "user" && <div className="acr-bubble-avatar user">mb</div>}
    </div>
  );
}

function ChatView() {
  return (
    <div className="acr-chat">
      <div className="acr-chat-head">
        <div className="acr-panel-title">
          <Icon name="chats-circle" size={14} />
          <span>pricing page prerender</span>
          <span className="acr-badge mint">● running</span>
        </div>
        <div className="acr-panel-tools">
          <button className="acr-iconbtn"><Icon name="pause" size={13} /></button>
          <button className="acr-iconbtn"><Icon name="dots-three" size={13} /></button>
        </div>
      </div>
      <div className="acr-chat-scroll">
        <div className="acr-chat-inner">
          <ChatBubble who="user" meta="2:14 PM">
            prerender a pricing page for a local AI IDE. three tiers, one-time purchase, your-keys framing.
          </ChatBubble>
          <ChatBubble who="agent" meta="claude-3-7 · 212 tok">
            Understood. I'll plan this in 14 steps — layout grid, hero, three tiers (Solo / Studio / Source), then wire the CTA. Starting with the scaffold.
          </ChatBubble>
          <ChatBubble who="agent" meta="tool · read_file">
            <span className="acr-tool-call">
              <Icon name="file-text" size={12} />
              <code>read colors_and_type.css</code>
              <span className="acr-tool-ok">212 tokens · ok</span>
            </span>
          </ChatBubble>
          <ChatBubble who="agent" meta="plan · step 2 of 14">
            <div className="acr-plan-mini">
              <div className="acr-plan-mini-step done"><Icon name="check" size={10} weight="bold" /><span>Read brand notes</span></div>
              <div className="acr-plan-mini-step done"><Icon name="check" size={10} weight="bold" /><span>Scaffold layout grid</span></div>
              <div className="acr-plan-mini-step run"><Icon name="circle-notch" size={10} /><span>Write hero section</span><span className="t">streaming · 1,840 tok/s</span></div>
              <div className="acr-plan-mini-step queued"><Icon name="circle" size={10} /><span>Compose pricing tiers</span></div>
              <div className="acr-plan-mini-step queued"><Icon name="circle" size={10} /><span>Wire CTA → waitlist</span></div>
            </div>
          </ChatBubble>
          <ChatBubble who="agent" meta="live · streaming">
            Writing hero copy now. See the prerender pane on the right — it's updating as tokens arrive.
            <span className="acr-typing"><span></span><span></span><span></span></span>
          </ChatBubble>
        </div>
      </div>
      <Composer />
    </div>
  );
}

function PlanStep({ n, title, status, detail }) {
  const statusCfg = {
    done: { cls: "done", icon: "check" },
    run: { cls: "run", icon: "circle-notch" },
    queued: { cls: "queued", icon: "circle" },
  }[status];
  return (
    <div className={`acr-step ${statusCfg.cls}`}>
      <div className="acr-step-marker">
        <Icon name={statusCfg.icon} size={12} weight={status === "done" ? "bold" : ""} />
      </div>
      <div className="acr-step-body">
        <div className="acr-step-title">
          <span className="acr-step-n">{String(n).padStart(2, "0")}</span>
          <span>{title}</span>
        </div>
        {detail && <div className="acr-step-detail">{detail}</div>}
      </div>
    </div>
  );
}

function PlanPanel() {
  return (
    <div className="acr-plan">
      <div className="acr-panel-head">
        <div className="acr-panel-title">
          <Icon name="list-checks" size={14} />
          <span>Plan</span>
          <span className="acr-badge mint">● running</span>
        </div>
        <div className="acr-panel-tools">
          <button className="acr-iconbtn"><Icon name="pause" size={13} /></button>
          <button className="acr-iconbtn"><Icon name="dots-three" size={13} /></button>
        </div>
      </div>
      <div className="acr-plan-body">
        <PlanStep n={1} title="Read brand notes" status="done" detail="colors_and_type.css · 212 tokens" />
        <PlanStep n={2} title="Scaffold layout grid" status="done" detail="12-col · 1200px max" />
        <PlanStep n={3} title="Write hero section" status="run" detail="streaming · 1,840 tok/s" />
        <PlanStep n={4} title="Compose pricing tiers" status="queued" />
        <PlanStep n={5} title="Wire CTA → waitlist" status="queued" />
      </div>
    </div>
  );
}

function PrerenderPane() {
  return (
    <div className="acr-prerender">
      <div className="acr-panel-head">
        <div className="acr-panel-title">
          <Icon name="browser" size={14} />
          <span>Prerender</span>
          <span className="acr-url-pill">localhost:4173 / pricing</span>
        </div>
        <div className="acr-panel-tools">
          <button className="acr-iconbtn"><Icon name="arrow-clockwise" size={13} /></button>
          <button className="acr-iconbtn"><Icon name="arrow-square-out" size={13} /></button>
          <button className="acr-iconbtn"><Icon name="device-mobile" size={13} /></button>
        </div>
      </div>
      <div className="acr-prerender-frame">
        <div className="acr-prerender-page">
          <div className="acr-prerender-nav">
            <span className="acr-prerender-brand">accuretta</span>
            <div className="acr-prerender-links">
              <span>product</span><span>pricing</span><span>docs</span>
            </div>
          </div>
          <div className="acr-prerender-hero">
            <div className="acr-prerender-eyebrow">Pricing · local-first</div>
            <h1 className="acr-prerender-h1">
              Pay for tokens.<br />Not for a <span className="hl">wrapper</span>.
            </h1>
            <p className="acr-prerender-sub">
              Accuretta runs on your machine. You bring the keys.<br />
              The app is a one-time purchase.
            </p>
            <div className="acr-prerender-tiers">
              <div className="acr-tier">
                <div className="t-name">Solo</div>
                <div className="t-price">$49</div>
                <div className="t-note">one time · your keys</div>
              </div>
              <div className="acr-tier hero">
                <div className="t-name">Studio</div>
                <div className="t-price">$149</div>
                <div className="t-note">one time · team seats</div>
              </div>
              <div className="acr-tier">
                <div className="t-name">Source</div>
                <div className="t-price">$––</div>
                <div className="t-note">self-compiled</div>
              </div>
            </div>
          </div>
          <div className="acr-cursor-ghost" />
        </div>
      </div>
      <div className="acr-prerender-foot">
        <span>● streaming · 3 of 5 sections</span>
        <span>1.2MB · 0 warnings</span>
      </div>
    </div>
  );
}

function Topbar() {
  return (
    <div className="acr-topbar">
      <div className="acr-top-path">
        <Icon name="folder-simple" size={12} />
        <span>~/projects/accuretta</span>
        <span className="sep">/</span>
        <span className="em">pricing.tsx</span>
      </div>
      <div className="acr-top-actions">
        <button className="acr-iconbtn"><Icon name="git-branch" size={13} /></button>
        <button className="acr-iconbtn"><Icon name="magnifying-glass" size={13} /></button>
        <button className="acr-iconbtn"><Icon name="bell" size={13} /></button>
        <div className="acr-avatar">mb</div>
      </div>
    </div>
  );
}

Object.assign(window, { Sidebar, FileTree, Composer, ChatView, ChatBubble, PlanPanel, PrerenderPane, Topbar, Icon });
