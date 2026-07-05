/*
 * Preflight — Hermes Idea Capture, dashboard plugin (frontend).
 *
 * A lightweight staging area for ideas/future work before they become Kanban
 * execution items. No build step: this is a plain IIFE that renders with the
 * React instance provided by the Hermes Plugin SDK.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React) {
    console.error("[preflight-idea-capture] Hermes Plugin SDK not available");
    return;
  }

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || React;
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useMemo = hooks.useMemo;
  var useCallback = hooks.useCallback;

  var BASE = "/api/plugins/preflight-idea-capture";
  var PRIORITY_META = {
    "": { label: "—", color: "#6b7280" },
    low: { label: "Low", color: "#64748b" },
    maybe: { label: "Maybe", color: "#0ea5e9" },
    high: { label: "High", color: "#f59e0b" },
    urgent: { label: "Urgent", color: "#ef4444" },
  };
  // Emoji for source types (labels come from the backend /config).
  var SOURCE_EMOJI = {
    reddit: "👽",
    twitter: "🐦",
    hackernews: "📰",
    email: "✉️",
    slack: "💬",
    client: "💼",
    internal: "🏠",
    web: "🌐",
    other: "🔖",
  };

  // ----------------------------------------------------------------------- //
  // Data access
  // ----------------------------------------------------------------------- //

  function req(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    if (SDK.fetchJSON) return SDK.fetchJSON(BASE + path, opts);
    return fetch(BASE + path, opts).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // ----------------------------------------------------------------------- //
  // Small utilities
  // ----------------------------------------------------------------------- //

  function timeAgo(iso) {
    if (!iso) return "";
    if (SDK.utils && SDK.utils.timeAgo) {
      try { return SDK.utils.timeAgo(iso); } catch (e) { /* fall through */ }
    }
    var then = new Date(iso).getTime();
    if (isNaN(then)) return "";
    var s = Math.floor((Date.now() - then) / 1000);
    if (s < 60) return "just now";
    var m = Math.floor(s / 60); if (m < 60) return m + "m ago";
    var hr = Math.floor(m / 60); if (hr < 24) return hr + "h ago";
    var d = Math.floor(hr / 24); if (d < 30) return d + "d ago";
    var mo = Math.floor(d / 30); if (mo < 12) return mo + "mo ago";
    return Math.floor(mo / 12) + "y ago";
  }

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return url; }
  }

  // ---- Safe, dependency-free markdown-lite renderer --------------------- //
  // Only produces React elements (never raw HTML), and only linkifies http(s)
  // URLs — so there is no injection surface.

  function inlineNodes(text, kp) {
    var out = [];
    var re = /(\*\*([^*]+)\*\*)|(`([^`]+)`)|(\[([^\]]+)\]\((https?:\/\/[^\s)]+)\))|((?:https?:\/\/)[^\s)]+)/g;
    var last = 0, m, idx = 0;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[2] !== undefined) out.push(h("strong", { key: kp + idx++ }, m[2]));
      else if (m[4] !== undefined) out.push(h("code", { key: kp + idx++, className: "ic-code" }, m[4]));
      else if (m[6] !== undefined) out.push(h("a", { key: kp + idx++, href: m[7], target: "_blank", rel: "noreferrer noopener", className: "ic-link" }, m[6]));
      else if (m[8] !== undefined) out.push(h("a", { key: kp + idx++, href: m[8], target: "_blank", rel: "noreferrer noopener", className: "ic-link" }, m[8]));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function renderMarkdown(md) {
    var lines = (md || "").replace(/\r\n/g, "\n").split("\n");
    var blocks = [], list = null, k = 0;
    function flush() {
      if (list) { blocks.push(h("ul", { key: "ul" + k++, className: "ic-md-ul" }, list)); list = null; }
    }
    lines.forEach(function (line, li) {
      var t = line.replace(/\s+$/, "");
      var hm = /^(#{1,3})\s+(.*)$/.exec(t);
      var bm = /^[-*]\s+(.*)$/.exec(t);
      if (hm) { flush(); blocks.push(h("h" + (hm[1].length + 2), { key: "h" + li, className: "ic-md-h" }, inlineNodes(hm[2], "h" + li + "-"))); }
      else if (bm) { if (!list) list = []; list.push(h("li", { key: "li" + li }, inlineNodes(bm[1], "li" + li + "-"))); }
      else if (t.trim() === "") { flush(); }
      else { flush(); blocks.push(h("p", { key: "p" + li, className: "ic-md-p" }, inlineNodes(t, "p" + li + "-"))); }
    });
    flush();
    if (!blocks.length) return [h("p", { key: "empty", className: "ic-muted" }, "No notes yet.")];
    return blocks;
  }

  function statusDef(statuses, id) {
    for (var i = 0; i < statuses.length; i++) if (statuses[i].id === id) return statuses[i];
    return { id: id, label: id || "—", color: "#6b7280" };
  }

  function catName(categories, id) {
    for (var i = 0; i < categories.length; i++) if (categories[i].id === id) return categories[i].name;
    return id ? id : "Uncategorized";
  }

  function subName(categories, catId, subId) {
    for (var i = 0; i < categories.length; i++) {
      if (categories[i].id === catId) {
        var subs = categories[i].subcategories || [];
        for (var j = 0; j < subs.length; j++) if (subs[j].id === subId) return subs[j].name;
      }
    }
    return subId || "";
  }

  function sourceLabel(sourceTypes, id) {
    for (var i = 0; i < (sourceTypes || []).length; i++) if (sourceTypes[i].id === id) return sourceTypes[i].label;
    return id || "";
  }

  // ----------------------------------------------------------------------- //
  // Generic modal
  // ----------------------------------------------------------------------- //

  function Modal(props) {
    return h("div", { className: "ic-modal-overlay", onClick: props.onClose },
      h("div", { className: "ic-modal", onClick: function (e) { e.stopPropagation(); } },
        h("div", { className: "ic-modal-head" },
          h("div", { className: "ic-modal-title" }, props.title),
          h("button", { className: "ic-icon-btn", onClick: props.onClose }, "✕")
        ),
        h("div", { className: "ic-modal-body" }, props.children)
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Presentational components
  // ----------------------------------------------------------------------- //

  function StatusBadge(props) {
    var d = statusDef(props.statuses, props.status);
    return h("span", { className: "ic-badge", style: { color: d.color, borderColor: d.color, background: d.color + "1f" } }, d.label);
  }

  function IdeaCard(props) {
    var idea = props.idea;
    var src = idea.source_url;
    var prio = PRIORITY_META[idea.priority || ""] || PRIORITY_META[""];
    return h("div", {
      className: "ic-card" + (props.active ? " ic-card-active" : ""),
      onClick: props.onClick,
    },
      h("div", { className: "ic-card-top" },
        h("div", { className: "ic-card-title" }, idea.title || "(untitled)"),
        h(StatusBadge, { status: idea.status, statuses: props.statuses })
      ),
      idea.summary ? h("div", { className: "ic-card-summary" }, idea.summary) : null,
      h("div", { className: "ic-card-meta" },
        h("span", { className: "ic-chip" }, catName(props.categories, idea.category)),
        idea.subcategory ? h("span", { className: "ic-chip ic-chip-sub" }, subName(props.categories, idea.category, idea.subcategory)) : null,
        idea.source_type ? h("span", { className: "ic-chip ic-chip-src" }, (SOURCE_EMOJI[idea.source_type] || "") + " " + sourceLabel(props.sourceTypes, idea.source_type)) : null,
        idea.priority ? h("span", { className: "ic-prio", style: { color: prio.color } }, "● " + prio.label) : null,
        (idea.tags || []).map(function (t, i) { return h("span", { key: i, className: "ic-tag" }, "#" + t); }),
        h("span", { className: "ic-spacer" }),
        src ? h("a", { className: "ic-src", href: src, target: "_blank", rel: "noreferrer noopener", onClick: function (e) { e.stopPropagation(); } }, "↗ " + hostOf(src)) : null,
        h("span", { className: "ic-ago" }, timeAgo(idea.updated_at))
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Sidebar (category / subcategory tree)
  // ----------------------------------------------------------------------- //

  function Sidebar(props) {
    var categories = props.categories, ideas = props.ideas, filter = props.filter, pick = props.onPick;

    function countCat(id) { return ideas.filter(function (i) { return i.category === id; }).length; }
    function countSub(cid, sid) { return ideas.filter(function (i) { return i.category === cid && i.subcategory === sid; }).length; }

    var rows = [];
    rows.push(h("button", {
      key: "all",
      className: "ic-tree-item" + (!filter.category ? " ic-tree-active" : ""),
      onClick: function () { pick(null, null); },
    }, h("span", null, "All ideas"), h("span", { className: "ic-count" }, ideas.length)));

    categories.forEach(function (c) {
      var open = filter.category === c.id;
      rows.push(h("button", {
        key: c.id,
        className: "ic-tree-item ic-tree-cat" + (open && !filter.subcategory ? " ic-tree-active" : ""),
        onClick: function () { pick(c.id, null); },
      },
        h("span", null, (c.subcategories && c.subcategories.length ? (open ? "▾ " : "▸ ") : "") + c.name),
        h("span", { className: "ic-count" }, countCat(c.id))
      ));
      if (open && c.subcategories) {
        c.subcategories.forEach(function (s) {
          rows.push(h("button", {
            key: c.id + "/" + s.id,
            className: "ic-tree-item ic-tree-sub" + (filter.subcategory === s.id ? " ic-tree-active" : ""),
            onClick: function () { pick(c.id, s.id); },
          }, h("span", null, s.name), h("span", { className: "ic-count" }, countSub(c.id, s.id))));
        });
      }
    });

    return h("aside", { className: "ic-sidebar" }, rows);
  }

  // ----------------------------------------------------------------------- //
  // Quick-add form
  // ----------------------------------------------------------------------- //

  function QuickAdd(props) {
    var categories = props.categories, statuses = props.statuses;
    var sourceTypes = props.sourceTypes || [];
    var templates = props.templates || [];
    var s = useState({ title: "", category: props.defaultCategory || "", subcategory: props.defaultSubcategory || "", status: statuses[0] ? statuses[0].id : "", priority: "", source_url: "", source_type: "", tags: "", notes_markdown: "" });
    var form = s[0], setForm = s[1];
    var busy = useState(false); var isBusy = busy[0], setBusy = busy[1];
    var tpl = useState(""); var tplId = tpl[0], setTplId = tpl[1];

    function set(k, v) {
      var next = {}; next[k] = v;
      if (k === "category") next.subcategory = "";
      setForm(Object.assign({}, form, next));
    }
    function applyTemplate(id) {
      setTplId(id);
      var t = templates.filter(function (x) { return x.id === id; })[0];
      if (!t) return;
      setForm(Object.assign({}, form, {
        status: t.status || form.status,
        priority: t.priority || "",
        source_type: t.source_type || "",
        tags: (t.tags || []).join(", "),
        notes_markdown: t.notes_markdown || "",
      }));
    }
    var cat = categories.filter(function (c) { return c.id === form.category; })[0];
    var subs = (cat && cat.subcategories) || [];

    function submit(e) {
      e.preventDefault();
      if (!form.title.trim()) return;
      setBusy(true);
      props.onCreate({
        title: form.title,
        category: form.category || null,
        subcategory: form.subcategory || null,
        status: form.status,
        priority: form.priority,
        source_url: form.source_url,
        source_type: form.source_type,
        notes_markdown: form.notes_markdown,
        tags: form.tags.split(",").map(function (t) { return t.trim(); }).filter(Boolean),
      }).then(function () { setBusy(false); }, function () { setBusy(false); });
    }

    return h("form", { className: "ic-quickadd", onSubmit: submit },
      templates.length ? h("div", { className: "ic-qa-row" },
        h("select", { className: "ic-input", value: tplId, onChange: function (e) { applyTemplate(e.target.value); } },
          h("option", { value: "" }, "Start from template…"),
          templates.map(function (t) { return h("option", { key: t.id, value: t.id }, t.name); })
        )
      ) : null,
      h("div", { className: "ic-qa-row" },
        h("input", { className: "ic-input ic-qa-title", placeholder: "New idea title…", value: form.title, autoFocus: true, onChange: function (e) { set("title", e.target.value); } })
      ),
      h("div", { className: "ic-qa-row" },
        h("select", { className: "ic-input", value: form.category, onChange: function (e) { set("category", e.target.value); } },
          h("option", { value: "" }, "Category…"),
          categories.map(function (c) { return h("option", { key: c.id, value: c.id }, c.name); })
        ),
        h("select", { className: "ic-input", value: form.subcategory, disabled: !subs.length, onChange: function (e) { set("subcategory", e.target.value); } },
          h("option", { value: "" }, subs.length ? "Subcategory…" : "—"),
          subs.map(function (sc) { return h("option", { key: sc.id, value: sc.id }, sc.name); })
        ),
        h("select", { className: "ic-input", value: form.status, onChange: function (e) { set("status", e.target.value); } },
          statuses.map(function (st) { return h("option", { key: st.id, value: st.id }, st.label); })
        ),
        h("select", { className: "ic-input", value: form.priority, onChange: function (e) { set("priority", e.target.value); } },
          Object.keys(PRIORITY_META).map(function (p) { return h("option", { key: p || "none", value: p }, "Priority: " + PRIORITY_META[p].label); })
        )
      ),
      h("div", { className: "ic-qa-row" },
        h("select", { className: "ic-input", value: form.source_type, onChange: function (e) { set("source_type", e.target.value); } },
          sourceTypes.map(function (st) { return h("option", { key: st.id || "none", value: st.id }, st.id ? "Source: " + st.label : "Source type…"); })
        ),
        h("input", { className: "ic-input", placeholder: "Source URL (optional)", value: form.source_url, onChange: function (e) { set("source_url", e.target.value); } }),
        h("input", { className: "ic-input", placeholder: "tags, comma, separated", value: form.tags, onChange: function (e) { set("tags", e.target.value); } })
      ),
      h("textarea", { className: "ic-input ic-qa-notes", placeholder: "Notes (markdown ok)…", value: form.notes_markdown, onChange: function (e) { set("notes_markdown", e.target.value); } }),
      h("div", { className: "ic-qa-actions" },
        h("button", { type: "button", className: "ic-btn ic-btn-ghost", onClick: props.onCancel }, "Cancel"),
        h("button", { type: "submit", className: "ic-btn ic-btn-primary", disabled: isBusy || !form.title.trim() }, isBusy ? "Adding…" : "Add idea")
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Quick capture — friction-free single-line entry (stays open for streaks)
  // ----------------------------------------------------------------------- //

  function QuickCapture(props) {
    var t = useState(""); var title = t[0], setTitle = t[1];
    var s = useState(""); var src = s[0], setSrc = s[1];
    var cnt = useState(0); var count = cnt[0], setCount = cnt[1];
    var busy = useState(false); var isBusy = busy[0], setBusy = busy[1];

    function submit(e) {
      e.preventDefault();
      if (!title.trim() || isBusy) return;
      setBusy(true);
      props.onCreate({
        title: title,
        category: props.defaultCategory || null,
        subcategory: props.defaultSubcategory || null,
        source_type: src,
      }).then(function () { setTitle(""); setCount(count + 1); setBusy(false); },
        function () { setBusy(false); });
    }

    return h("form", { className: "ic-capture", onSubmit: submit },
      h("span", { className: "ic-capture-bolt" }, "⚡"),
      h("input", { className: "ic-input ic-capture-input", placeholder: "Quick capture — type an idea, press Enter…", value: title, autoFocus: true, onChange: function (e) { setTitle(e.target.value); } }),
      h("select", { className: "ic-input ic-capture-src", value: src, onChange: function (e) { setSrc(e.target.value); } },
        (props.sourceTypes || []).map(function (st) { return h("option", { key: st.id || "none", value: st.id }, st.id ? st.label : "Source…"); })
      ),
      h("button", { className: "ic-btn ic-btn-primary", type: "submit", disabled: !title.trim() || isBusy }, "Capture"),
      count ? h("span", { className: "ic-capture-count" }, "Added " + count + " ✓") : null,
      h("button", { className: "ic-icon-btn", type: "button", onClick: props.onClose, title: "Close" }, "✕")
    );
  }

  // ----------------------------------------------------------------------- //
  // Detail pane
  // ----------------------------------------------------------------------- //

  function DetailPane(props) {
    var idea = props.idea, categories = props.categories, statuses = props.statuses;
    var ed = useState(null); var draft = ed[0], setDraft = ed[1];
    var up = useState(""); var upBody = up[0], setUpBody = up[1];
    var au = useState("me"); var author = au[0], setAuthor = au[1];

    useEffect(function () {
      setDraft({
        title: idea.title || "",
        summary: idea.summary || "",
        source_url: idea.source_url || "",
        notes_markdown: idea.notes_markdown || "",
        tags: (idea.tags || []).join(", "),
      });
      setUpBody("");
    }, [idea.id]);

    if (!draft) return null;
    var cat = categories.filter(function (c) { return c.id === idea.category; })[0];
    var subs = (cat && cat.subcategories) || [];

    var dirty = draft.title !== (idea.title || "") ||
      draft.summary !== (idea.summary || "") ||
      draft.source_url !== (idea.source_url || "") ||
      draft.notes_markdown !== (idea.notes_markdown || "") ||
      draft.tags !== (idea.tags || []).join(", ");

    function setD(k, v) { var n = {}; n[k] = v; setDraft(Object.assign({}, draft, n)); }
    function saveText() {
      props.onPatch(idea.id, {
        title: draft.title, summary: draft.summary, source_url: draft.source_url,
        notes_markdown: draft.notes_markdown,
        tags: draft.tags.split(",").map(function (t) { return t.trim(); }).filter(Boolean),
      });
    }
    function postUpdate() {
      if (!upBody.trim()) return;
      props.onAddUpdate(idea.id, upBody, author).then(function () { setUpBody(""); });
    }

    var updates = (idea.updates || []).slice().reverse();

    return h("section", { className: "ic-detail" },
      h("div", { className: "ic-detail-head" },
        h("input", { className: "ic-title-input", value: draft.title, onChange: function (e) { setD("title", e.target.value); } }),
        h("button", { className: "ic-icon-btn", title: "Close", onClick: props.onClose }, "✕")
      ),

      h("div", { className: "ic-detail-controls" },
        h("label", { className: "ic-field" }, h("span", null, "Status"),
          h("select", { className: "ic-input", value: idea.status || "", onChange: function (e) { props.onPatch(idea.id, { status: e.target.value }); } },
            statuses.map(function (st) { return h("option", { key: st.id, value: st.id }, st.label); })
          )
        ),
        h("label", { className: "ic-field" }, h("span", null, "Priority"),
          h("select", { className: "ic-input", value: idea.priority || "", onChange: function (e) { props.onPatch(idea.id, { priority: e.target.value }); } },
            Object.keys(PRIORITY_META).map(function (p) { return h("option", { key: p || "none", value: p }, PRIORITY_META[p].label); })
          )
        ),
        h("label", { className: "ic-field" }, h("span", null, "Source type"),
          h("select", { className: "ic-input", value: idea.source_type || "", onChange: function (e) { props.onPatch(idea.id, { source_type: e.target.value }); } },
            (props.sourceTypes || []).map(function (st) { return h("option", { key: st.id || "none", value: st.id }, st.label); })
          )
        ),
        h("label", { className: "ic-field" }, h("span", null, "Category"),
          h("select", { className: "ic-input", value: idea.category || "", onChange: function (e) { props.onPatch(idea.id, { category: e.target.value || null, subcategory: null }); } },
            h("option", { value: "" }, "Uncategorized"),
            categories.map(function (c) { return h("option", { key: c.id, value: c.id }, c.name); })
          )
        ),
        h("label", { className: "ic-field" }, h("span", null, "Subcategory"),
          h("select", { className: "ic-input", value: idea.subcategory || "", disabled: !subs.length, onChange: function (e) { props.onPatch(idea.id, { subcategory: e.target.value || null }); } },
            h("option", { value: "" }, "—"),
            subs.map(function (sc) { return h("option", { key: sc.id, value: sc.id }, sc.name); })
          )
        )
      ),

      h("label", { className: "ic-field ic-field-full" }, h("span", null, "Summary"),
        h("input", { className: "ic-input", value: draft.summary, placeholder: "Short plain-English summary", onChange: function (e) { setD("summary", e.target.value); } })
      ),
      h("label", { className: "ic-field ic-field-full" }, h("span", null, "Source URL"),
        h("input", { className: "ic-input", value: draft.source_url, placeholder: "https://…", onChange: function (e) { setD("source_url", e.target.value); } })
      ),
      h("label", { className: "ic-field ic-field-full" }, h("span", null, "Tags"),
        h("input", { className: "ic-input", value: draft.tags, placeholder: "comma, separated", onChange: function (e) { setD("tags", e.target.value); } })
      ),

      h("div", { className: "ic-notes-block" },
        h("div", { className: "ic-notes-head" }, h("span", null, "Notes"), h("span", { className: "ic-muted ic-small" }, "markdown")),
        h("textarea", { className: "ic-input ic-notes-edit", value: draft.notes_markdown, placeholder: "Write notes in markdown…", onChange: function (e) { setD("notes_markdown", e.target.value); } }),
        h("div", { className: "ic-notes-preview" }, renderMarkdown(draft.notes_markdown))
      ),

      h("div", { className: "ic-detail-actions" },
        h("button", { className: "ic-btn ic-btn-primary", disabled: !dirty, onClick: saveText }, dirty ? "Save changes" : "Saved"),
        h("button", { className: "ic-btn ic-btn-ghost", title: "Generate a Kanban card draft to copy (nothing is dispatched automatically).", onClick: function () { props.onPromote(idea.id); } },
          idea.promoted_to_kanban ? "View Kanban draft" : "Draft Kanban card"),
        idea.promoted_to_kanban ? h("span", { className: "ic-drafted" }, "✓ drafted " + timeAgo(idea.promoted_to_kanban.drafted_at)) : null,
        h("span", { className: "ic-spacer" }),
        h("button", { className: "ic-btn ic-btn-danger", onClick: function () { if (window.confirm("Delete this idea permanently?")) props.onDelete(idea.id); } }, "Delete")
      ),

      h("div", { className: "ic-updates" },
        h("div", { className: "ic-updates-head" }, "Updates"),
        h("div", { className: "ic-update-add" },
          h("input", { className: "ic-input ic-update-by", value: author, title: "Author", onChange: function (e) { setAuthor(e.target.value); } }),
          h("input", { className: "ic-input ic-update-body", value: upBody, placeholder: "Add an update…", onKeyDown: function (e) { if (e.key === "Enter") postUpdate(); }, onChange: function (e) { setUpBody(e.target.value); } }),
          h("button", { className: "ic-btn ic-btn-primary", onClick: postUpdate, disabled: !upBody.trim() }, "Log")
        ),
        updates.length ? updates.map(function (u, i) {
          return h("div", { key: i, className: "ic-update" + (u.by === "system" ? " ic-update-system" : "") },
            h("div", { className: "ic-update-meta" }, h("strong", null, u.by || "?"), h("span", { className: "ic-ago" }, timeAgo(u.at))),
            h("div", { className: "ic-update-text" }, u.body)
          );
        }) : h("div", { className: "ic-muted ic-small" }, "No updates yet."),
      ),

      h("div", { className: "ic-meta-footer" },
        "Created " + timeAgo(idea.created_at) + " · Updated " + timeAgo(idea.updated_at) + " · " + idea.id
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Manage view (categories + statuses)
  // ----------------------------------------------------------------------- //

  function ManageView(props) {
    var config = props.config, A = props.actions;
    var sourceTypes = config.source_types || [];
    var nc = useState(""); var newCat = nc[0], setNewCat = nc[1];
    var ns = useState({ label: "", color: "#6b7280" }); var newStatus = ns[0], setNewStatus = ns[1];
    var subDrafts = useState({}); var subDraft = subDrafts[0], setSubDraft = subDrafts[1];
    var nt = useState({ name: "", source_type: "", status: "", priority: "", tags: "", notes_markdown: "" });
    var newTpl = nt[0], setNewTpl = nt[1];
    var im = useState({ mode: "merge", busy: false, msg: "" }); var imp = im[0], setImp = im[1];

    function subVal(id) { return subDraft[id] || ""; }
    function setSubVal(id, v) { var n = Object.assign({}, subDraft); n[id] = v; setSubDraft(n); }

    return h("div", { className: "ic-manage" },
      h("div", { className: "ic-manage-col" },
        h("h3", { className: "ic-manage-h" }, "Categories"),
        h("div", { className: "ic-row-add" },
          h("input", { className: "ic-input", placeholder: "New category name", value: newCat, onChange: function (e) { setNewCat(e.target.value); } }),
          h("button", { className: "ic-btn ic-btn-primary", disabled: !newCat.trim(), onClick: function () { A.addCategory(newCat).then(function () { setNewCat(""); }); } }, "Add")
        ),
        config.categories.map(function (c) {
          return h("div", { key: c.id, className: "ic-manage-cat" },
            h("div", { className: "ic-manage-cat-head" },
              h("input", { className: "ic-input ic-inline-edit", defaultValue: c.name, onBlur: function (e) { if (e.target.value.trim() && e.target.value !== c.name) A.renameCategory(c.id, e.target.value); } }),
              h("button", { className: "ic-icon-btn ic-danger", title: "Delete category", onClick: function () { if (window.confirm('Delete category "' + c.name + '"? Ideas keep their content and show as Uncategorized.')) A.deleteCategory(c.id); } }, "🗑")
            ),
            h("div", { className: "ic-sub-list" },
              (c.subcategories || []).map(function (s) {
                return h("div", { key: s.id, className: "ic-sub-row" },
                  h("input", { className: "ic-input ic-inline-edit", defaultValue: s.name, onBlur: function (e) { if (e.target.value.trim() && e.target.value !== s.name) A.renameSub(c.id, s.id, e.target.value); } }),
                  h("button", { className: "ic-icon-btn ic-danger", title: "Delete subcategory", onClick: function () { A.deleteSub(c.id, s.id); } }, "✕")
                );
              }),
              h("div", { className: "ic-sub-row" },
                h("input", { className: "ic-input", placeholder: "+ subcategory", value: subVal(c.id), onChange: function (e) { setSubVal(c.id, e.target.value); }, onKeyDown: function (e) { if (e.key === "Enter" && subVal(c.id).trim()) { A.addSub(c.id, subVal(c.id)).then(function () { setSubVal(c.id, ""); }); } } }),
                h("button", { className: "ic-btn ic-btn-ghost", disabled: !subVal(c.id).trim(), onClick: function () { A.addSub(c.id, subVal(c.id)).then(function () { setSubVal(c.id, ""); }); } }, "Add")
              )
            )
          );
        })
      ),

      h("div", { className: "ic-manage-col" },
        h("h3", { className: "ic-manage-h" }, "Statuses"),
        h("div", { className: "ic-row-add" },
          h("input", { className: "ic-color", type: "color", value: newStatus.color, onChange: function (e) { setNewStatus(Object.assign({}, newStatus, { color: e.target.value })); } }),
          h("input", { className: "ic-input", placeholder: "New status label", value: newStatus.label, onChange: function (e) { setNewStatus(Object.assign({}, newStatus, { label: e.target.value })); } }),
          h("button", { className: "ic-btn ic-btn-primary", disabled: !newStatus.label.trim(), onClick: function () { A.addStatus(newStatus).then(function () { setNewStatus({ label: "", color: "#6b7280" }); }); } }, "Add")
        ),
        config.statuses.map(function (st) {
          return h("div", { key: st.id, className: "ic-status-row" },
            h("input", { className: "ic-color", type: "color", value: st.color, onChange: function (e) { A.editStatus(st.id, { color: e.target.value }); } }),
            h("input", { className: "ic-input ic-inline-edit", defaultValue: st.label, onBlur: function (e) { if (e.target.value.trim() && e.target.value !== st.label) A.editStatus(st.id, { label: e.target.value }); } }),
            h("span", { className: "ic-badge", style: { color: st.color, borderColor: st.color, background: st.color + "1f" } }, st.label),
            h("button", { className: "ic-icon-btn ic-danger", title: "Delete status", onClick: function () { A.deleteStatus(st.id); } }, "✕")
          );
        }),
        h("p", { className: "ic-muted ic-small" }, "Deleting a status does not change ideas already using it; reassign them from the detail pane.")
      ),

      h("div", { className: "ic-manage-col" },
        h("h3", { className: "ic-manage-h" }, "Templates"),
        h("p", { className: "ic-muted ic-small" }, "Prefill the new-idea form (status, priority, source type, tags, notes)."),
        (config.templates || []).map(function (t) {
          return h("div", { key: t.id, className: "ic-tpl-row" },
            h("span", { className: "ic-tpl-name" }, t.name),
            t.source_type ? h("span", { className: "ic-chip ic-chip-src" }, (SOURCE_EMOJI[t.source_type] || "") + " " + sourceLabel(sourceTypes, t.source_type)) : null,
            t.status ? h("span", { className: "ic-chip" }, t.status) : null,
            h("span", { className: "ic-spacer" }),
            h("button", { className: "ic-icon-btn ic-danger", title: "Delete template", onClick: function () { A.deleteTemplate(t.id); } }, "✕")
          );
        }),
        h("div", { className: "ic-tpl-add" },
          h("input", { className: "ic-input", placeholder: "Template name", value: newTpl.name, onChange: function (e) { setNewTpl(Object.assign({}, newTpl, { name: e.target.value })); } }),
          h("div", { className: "ic-qa-row" },
            h("select", { className: "ic-input", value: newTpl.source_type, onChange: function (e) { setNewTpl(Object.assign({}, newTpl, { source_type: e.target.value })); } },
              sourceTypes.map(function (st) { return h("option", { key: st.id || "none", value: st.id }, st.id ? st.label : "Source type…"); })
            ),
            h("select", { className: "ic-input", value: newTpl.status, onChange: function (e) { setNewTpl(Object.assign({}, newTpl, { status: e.target.value })); } },
              h("option", { value: "" }, "Status…"),
              config.statuses.map(function (st) { return h("option", { key: st.id, value: st.id }, st.label); })
            ),
            h("select", { className: "ic-input", value: newTpl.priority, onChange: function (e) { setNewTpl(Object.assign({}, newTpl, { priority: e.target.value })); } },
              Object.keys(PRIORITY_META).map(function (p) { return h("option", { key: p || "none", value: p }, "Priority: " + PRIORITY_META[p].label); })
            )
          ),
          h("input", { className: "ic-input", placeholder: "tags, comma, separated", value: newTpl.tags, onChange: function (e) { setNewTpl(Object.assign({}, newTpl, { tags: e.target.value })); } }),
          h("textarea", { className: "ic-input ic-qa-notes", placeholder: "Notes scaffold (markdown)…", value: newTpl.notes_markdown, onChange: function (e) { setNewTpl(Object.assign({}, newTpl, { notes_markdown: e.target.value })); } }),
          h("button", { className: "ic-btn ic-btn-primary", disabled: !newTpl.name.trim(), onClick: function () {
            A.addTemplate({ name: newTpl.name, source_type: newTpl.source_type, status: newTpl.status, priority: newTpl.priority, notes_markdown: newTpl.notes_markdown, tags: newTpl.tags.split(",").map(function (x) { return x.trim(); }).filter(Boolean) })
              .then(function () { setNewTpl({ name: "", source_type: "", status: "", priority: "", tags: "", notes_markdown: "" }); });
          } }, "Add template")
        )
      ),

      h("div", { className: "ic-manage-col" },
        h("h3", { className: "ic-manage-h" }, "Data"),
        h("p", { className: "ic-muted ic-small" }, "Back up or move everything (categories, statuses, templates, and all ideas) as one JSON file."),
        h("div", { className: "ic-row-add" },
          h("button", { className: "ic-btn", onClick: A.exportData }, "⬇ Export JSON")
        ),
        h("div", { className: "ic-data-import" },
          h("label", { className: "ic-field" }, h("span", null, "Import mode"),
            h("select", { className: "ic-input", value: imp.mode, onChange: function (e) { setImp(Object.assign({}, imp, { mode: e.target.value })); } },
              h("option", { value: "merge" }, "Merge (add/overwrite, keep the rest)"),
              h("option", { value: "replace" }, "Replace (wipe existing ideas first)")
            )
          ),
          h("input", {
            className: "ic-input", type: "file", accept: "application/json,.json", disabled: imp.busy,
            onChange: function (e) {
              var file = e.target.files && e.target.files[0];
              if (!file) return;
              if (imp.mode === "replace" && !window.confirm("Replace mode wipes ALL existing ideas before importing. Continue?")) { e.target.value = ""; return; }
              setImp(Object.assign({}, imp, { busy: true, msg: "" }));
              var input = e.target;
              file.text().then(function (text) {
                var bundle; try { bundle = JSON.parse(text); } catch (err) { setImp(Object.assign({}, imp, { busy: false, msg: "Invalid JSON file" })); input.value = ""; return null; }
                return A.importData(imp.mode, bundle).then(function (r) {
                  setImp(Object.assign({}, imp, { busy: false, msg: "Imported " + (r.ideas_written || 0) + " ideas" + (r.config_updated ? " + config" : "") }));
                  input.value = "";
                });
              }).catch(function (err) { setImp(Object.assign({}, imp, { busy: false, msg: "Import failed: " + (err && err.message || err) })); input.value = ""; });
            }
          }),
          imp.msg ? h("div", { className: "ic-muted ic-small" }, imp.msg) : null
        )
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Root
  // ----------------------------------------------------------------------- //

  function App() {
    var cfgS = useState({ categories: [], statuses: [], priorities: [] }); var config = cfgS[0], setConfig = cfgS[1];
    var idS = useState([]); var ideas = idS[0], setIdeas = idS[1];
    var ld = useState(true); var loading = ld[0], setLoading = ld[1];
    var er = useState(null); var error = er[0], setError = er[1];
    var vw = useState("ideas"); var view = vw[0], setView = vw[1];
    var flt = useState({ category: null, subcategory: null, status: "", source_type: "", q: "", sort: "updated" });
    var filter = flt[0], setFilter = flt[1];
    var sel = useState(null); var selectedId = sel[0], setSelectedId = sel[1];
    var det = useState(null); var detail = det[0], setDetail = det[1];
    var qa = useState(false); var quickOpen = qa[0], setQuickOpen = qa[1];
    var qc = useState(false); var captureOpen = qc[0], setCaptureOpen = qc[1];
    var dm = useState(null); var draft = dm[0], setDraft = dm[1];

    var loadConfig = useCallback(function () {
      return req("GET", "/config").then(setConfig);
    }, []);
    var loadIdeas = useCallback(function () {
      return req("GET", "/ideas").then(function (r) { setIdeas(r.ideas || []); });
    }, []);

    useEffect(function () {
      Promise.all([loadConfig(), loadIdeas()])
        .then(function () { setLoading(false); })
        .catch(function (e) { setError(String(e && e.message || e)); setLoading(false); });
    }, []);

    useEffect(function () {
      if (!selectedId) { setDetail(null); return; }
      var live = true;
      req("GET", "/ideas/" + selectedId).then(function (d) { if (live) setDetail(d); }).catch(function () { if (live) setDetail(null); });
      return function () { live = false; };
    }, [selectedId]);

    // Derived, fully client-side filtered + sorted list.
    var visible = useMemo(function () {
      var out = ideas.filter(function (i) {
        if (filter.category && i.category !== filter.category) return false;
        if (filter.subcategory && i.subcategory !== filter.subcategory) return false;
        if (filter.status && i.status !== filter.status) return false;
        if (filter.source_type && i.source_type !== filter.source_type) return false;
        if (filter.q) {
          var q = filter.q.toLowerCase();
          var hay = (i.title || "") + " " + (i.summary || "") + " " + (i.tags || []).join(" ");
          if (hay.toLowerCase().indexOf(q) === -1) return false;
        }
        return true;
      });
      out.sort(function (a, b) {
        if (filter.sort === "title") return (a.title || "").localeCompare(b.title || "");
        if (filter.sort === "created") return (b.created_at || "").localeCompare(a.created_at || "");
        return (b.updated_at || "").localeCompare(a.updated_at || "");
      });
      return out;
    }, [ideas, filter]);

    function refreshAfter(p) {
      return p.then(function (res) { loadIdeas(); return res; }, function (e) { setError(String(e && e.message || e)); throw e; });
    }

    // Idea actions
    function createIdea(form) {
      return refreshAfter(req("POST", "/ideas", form)).then(function (idea) {
        setQuickOpen(false);
        setSelectedId(idea.id);
        setDetail(idea);
        return idea;
      });
    }
    function patchIdea(id, patch) {
      return refreshAfter(req("PATCH", "/ideas/" + id, patch)).then(function (idea) { setDetail(idea); return idea; });
    }
    function deleteIdea(id) {
      return refreshAfter(req("DELETE", "/ideas/" + id)).then(function () { setSelectedId(null); setDetail(null); });
    }
    function addUpdate(id, body, by) {
      return refreshAfter(req("POST", "/ideas/" + id + "/updates", { body: body, by: by })).then(function (idea) { setDetail(idea); return idea; });
    }
    // Quick capture: file an idea without stealing focus into the detail pane.
    function captureIdea(form) {
      return refreshAfter(req("POST", "/ideas", form));
    }
    function promoteDraft(id) {
      return req("POST", "/ideas/" + id + "/promote-draft", {}).then(function (r) {
        setDetail(r.idea); loadIdeas(); setDraft(r.draft); return r;
      }, function (e) { setError(String(e && e.message || e)); });
    }
    function exportData() {
      return req("GET", "/export").then(function (d) {
        try {
          var blob = new Blob([JSON.stringify(d, null, 2)], { type: "application/json" });
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url; a.download = "idea-capture-export.json";
          document.body.appendChild(a); a.click(); document.body.removeChild(a);
          URL.revokeObjectURL(url);
        } catch (e) { setError("Export failed: " + (e && e.message || e)); }
      }, function (e) { setError(String(e && e.message || e)); });
    }
    function importData(mode, bundle) {
      var payload = { mode: mode, config: bundle && bundle.config, ideas: bundle && bundle.ideas };
      return req("POST", "/import", payload).then(function (r) {
        loadConfig(); loadIdeas(); setSelectedId(null);
        return r;
      });
    }

    // Config actions
    function afterCfg(p) { return p.then(function (r) { loadConfig(); return r; }, function (e) { setError(String(e && e.message || e)); throw e; }); }
    var actions = {
      addCategory: function (name) { return afterCfg(req("POST", "/categories", { name: name })); },
      renameCategory: function (id, name) { return afterCfg(req("PATCH", "/categories/" + id, { name: name })); },
      deleteCategory: function (id) { return afterCfg(req("DELETE", "/categories/" + id)); },
      addSub: function (cid, name) { return afterCfg(req("POST", "/categories/" + cid + "/subcategories", { name: name })); },
      renameSub: function (cid, sid, name) { return afterCfg(req("PATCH", "/categories/" + cid + "/subcategories/" + sid, { name: name })); },
      deleteSub: function (cid, sid) { return afterCfg(req("DELETE", "/categories/" + cid + "/subcategories/" + sid)); },
      addStatus: function (s) { return afterCfg(req("POST", "/statuses", s)); },
      editStatus: function (id, patch) { return afterCfg(req("PATCH", "/statuses/" + id, patch)); },
      deleteStatus: function (id) { return afterCfg(req("DELETE", "/statuses/" + id)); },
      addTemplate: function (t) { return afterCfg(req("POST", "/templates", t)); },
      deleteTemplate: function (id) { return afterCfg(req("DELETE", "/templates/" + id)); },
      exportData: exportData,
      importData: importData,
    };

    // ---- Render ---- //
    var header = h("div", { className: "ic-header" },
      h("div", { className: "ic-brand" },
        h("span", { className: "ic-logo" }, "🛫"),
        h("div", null,
          h("div", { className: "ic-h1" }, "Preflight"),
          h("div", { className: "ic-h2" }, "Idea capture — park ideas before they become Kanban work")
        )
      ),
      h("div", { className: "ic-header-actions" },
        h("div", { className: "ic-viewtoggle" },
          h("button", { className: "ic-seg" + (view === "ideas" ? " ic-seg-on" : ""), onClick: function () { setView("ideas"); } }, "Ideas"),
          h("button", { className: "ic-seg" + (view === "manage" ? " ic-seg-on" : ""), onClick: function () { setView("manage"); } }, "Manage")
        ),
        view === "ideas" ? h("button", { className: "ic-btn", onClick: function () { setCaptureOpen(!captureOpen); } }, captureOpen ? "Close capture" : "⚡ Quick capture") : null,
        view === "ideas" ? h("button", { className: "ic-btn ic-btn-primary", onClick: function () { setQuickOpen(!quickOpen); } }, quickOpen ? "Close" : "+ New idea") : null
      )
    );

    var body;
    if (loading) {
      body = h("div", { className: "ic-empty" }, "Loading…");
    } else if (view === "manage") {
      body = h(ManageView, { config: config, actions: actions });
    } else {
      var toolbar = h("div", { className: "ic-toolbar" },
        h("input", { className: "ic-input ic-search", placeholder: "Search ideas…", value: filter.q, onChange: function (e) { setFilter(Object.assign({}, filter, { q: e.target.value })); } }),
        h("select", { className: "ic-input", value: filter.status, onChange: function (e) { setFilter(Object.assign({}, filter, { status: e.target.value })); } },
          h("option", { value: "" }, "All statuses"),
          config.statuses.map(function (st) { return h("option", { key: st.id, value: st.id }, st.label); })
        ),
        h("select", { className: "ic-input", value: filter.source_type, onChange: function (e) { setFilter(Object.assign({}, filter, { source_type: e.target.value })); } },
          h("option", { value: "" }, "All sources"),
          (config.source_types || []).filter(function (st) { return st.id; }).map(function (st) { return h("option", { key: st.id, value: st.id }, st.label); })
        ),
        h("select", { className: "ic-input", value: filter.sort, onChange: function (e) { setFilter(Object.assign({}, filter, { sort: e.target.value })); } },
          h("option", { value: "updated" }, "Recently updated"),
          h("option", { value: "created" }, "Recently created"),
          h("option", { value: "title" }, "Title A–Z")
        )
      );

      var list = visible.length
        ? visible.map(function (i) { return h(IdeaCard, { key: i.id, idea: i, statuses: config.statuses, categories: config.categories, sourceTypes: config.source_types, active: i.id === selectedId, onClick: function () { setSelectedId(i.id === selectedId ? null : i.id); } }); })
        : [h("div", { key: "e", className: "ic-empty" }, ideas.length ? "No ideas match these filters." : "No ideas yet — use “⚡ Quick capture” or “+ New idea”.")];

      body = h("div", { className: "ic-workspace" },
        h(Sidebar, { categories: config.categories, ideas: ideas, filter: filter, onPick: function (c, s) { setFilter(Object.assign({}, filter, { category: c, subcategory: s })); } }),
        h("div", { className: "ic-main" },
          captureOpen ? h(QuickCapture, {
            sourceTypes: config.source_types,
            defaultCategory: filter.category, defaultSubcategory: filter.subcategory,
            onCreate: captureIdea, onClose: function () { setCaptureOpen(false); },
          }) : null,
          toolbar,
          quickOpen ? h(QuickAdd, {
            categories: config.categories, statuses: config.statuses,
            sourceTypes: config.source_types, templates: config.templates,
            defaultCategory: filter.category, defaultSubcategory: filter.subcategory,
            onCreate: createIdea, onCancel: function () { setQuickOpen(false); },
          }) : null,
          h("div", { className: "ic-list" }, list)
        ),
        detail ? h(DetailPane, {
          idea: detail, categories: config.categories, statuses: config.statuses, sourceTypes: config.source_types,
          onPatch: patchIdea, onDelete: deleteIdea, onAddUpdate: addUpdate, onPromote: promoteDraft, onClose: function () { setSelectedId(null); },
        }) : null
      );
    }

    var draftModal = draft ? h(Modal, { title: "Kanban card draft", onClose: function () { setDraft(null); } },
      h("p", { className: "ic-muted ic-small" }, "This is a draft to copy into Kanban — nothing was created or dispatched automatically."),
      h("textarea", { className: "ic-input ic-draft-text", readOnly: true, value: draft.markdown }),
      h("div", { className: "ic-modal-actions" },
        h("button", { className: "ic-btn ic-btn-primary", onClick: function () {
          try {
            if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(draft.markdown); }
          } catch (e) { /* clipboard may be unavailable; text is selectable */ }
        } }, "Copy to clipboard"),
        h("button", { className: "ic-btn ic-btn-ghost", onClick: function () { setDraft(null); } }, "Close")
      )
    ) : null;

    return h("div", { className: "ic-root" },
      error ? h("div", { className: "ic-error", onClick: function () { setError(null); } }, "⚠ " + error + "  (click to dismiss)") : null,
      header,
      body,
      draftModal
    );
  }

  // ----------------------------------------------------------------------- //
  // Register with Hermes
  // ----------------------------------------------------------------------- //

  if (window.__HERMES_PLUGINS__ && window.__HERMES_PLUGINS__.register) {
    window.__HERMES_PLUGINS__.register("preflight-idea-capture", App);
  } else {
    console.error("[preflight-idea-capture] __HERMES_PLUGINS__.register not available");
  }
})();
