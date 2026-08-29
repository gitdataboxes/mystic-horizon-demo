(function () {
  if (window.__shellInit) return;
  window.__shellInit = true;

  const transcript = document.getElementById("shell-transcript");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const callStartBtn = document.getElementById("call-start");
  const presenceStrip = document.querySelector(".presence-strip");
  const sidebar = document.getElementById("nav-sidebar");
  const navToggle = document.getElementById("nav-toggle");
  const navCollapse = document.getElementById("nav-collapse");
  const chatSidebar = document.getElementById("chat-sidebar");
  const chatCollapse = document.getElementById("chat-collapse");
  const hudUserWave = document.getElementById("hud-user-wave");
  const hudAgentWave = document.getElementById("hud-agent-wave");
  const hudUserTxt = document.getElementById("hud-user-txt");
  const hudAgentTxt = document.getElementById("hud-agent-txt");
  const hudTraceEl = document.getElementById("hud-trace");
  const hudSysDot = document.getElementById("hud-sys-dot");
  const hudSysState = document.getElementById("hud-sys-state");
  const hudSysLatency = document.getElementById("hud-sys-latency");
  const hudSysTimer = document.getElementById("hud-sys-timer");
  const hudSysMode = document.getElementById("hud-sys-mode");
  const gearToggle = document.getElementById("gear-toggle");
  const gearDropdown = document.getElementById("gear-dropdown");
  const hudMicToggle = document.getElementById("hud-mic");
  const hudStripEl = document.getElementById("hud-strip");
  const hudRttCanvas = document.getElementById("hud-rtt");
  const hudCollapse = document.getElementById("hud-collapse");

  let agentAnalyser = null;
  let userAnalyser = null;
  let agentWavePulse = 0;
  let userWavePulse = 0;
  let waveformRaf = null;
  let historyLoaded = false;
  let hudHistoryLoaded = false;
  let typingIndicator = null;
  let activeToolCards = [];
  let currentVoiceState = "disconnected";
  const streamingMessages = new Map();
  const suppressedStreamSegments = new Set();
  const recentAgentMessages = [];
  const MIC_SOUND_URLS = {
    on: "/soundfx/highendSwitchOn.ogg",
    off: "/soundfx/highendSwitchOff.ogg",
  };
  const MESSAGE_DEDUPE_WINDOW_MS = 8000;

  function playMicToggleSound(turningOn) {
    try {
      const audio = new Audio(turningOn ? MIC_SOUND_URLS.on : MIC_SOUND_URLS.off);
      audio.volume = 0.8;
      const playPromise = audio.play();
      if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => {});
    } catch (_err) {}
  }

  window.MysticSoundFx = Object.assign({}, window.MysticSoundFx || {}, {
    playMicToggle: playMicToggleSound,
  });

  function basePresenceForVoice() {
    if (currentVoiceState === "connecting" || currentVoiceState === "requesting") return "thinking";
    if (currentVoiceState === "listening") return "listening";
    return "idle";
  }

  function setPresence(state) {
    if (presenceStrip) presenceStrip.dataset.presence = state;
    var modes = { idle: "IDLE", listening: "LISTENING", thinking: "THINKING", speaking: "SPEAKING", error: "ERROR" };
    updateHudSysMode(modes[state] || "IDLE");
  }

  function restorePresence() {
    setPresence(basePresenceForVoice());
  }

  if (navToggle && sidebar) {
    navToggle.addEventListener("click", () => {
      const isOpen = sidebar.dataset.navOpen === "1";
      sidebar.dataset.navOpen = isOpen ? "0" : "1";
      navToggle.setAttribute("aria-expanded", isOpen ? "false" : "true");
    });
  }

  if (navCollapse && sidebar) {
    navCollapse.addEventListener("click", () => {
      const collapsed = sidebar.dataset.collapsed === "1";
      sidebar.dataset.collapsed = collapsed ? "0" : "1";
      navCollapse.setAttribute("aria-expanded", collapsed ? "true" : "false");
      navCollapse.innerHTML = collapsed ? "&#x25C2;" : "&#x25B8;";
    });
  }

  function setChatState(state) {
    if (!chatSidebar) return;
    const open = state === "open";
    chatSidebar.dataset.state = open ? "open" : "collapsed";
    if (!chatCollapse) return;
    chatCollapse.setAttribute("aria-expanded", open ? "true" : "false");
    chatCollapse.innerHTML = open ? "&#x25B8;" : "&#x25C2;";
  }

  if (chatCollapse && chatSidebar) {
    setChatState(chatSidebar.dataset.state || "open");
    chatCollapse.addEventListener("click", () => {
      setChatState(chatSidebar.dataset.state === "open" ? "collapsed" : "open");
    });
  }

  // --- Chat sidebar drag-to-resize ---
  const chatResizeHandle = document.getElementById("chat-resize-handle");
  if (chatResizeHandle && chatSidebar) {
    const savedWidth = localStorage.getItem("mh-chat-width");
    if (savedWidth) chatSidebar.style.setProperty("--chat-width", savedWidth + "px");

    let startX = 0;
    let startWidth = 0;

    function onResizeMove(e) {
      const dx = startX - e.clientX;
      const w = Math.max(200, Math.min(startWidth + dx, window.innerWidth * 0.6));
      chatSidebar.style.setProperty("--chat-width", w + "px");
    }

    function onResizeEnd() {
      chatSidebar.classList.remove("is-resizing");
      document.removeEventListener("mousemove", onResizeMove);
      document.removeEventListener("mouseup", onResizeEnd);
      const final = parseInt(getComputedStyle(chatSidebar).width, 10);
      if (final) localStorage.setItem("mh-chat-width", final);
    }

    chatResizeHandle.addEventListener("mousedown", (e) => {
      if (chatSidebar.dataset.state !== "open") return;
      e.preventDefault();
      startX = e.clientX;
      startWidth = chatSidebar.getBoundingClientRect().width;
      chatSidebar.classList.add("is-resizing");
      document.addEventListener("mousemove", onResizeMove);
      document.addEventListener("mouseup", onResizeEnd);
    });
  }

  function scrollFeed() {
    if (transcript) transcript.scrollTop = transcript.scrollHeight;
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function safeHref(url) {
    const value = String(url || "").trim();
    if (/^(https?:\/\/|mailto:|\/|#)/i.test(value)) {
      return value.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    return "#";
  }

  function renderMarkdown(text) {
    let rendered = escapeHtml(text);
    const blockPlaceholders = [];
    const inlinePlaceholders = [];

    rendered = rendered.replace(/```([\s\S]*?)```/g, (_match, code) => {
      const key = "@@CODEBLOCK" + blockPlaceholders.length + "@@";
      const body = String(code || "").replace(/^\n+|\n+$/g, "");
      blockPlaceholders.push(
        '<pre><code>' + body + "</code></pre>"
      );
      return key;
    });

    rendered = rendered.replace(/`([^`\n]+)`/g, (_match, code) => {
      const key = "@@INLINECODE" + inlinePlaceholders.length + "@@";
      inlinePlaceholders.push("<code>" + code + "</code>");
      return key;
    });

    rendered = rendered.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    rendered = rendered.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, (_match, prefix, value) => {
      return prefix + "<em>" + value + "</em>";
    });
    rendered = rendered.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label, url) => {
      return '<a href="' + safeHref(url) + '" target="_blank" rel="noreferrer">' + label + "</a>";
    });

    const formattedLines = [];
    let listType = null; // "ul" | "ol" | null
    rendered.split("\n").forEach((line) => {
      const trimmed = line.trim();
      const isUl = trimmed.startsWith("- ") || trimmed.startsWith("* ");
      const olMatch = trimmed.match(/^(\d+)\.\s+(.*)$/);
      const isOl = !!olMatch;

      if (isUl) {
        if (listType !== "ul") {
          if (listType) formattedLines.push("</" + listType + ">");
          formattedLines.push("<ul>");
          listType = "ul";
        }
        formattedLines.push("<li>" + trimmed.slice(2) + "</li>");
        return;
      }
      if (isOl) {
        if (listType !== "ol") {
          if (listType) formattedLines.push("</" + listType + ">");
          formattedLines.push("<ol>");
          listType = "ol";
        }
        formattedLines.push("<li>" + olMatch[2] + "</li>");
        return;
      }
      if (listType) {
        formattedLines.push("</" + listType + ">");
        listType = null;
      }

      // headers
      const hMatch = trimmed.match(/^(#{1,4})\s+(.*)$/);
      if (hMatch) {
        const level = hMatch[1].length;
        formattedLines.push("<h" + level + ">" + hMatch[2] + "</h" + level + ">");
        return;
      }

      // horizontal rule
      if (/^[-*_]{3,}$/.test(trimmed)) {
        formattedLines.push("<hr>");
        return;
      }

      // blockquotes
      if (trimmed.startsWith("&gt; ") || trimmed === "&gt;") {
        const content = trimmed === "&gt;" ? "" : trimmed.slice(5);
        formattedLines.push("<blockquote>" + content + "</blockquote>");
        return;
      }

      formattedLines.push(line);
    });
    if (listType) formattedLines.push("</" + listType + ">");

    // merge adjacent blockquotes, then convert tables
    rendered = formattedLines.join("\n")
      .replace(/<\/blockquote>\n<blockquote>/g, "<br>");

    // tables: detect pipe-delimited rows with a separator line
    rendered = rendered.replace(
      /(^|\n)(\|.+\|)\n\|[-| :]+\|\n((?:\|.+\|\n?)+)/gm,
      function(_match, pre, headerRow, bodyRows) {
        var cells = function(row) {
          return row.replace(/^\||\|$/g, "").split("|").map(function(c) { return c.trim(); });
        };
        var hCells = cells(headerRow);
        var html = "<table><thead><tr>" +
          hCells.map(function(c) { return "<th>" + c + "</th>"; }).join("") +
          "</tr></thead><tbody>";
        bodyRows.trim().split("\n").forEach(function(row) {
          html += "<tr>" +
            cells(row).map(function(c) { return "<td>" + c + "</td>"; }).join("") +
            "</tr>";
        });
        html += "</tbody></table>";
        return pre + html;
      }
    );

    rendered = rendered.replace(/\n\n/g, "<br><br>");

    inlinePlaceholders.forEach((html, index) => {
      rendered = rendered.replaceAll("@@INLINECODE" + index + "@@", html);
    });
    blockPlaceholders.forEach((html, index) => {
      rendered = rendered.replaceAll("@@CODEBLOCK" + index + "@@", html);
    });
    return rendered;
  }

  function createMessageElements(speaker) {
    if (!transcript) return null;
    const role = speaker === "user" ? "user" : speaker === "agent" ? "agent" : "system";
    const item = document.createElement("article");
    item.className = "msg msg-" + role;

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = role === "user" ? "YOU >" : role === "agent" ? "AGT >" : "SYS >";

    const body = document.createElement("div");
    body.className = "msg-body";

    item.appendChild(meta);
    item.appendChild(body);
    transcript.appendChild(item);
    scrollFeed();
    return { item: item, body: body, role: role };
  }

  function updateMessageBody(body, role, text) {
    if (role === "agent") {
      body.innerHTML = renderMarkdown(text);
      return;
    }
    body.textContent = text;
  }

  function normalizeMessageText(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function pruneRecentAgentMessages() {
    const cutoff = Date.now() - MESSAGE_DEDUPE_WINDOW_MS;
    while (recentAgentMessages.length && recentAgentMessages[0].at < cutoff) {
      recentAgentMessages.shift();
    }
  }

  function rememberAgentMessage(text) {
    const normalized = normalizeMessageText(text);
    if (!normalized) return;
    pruneRecentAgentMessages();
    recentAgentMessages.push({ text: normalized, at: Date.now() });
    if (recentAgentMessages.length > 20) recentAgentMessages.shift();
  }

  function hasRecentAgentMessage(text) {
    const normalized = normalizeMessageText(text);
    if (!normalized) return false;
    pruneRecentAgentMessages();
    return recentAgentMessages.some((entry) => entry.text === normalized);
  }

  function hasRenderedAgentMessage(text) {
    const normalized = normalizeMessageText(text);
    if (!transcript || !normalized) return false;
    const items = transcript.querySelectorAll(".msg-agent .msg-body");
    for (let i = items.length - 1; i >= 0 && i >= items.length - 8; i--) {
      if (normalizeMessageText(items[i].textContent || "") === normalized) return true;
    }
    return false;
  }

  function hasRecentAgentPrefix(text) {
    const normalized = normalizeMessageText(text);
    if (normalized.length < 3) return false;
    pruneRecentAgentMessages();
    return recentAgentMessages.some((entry) => entry.text === normalized || entry.text.indexOf(normalized) === 0);
  }

  function hasActiveAgentStreamOverlap(text) {
    const normalized = normalizeMessageText(text);
    if (!normalized) return false;
    for (const message of streamingMessages.values()) {
      if (!message || message.role !== "agent" || !message.body) continue;
      const active = normalizeMessageText(message.body.textContent || "");
      if (!active) continue;
      if (active === normalized || normalized.indexOf(active) === 0 || active.indexOf(normalized) === 0) {
        return true;
      }
    }
    return false;
  }

  function showTypingIndicator() {
    if (!transcript || typingIndicator) return;
    const message = createMessageElements("agent");
    if (!message) return;
    message.item.classList.add("msg-typing");
    message.body.innerHTML = (
      '<span class="typing-dots" aria-label="Agent is typing">' +
      "<span></span><span></span><span></span>" +
      "</span>"
    );
    typingIndicator = message.item;
    scrollFeed();
    setPresence("thinking");
  }

  function hideTypingIndicator() {
    if (!typingIndicator) return;
    typingIndicator.remove();
    typingIndicator = null;
    restorePresence();
  }

  function formatToolName(name) {
    var text = String(name || "tool").trim();
    return (text || "tool").replace(/[-_]/g, " ");
  }

  function toolVerb(name) {
    var verbs = {
      read: "Reading", write: "Writing", edit: "Editing",
      say: "Speaking", display: "Displaying", notify: "Notifying",
    };
    return verbs[name] || "Using " + formatToolName(name);
  }

  function toolVerbPast(name) {
    var verbs = {
      read: "Read", write: "Wrote", edit: "Edited",
      say: "Spoke", display: "Displayed", notify: "Notified",
    };
    return verbs[name] || formatToolName(name);
  }

  function formatDuration(ms) {
    if (ms < 1000) return ms + "ms";
    return (ms / 1000).toFixed(1) + "s";
  }

  function showToolCard(name, argsSummary) {
    if (!transcript) return;
    var card = document.createElement("div");
    card.className = "tool-card";
    card.setAttribute("data-status", "running");
    card.setAttribute("data-tool", name);
    card.setAttribute("role", "status");
    card.setAttribute("aria-live", "polite");

    var spinner = document.createElement("span");
    spinner.className = "tool-card-spinner";

    var label = document.createElement("span");
    label.className = "tool-card-label";
    var labelHtml = toolVerb(name);
    if (argsSummary) labelHtml += " <strong>" + escapeHtml(argsSummary) + "</strong>";
    label.innerHTML = labelHtml;

    card.appendChild(spinner);
    card.appendChild(label);
    transcript.appendChild(card);
    scrollFeed();
    activeToolCards.push(card);
    hudTraceAppend("tool", name + (argsSummary ? " \u2014 " + argsSummary : ""));
    setPresence("thinking");
  }

  function findActiveToolCard(name) {
    var matching = activeToolCards.find((card) => (
      card.getAttribute("data-status") === "running" &&
      card.getAttribute("data-tool") === name
    ));
    if (matching) return matching;
    return activeToolCards.find((card) => card.getAttribute("data-status") === "running") || null;
  }

  function completeToolCard(name, durationMs, isError) {
    var card = findActiveToolCard(name);
    if (!card) return;
    activeToolCards = activeToolCards.filter((activeCard) => activeCard !== card);

    card.setAttribute("data-status", isError ? "error" : "done");
    hudTraceAppend(isError ? "err" : "done", name + (typeof durationMs === "number" && durationMs > 0 ? " " + formatDuration(durationMs) : ""));

    // Replace spinner with icon
    var spinner = card.querySelector(".tool-card-spinner");
    if (spinner) {
      var icon = document.createElement("span");
      icon.className = "tool-card-icon " + (isError ? "tool-card-icon--error" : "tool-card-icon--done");
      icon.textContent = isError ? "\u2717" : "\u2713";
      card.replaceChild(icon, spinner);
    }

    // Update label to past tense
    var label = card.querySelector(".tool-card-label");
    if (label) {
      var strong = label.querySelector("strong");
      var detail = strong ? " <strong>" + strong.innerHTML + "</strong>" : "";
      label.innerHTML = (isError ? "Failed " + formatToolName(name) : toolVerbPast(name)) + detail;
    }

    // Add duration
    if (typeof durationMs === "number" && durationMs > 0) {
      var meta = document.createElement("span");
      meta.className = "tool-card-meta";
      meta.textContent = formatDuration(durationMs);
      card.appendChild(meta);
    }
    if (isError) setPresence("error");
    else restorePresence();
  }

  function hideToolIndicator() {
    activeToolCards.slice().forEach((card) => {
      completeToolCard(card.getAttribute("data-tool"), 0, false);
    });
  }

  function appendSystemMessage(text) {
    hideTypingIndicator();
    hideToolIndicator();
    appendMessage("system", text, "text");
  }

  function appendDisplay(payload) {
    if (!transcript) return;
    const card = document.createElement("article");
    card.className = "display-card";
    const pre = document.createElement("pre");
    pre.textContent =
      typeof payload === "string" ? payload : JSON.stringify(payload || {}, null, 2);
    card.appendChild(pre);
    transcript.appendChild(card);
    scrollFeed();
  }

  function appendMessage(speaker, text, modality, options) {
    if (!text) return;
    const stream = options || {};
    if (!stream.skipHud) updateHudStt(speaker, text, stream.streaming === true);
    if (speaker === "agent") hideTypingIndicator();

    if (modality === "voice") {
      if (speaker === "agent") {
        setPresence(options && options.streaming === true ? "speaking" : basePresenceForVoice());
      } else if (speaker === "user") {
        setPresence("listening");
      }
      return;
    }

    const segmentId = stream.segmentId || null;
    const isStreaming = stream.streaming === true;
    const source = stream.source || null;
    const dedupeAgentText = speaker === "agent" && modality !== "voice" && (source === "event" || source === "stream");
    const shouldSuppressDuplicateAgentText =
      speaker === "agent" &&
      modality !== "voice" &&
      !isStreaming &&
      (hasRecentAgentMessage(text) || hasRenderedAgentMessage(text));
    if (segmentId) {
      if (suppressedStreamSegments.has(segmentId)) {
        if (!isStreaming) suppressedStreamSegments.delete(segmentId);
        return null;
      }
      if (dedupeAgentText && source === "stream" && isStreaming && hasRecentAgentPrefix(text)) {
        suppressedStreamSegments.add(segmentId);
        return null;
      }
      let existing = streamingMessages.get(segmentId);
      if (!existing) {
        if (shouldSuppressDuplicateAgentText) return null;
        existing = createMessageElements(speaker);
        if (!existing) return;
        streamingMessages.set(segmentId, existing);
      }
      updateMessageBody(existing.body, existing.role, text);
      if (existing.role === "agent") {
        setPresence(isStreaming ? "thinking" : basePresenceForVoice());
      }
      if (!isStreaming) {
        streamingMessages.delete(segmentId);
        if (dedupeAgentText && hasRecentAgentMessage(text)) {
          existing.item.remove();
          return null;
        }
        if (speaker === "agent" && modality !== "voice") rememberAgentMessage(text);
      }
      scrollFeed();
      return existing;
    }

    if (shouldSuppressDuplicateAgentText) return null;
    const message = createMessageElements(speaker);
    if (!message) return;
    updateMessageBody(message.body, message.role, text);
    if (speaker === "agent" && modality !== "voice") rememberAgentMessage(text);
    scrollFeed();
    return message;
  }

  function replayHistoryEntries(entries, options) {
    if (historyLoaded) return;
    if (!Array.isArray(entries) || entries.length === 0) return;
    historyLoaded = true;
    entries.forEach((entry) => {
      if (entry && entry.type === "tool_started") {
        showToolCard(entry.name, entry.args_summary || "");
        return;
      }
      if (entry && entry.type === "tool_completed") {
        completeToolCard(entry.name, entry.duration_ms, !!entry.error);
        return;
      }
      appendMessage(entry.speaker || "agent", entry.text || "", entry.modality || "text", {
        skipHud: options && options.skipHud === true,
      });
    });
  }

  function replayHudHistory(entries) {
    if (hudHistoryLoaded) return true;
    if (!Array.isArray(entries) || entries.length === 0) return false;
    hudHistoryLoaded = true;
    entries.forEach((entry) => {
      if (!entry) return;
      if (entry.type === "tool_started") {
        hudTraceAppend("tool", (entry.name || "tool") + (entry.args_summary ? " \u2014 " + entry.args_summary : ""));
        return;
      }
      if (entry.type === "tool_completed") {
        hudTraceAppend(
          entry.error ? "err" : "done",
          (entry.name || "tool") + (typeof entry.duration_ms === "number" && entry.duration_ms > 0 ? " " + formatDuration(entry.duration_ms) : "")
        );
        return;
      }
      updateHudStt(entry.speaker || "agent", entry.text || "", false);
    });
    return true;
  }

  function primeDashboardHistory() {
    fetch("/dashboard/api/voice/history", { headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data) return;
        var hasHud = replayHudHistory(data.hudHistory);
        replayHistoryEntries(data.history, { skipHud: hasHud });
      })
      .catch(() => {});
  }

  function handleProgress(step) {
    const labels = {
      requesting_token: "Requesting session token",
      token_received: "Token received",
      connecting_room: "Connecting to room",
      room_joined: "Room joined",
    };
    const text = labels[step] || step;
    hudTraceAppend("sys", text);
  }

  // --- HUD ---

  const hudPinnedToBottom = new WeakMap();
  const hudProgrammaticScroll = new WeakMap();

  function isAtBottom(el) {
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 8;
  }

  function shouldStickToBottom(el) {
    if (!el) return false;
    if (!hudPinnedToBottom.has(el)) return true;
    return hudPinnedToBottom.get(el) !== false;
  }

  function stickToBottom(el, shouldStick) {
    if (!el || !shouldStick) return;
    var token = {};
    hudProgrammaticScroll.set(el, token);
    el.scrollTop = el.scrollHeight;
    hudPinnedToBottom.set(el, true);
    window.setTimeout(function () {
      if (hudProgrammaticScroll.get(el) === token) hudProgrammaticScroll.delete(el);
    }, 80);
  }

  var hudScrollTimers = new WeakMap();
  function flashHudScrollbar(el) {
    if (!el) return;
    el.classList.add("is-scrolling");
    var prev = hudScrollTimers.get(el);
    if (prev) clearTimeout(prev);
    var t = setTimeout(function () { el.classList.remove("is-scrolling"); }, 1200);
    hudScrollTimers.set(el, t);
  }

  [hudUserTxt, hudAgentTxt, hudTraceEl].forEach(function (el) {
    if (!el) return;
    el.addEventListener("mousedown", function () { flashHudScrollbar(el); });
    el.addEventListener("wheel", function () { flashHudScrollbar(el); }, { passive: true });
    el.addEventListener("scroll", function () {
      hudPinnedToBottom.set(el, isAtBottom(el));
      if (!hudProgrammaticScroll.get(el)) flashHudScrollbar(el);
    }, { passive: true });
    el.addEventListener("touchstart", function () { flashHudScrollbar(el); }, { passive: true });
  });

  function updateHudStt(speaker, text, isStreaming) {
    var el = speaker === "user" ? hudUserTxt : hudAgentTxt;
    if (!el || !text) return;
    var stick = shouldStickToBottom(el);
    var partial = el.querySelector(".hud-stt-partial");
    if (isStreaming) {
      if (partial) {
        partial.textContent = text;
        partial.title = text;
      }
      else {
        var d = document.createElement("div");
        d.className = "hud-stt-partial";
        d.textContent = text;
        d.title = text;
        el.appendChild(d);
      }
    } else {
      if (partial) {
        partial.className = "";
        partial.textContent = text;
        partial.title = text;
      }
      else {
        var last = el.lastElementChild;
        if (last && last.textContent === text) {
          last.title = text;
          stickToBottom(el, stick);
          return;
        }
        var d = document.createElement("div");
        d.textContent = text;
        d.title = text;
        el.appendChild(d);
      }
      while (el.children.length > 200) el.removeChild(el.firstChild);
    }
    stickToBottom(el, stick);
  }

  function hudTraceAppend(tag, text) {
    if (!hudTraceEl) return;
    var stick = shouldStickToBottom(hudTraceEl);
    var line = document.createElement("div");
    line.className = "hud-trace-line";
    var span = document.createElement("span");
    span.className = "hud-trace-tag hud-trace-tag--" + tag;
    span.textContent = "[" + tag + "]";
    line.appendChild(span);
    line.appendChild(document.createTextNode(" " + text));
    hudTraceEl.appendChild(line);
    while (hudTraceEl.children.length > 200) hudTraceEl.removeChild(hudTraceEl.firstChild);
    stickToBottom(hudTraceEl, stick);
  }

  var sessionStart = 0;
  var sessionTimerInterval = null;

  function updateHudSys(state) {
    if (hudSysDot) hudSysDot.dataset.state = state;
    if (hudSysState) {
      var labels = { disconnected: "OFFLINE", connecting: "CONNECTING", connected: "CONNECTED", requesting: "CONNECTING", listening: "CONNECTED" };
      hudSysState.textContent = labels[state] || state.toUpperCase();
    }
    if (state === "connected" && !sessionTimerInterval) {
      sessionStart = Date.now();
      sessionTimerInterval = setInterval(tickSessionTimer, 1000);
      tickSessionTimer();
    } else if (state === "disconnected") {
      if (sessionTimerInterval) { clearInterval(sessionTimerInterval); sessionTimerInterval = null; }
    }
  }

  function tickSessionTimer() {
    if (!hudSysTimer) return;
    var s = Math.floor((Date.now() - sessionStart) / 1000);
    hudSysTimer.textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  function updateHudSysMode(mode) {
    if (hudSysMode) hudSysMode.textContent = mode;
  }

  // --- Waveform rendering ---

  function resizeCanvases() {
    [hudAgentWave, hudUserWave, hudRttCanvas].forEach((canvas) => {
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      if (rect.width === 0) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
    });
  }

  const waveState = new WeakMap();
  const WAVE_LERP = 0.12;
  const WAVE_STEPS = 200;
  const WAVE_LAYERS = [
    { freq: 2.0, amp: 0.38, speed: 0.8,  alpha: 0.9,  width: 1.6 },
    { freq: 3.2, amp: 0.30, speed: 1.2,  alpha: 0.55, width: 1.2 },
    { freq: 4.8, amp: 0.22, speed: 1.7,  alpha: 0.35, width: 1.0 },
    { freq: 1.3, amp: 0.18, speed: 0.5,  alpha: 0.25, width: 0.8 },
  ];

  function drawWaveform(canvas, analyser) {
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    if (w === 0 || h === 0) return;
    const mid = h / 2;
    const dpr = window.devicePixelRatio || 1;

    // Compute RMS energy (0..1)
    let rms = 0;
    if (analyser && typeof analyser.getByteTimeDomainData === "function") {
      const bins = analyser.frequencyBinCount;
      const raw = new Uint8Array(bins);
      analyser.getByteTimeDomainData(raw);
      for (let i = 0; i < bins; i++) {
        const v = (raw[i] - 128) / 128;
        rms += v * v;
      }
      rms = Math.sqrt(rms / bins);
    }
    if (canvas === hudAgentWave) {
      rms = Math.max(rms, agentWavePulse);
      agentWavePulse *= 0.90;
    } else if (canvas === hudUserWave) {
      rms = Math.max(rms, userWavePulse);
      userWavePulse *= 0.90;
    }

    // Smooth energy over time
    let st = waveState.get(canvas);
    if (!st) { st = { energy: 0, phase: 0 }; waveState.set(canvas, st); }
    st.energy += (rms - st.energy) * WAVE_LERP;
    st.phase += 0.015;
    const energy = st.energy;
    if (canvas === hudAgentWave) window.MysticHudAgentEnergy = energy;
    else if (canvas === hudUserWave) window.MysticHudUserEnergy = energy;

    // Clear with trail
    ctx.fillStyle = "hsla(165, 12%, 5%, 0.28)";
    ctx.fillRect(0, 0, w, h);

    // Draw intertwined sine layers
    for (let li = 0; li < WAVE_LAYERS.length; li++) {
      const L = WAVE_LAYERS[li];
      const amplitude = L.amp * energy * mid * 8;
      const phase = st.phase * L.speed + li * 1.8;

      // Glow pass
      ctx.save();
      ctx.shadowColor = "hsla(165, 60%, 55%, " + (0.4 * L.alpha) + ")";
      ctx.shadowBlur = 6 * dpr;
      ctx.beginPath();
      for (let i = 0; i <= WAVE_STEPS; i++) {
        const t = i / WAVE_STEPS;
        const x = t * w;
        const y = mid + Math.sin(t * Math.PI * 2 * L.freq + phase) * amplitude;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.lineWidth = (L.width + 1.0) * dpr;
      ctx.strokeStyle = "hsla(165, 50%, 55%, " + (L.alpha * 0.3) + ")";
      ctx.stroke();
      ctx.restore();

      // Main line
      ctx.beginPath();
      for (let i = 0; i <= WAVE_STEPS; i++) {
        const t = i / WAVE_STEPS;
        const x = t * w;
        const y = mid + Math.sin(t * Math.PI * 2 * L.freq + phase) * amplitude;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.lineWidth = L.width * dpr;
      ctx.strokeStyle = "hsla(165, 55%, 65%, " + L.alpha + ")";
      ctx.stroke();
    }
  }

  // --- RTT gauge ---
  // Prefers agent-side provider network samples (TCP/TLS handshake latency) when
  // the worker publishes them. The old turn-timing fallback remains useful while
  // the room is connecting or when all providers are local.

  var RTT_MAX = 10000;
  var RTT_PEAK_DECAY = 0.97;
  var RTT_PHASE_TIMEOUT = 15000;

  var rtt = {
    phase: "idle", phaseTime: 0, speechEnd: 0, inputTime: 0, llmFirst: 0,
    stt: 0, llm: 0, tts: 0,
    pStt: 0, pLlm: 0, pTts: 0,
    speaking: false, silent: 0,
    network: false, lastProviderAt: 0,
    samples: { stt: null, llm: null, tts: null },
  };

  function rttSetPhase(p) {
    rtt.phase = p;
    rtt.phaseTime = performance.now();
  }

  function rttClearTurn() {
    rtt.stt = 0; rtt.llm = 0; rtt.tts = 0;
  }

  function rttUserVoice() {
    var now = performance.now();
    rtt.stt = rtt.speechEnd > 0 ? now - rtt.speechEnd : 0;
    rtt.llm = 0; rtt.tts = 0;
    rtt.inputTime = now;
    rttSetPhase("waiting_llm");
    rtt.speechEnd = 0;
  }

  function rttTextInput() {
    rttClearTurn();
    rtt.inputTime = performance.now();
    rttSetPhase("waiting_llm");
  }

  function rttAgentToken(modality) {
    if (rtt.phase !== "waiting_llm") return;
    var now = performance.now();
    rtt.llmFirst = now;
    rtt.llm = now - rtt.inputTime;
    if (modality === "voice") { rttSetPhase("waiting_tts"); }
    else { rtt.tts = 0; rttSetPhase("idle"); }
  }

  function rttReset() {
    rttSetPhase("idle");
    rtt.speechEnd = 0; rtt.inputTime = 0; rtt.llmFirst = 0;
    rtt.stt = 0; rtt.llm = 0; rtt.tts = 0;
    rtt.pStt = 0; rtt.pLlm = 0; rtt.pTts = 0;
    rtt.speaking = false; rtt.silent = 0;
    rtt.network = false; rtt.lastProviderAt = 0;
    rtt.samples = { stt: null, llm: null, tts: null };
    updateHudSysLatency();
  }

  function rttProviderLatency(payload) {
    if (!payload || !payload.samples) return;
    rtt.network = true;
    rtt.lastProviderAt = performance.now();
    rtt.samples = {
      stt: payload.samples.stt || null,
      llm: payload.samples.llm || null,
      tts: payload.samples.tts || null,
    };
    rtt.stt = sampleLatency(rtt.samples.stt);
    rtt.llm = sampleLatency(rtt.samples.llm);
    rtt.tts = sampleLatency(rtt.samples.tts);
    updateHudSysLatency();
  }

  function sampleLatency(sample) {
    var value = sample && typeof sample.latencyMs === "number" ? sample.latencyMs : 0;
    return value > 0 ? value : 0;
  }

  function updateHudSysLatency() {
    if (!hudSysLatency) return;
    if (!rtt.network) {
      hudSysLatency.textContent = "--ms";
      return;
    }
    var total = sampleLatency(rtt.samples.stt) + sampleLatency(rtt.samples.llm) + sampleLatency(rtt.samples.tts);
    hudSysLatency.textContent = total > 0 ? Math.round(total) + "ms" : "--ms";
  }

  function sampleText(sample) {
    if (!sample) return "--";
    if (sample.status === "ok" && typeof sample.latencyMs === "number") {
      return Math.round(sample.latencyMs) + "ms";
    }
    if (sample.status === "local") return "LOCAL";
    if (sample.status === "timeout") return "TIME";
    if (sample.status === "error") return "ERR";
    if (sample.status === "unconfigured") return "OFF";
    return "--";
  }

  function rmsEnergy(analyser) {
    if (!analyser) return 0;
    var n = analyser.frequencyBinCount;
    var buf = new Uint8Array(n);
    analyser.getByteTimeDomainData(buf);
    var sum = 0;
    for (var i = 0; i < n; i++) { var v = (buf[i] - 128) / 128; sum += v * v; }
    return Math.sqrt(sum / n);
  }

  function rttTick() {
    var now = performance.now();

    if (rtt.network && now - rtt.lastProviderAt > 20000) {
      rtt.network = false;
      rtt.samples = { stt: null, llm: null, tts: null };
      rttClearTurn();
      updateHudSysLatency();
    }

    if (rtt.phase !== "idle" && now - rtt.phaseTime > RTT_PHASE_TIMEOUT) {
      rttSetPhase("idle");
    }

    var userE = rmsEnergy(userAnalyser);
    if (userE > 0.05) {
      rtt.silent = 0;
      if (!rtt.speaking) rttClearTurn();
      rtt.speaking = true;
    } else if (rtt.speaking) {
      rtt.silent++;
      if (rtt.silent > 10) {
        rtt.speaking = false;
        rtt.speechEnd = now;
        if (rtt.phase === "idle") rttSetPhase("waiting_stt");
      }
    }

    if (rtt.phase === "waiting_tts") {
      if (rmsEnergy(agentAnalyser) > 0.03) {
        rtt.tts = now - rtt.llmFirst;
        rttSetPhase("idle");
      }
    }
  }

  function drawRtt(canvas) {
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    var w = canvas.width, h = canvas.height;
    if (w === 0 || h === 0) return;
    var dpr = window.devicePixelRatio || 1;

    rtt.pStt = Math.max(rtt.pStt * RTT_PEAK_DECAY, rtt.stt);
    rtt.pLlm = Math.max(rtt.pLlm * RTT_PEAK_DECAY, rtt.llm);
    rtt.pTts = Math.max(rtt.pTts * RTT_PEAK_DECAY, rtt.tts);

    ctx.fillStyle = "hsl(165, 12%, 5%)";
    ctx.fillRect(0, 0, w, h);

    var pad = Math.round(2 * dpr);
    var rowGap = Math.round(3 * dpr);
    var isActive = currentVoiceState !== "disconnected";

    var fontSize = Math.round(8 * dpr);
    ctx.font = fontSize + "px 'Share Tech Mono', monospace";
    var valueW = Math.ceil(ctx.measureText("0000ms").width) + Math.round(3 * dpr);
    var labelW = Math.ceil(ctx.measureText("LLM").width) + Math.round(4 * dpr);

    var barX = pad + labelW;
    var barAreaW = Math.max(1, w - barX - pad - valueW);
    var totalH = h - pad * 2 - rowGap * 2;
    var rowH = Math.floor(totalH / 3);
    var segW = Math.max(1, Math.round(2 * dpr));
    var segGap = Math.max(1, Math.round(1 * dpr));
    var segStep = segW + segGap;
    var segCount = Math.floor((barAreaW + segGap) / segStep);

    var vals = [rtt.stt, rtt.llm, rtt.tts];
    var peaks = [rtt.pStt, rtt.pLlm, rtt.pTts];
    var labels = ["STT", "LLM", "TTS"];
    var samples = [rtt.samples.stt, rtt.samples.llm, rtt.samples.tts];
    var logMax = Math.log10(1 + RTT_MAX / 100);

    for (var i = 0; i < 3; i++) {
      var y = pad + i * (rowH + rowGap);
      var ratio = Math.min(Math.log10(1 + vals[i] / 100) / logMax, 1);
      var pRatio = Math.min(Math.log10(1 + peaks[i] / 100) / logMax, 1);
      var litCount = Math.round(ratio * segCount);
      var peakSeg = Math.round(pRatio * segCount) - 1;
      if (isActive && !rtt.network) litCount = Math.max(1, litCount);

      ctx.fillStyle = litCount > 0 ? "hsl(165, 55%, 65%)" : "hsl(165, 20%, 24%)";
      ctx.font = fontSize + "px 'Share Tech Mono', monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(labels[i], pad, y + rowH / 2);

      for (var s = 0; s < segCount; s++) {
        var sx = barX + s * (segW + segGap);
        if (s < litCount) {
          ctx.fillStyle = "hsl(165, 55%, 65%)";
        } else if (s === peakSeg && peakSeg >= litCount) {
          ctx.fillStyle = "hsla(165, 55%, 65%, 0.45)";
        } else {
          ctx.fillStyle = "hsl(165, 15%, 8%)";
        }
        ctx.fillRect(sx, y, segW, rowH);
      }

      ctx.fillStyle = samples[i] && samples[i].status === "ok" ? "hsl(165, 55%, 65%)" : "hsl(165, 20%, 42%)";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(rtt.network ? sampleText(samples[i]) : "--", w - pad, y + rowH / 2);
    }
  }

  function waveformTick() {
    drawWaveform(hudAgentWave, agentAnalyser);
    drawWaveform(hudUserWave, userAnalyser);
    rttTick();
    drawRtt(hudRttCanvas);
    waveformRaf = requestAnimationFrame(waveformTick);
  }

  function startWaveformLoop() {
    if (waveformRaf) return;
    waveformRaf = requestAnimationFrame(waveformTick);
  }

  function stopWaveformLoop() {
    if (waveformRaf) {
      cancelAnimationFrame(waveformRaf);
      waveformRaf = null;
    }
  }

  window.addEventListener("resize", resizeCanvases);

  // --- Voice state ---

  function setVoiceState(state, detail) {
    void detail;
    currentVoiceState = state;
    updateHudSys(state);
    hudTraceAppend("agt", state);
    if (state === "disconnected") {
      rttReset();
      setPresence("idle");
    } else if (state === "connecting" || state === "requesting") {
      setPresence("thinking");
    } else if (state === "listening") {
      setPresence("listening");
    } else {
      setPresence("idle");
    }

    const inCall = state === "listening" || state === "requesting";
    if (callStartBtn) callStartBtn.hidden = inCall;

    if (hudMicToggle) {
      hudMicToggle.disabled = state === "connecting" || state === "requesting";
      hudMicToggle.dataset.active = state === "listening" ? "1" : "0";
      hudMicToggle.textContent = state === "listening" ? "LIVE" : "MIC";
    }

    if (state === "listening" || state === "connected") {
      resizeCanvases();
      startWaveformLoop();
    } else {
      stopWaveformLoop();
    }

  }

  // --- SSE activity stream ---

  if (window.EventSource) {
    const stream = new EventSource("/dashboard/stream");
    stream.addEventListener("activity", (event) => {
      try {
        var data = JSON.parse(event.data);
        document.dispatchEvent(new CustomEvent("mh:activity", { detail: data }));
      } catch (_error) {}
    });
  }

  // --- Voice bridge ---

  let bridge = null;
  let activeBridge = null;
  async function ensureConnected() {
    const b = activeBridge || bridge;
    if (!b) {
      throw new Error("Voice bridge is not available.");
    }
    if (b.isConnected()) return;
    await b.connect();
  }

  if (transcript && window.MysticVoiceBridge && typeof window.MysticVoiceBridge.create === "function") {
    bridge = window.MysticVoiceBridge.create({
      onState: (state, detail) => setVoiceState(state, detail),
      onError: (message) => {
        appendSystemMessage(message);
        setPresence("error");
      },
      onHistory: (entries, options) => replayHistoryEntries(entries, options),
      onHudHistory: (entries) => replayHudHistory(entries),
      onTranscript: (payload) => handleTranscript(payload),
      onEvent: (payload) => handleBridgeEvent(payload),
      onProgress: (step) => handleProgress(step),
      onAgentAudio: (analyser) => { agentAnalyser = analyser; },
      onUserAudio: (analyser) => { userAnalyser = analyser; },
    });

    function handleTranscript(payload) {
      if (!payload) return;
      const speaker = payload.speaker || "agent";
      const modality = payload.modality || "voice";
      const text = payload.text || "";
      if (!text) return;
      if (speaker === "user" && modality === "voice") {
        rttUserVoice();
        userWavePulse = Math.max(userWavePulse, 0.04);
      }
      if (speaker === "agent") rttAgentToken(modality);
      if (speaker === "agent" && modality === "voice") {
        agentWavePulse = Math.max(agentWavePulse, payload.streaming === true ? 0.08 : 0.04);
      }
      const options = {
        streaming: payload.streaming === true,
        segmentId: payload.segmentId || null,
        source: payload.source || null,
      };
      if (
        speaker === "agent" &&
        modality !== "voice" &&
        options.source === "event" &&
        !options.streaming &&
        !options.segmentId
      ) {
        window.setTimeout(() => {
          if (hasRecentAgentMessage(text) || hasActiveAgentStreamOverlap(text)) return;
          appendMessage(speaker, text, modality, options);
        }, 200);
        return;
      }
      appendMessage(speaker, text, modality, options);
    }

    function handleBridgeEvent(payload) {
      if (!payload) return;
      if (payload.type === "display") {
        appendDisplay(payload.payload || payload.text || payload);
        return;
      }
      if (payload.type === "provider_latency") {
        rttProviderLatency(payload);
        return;
      }
      if (payload.type === "tool_started") {
        showToolCard(payload.name, payload.args_summary || "");
        if (typeof window.MysticAgentArc === "function") {
          window.MysticAgentArc({
            kind: "call",
            name: payload.name,
            snippet: payload.args_summary || "",
          });
        }
        return;
      }
      if (payload.type === "tool_completed") {
        completeToolCard(payload.name, payload.duration_ms, !!payload.error);
        if (typeof window.MysticAgentArc === "function") {
          var snippet = typeof payload.duration_ms === "number" && payload.duration_ms > 0
            ? formatDuration(payload.duration_ms)
            : "";
          window.MysticAgentArc({
            kind: "response",
            name: payload.name,
            snippet: snippet,
            error: !!payload.error,
          });
        }
      }
    }

    // The dashboard bridge owns the real app HUD + sidebar. Temporary
    // experiences such as the game can ask us to pause/resume dashboard
    // microphone capture, but they render into their own DOM.
    const defaultBridge = bridge;
    activeBridge = defaultBridge;

    window.MysticShell = {
      setActiveBridge: (next) => {
        activeBridge = next || defaultBridge;
        // Drop the previous room's analysers immediately. When we swap
        // back to the default (dashboard) bridge, pull its last-known
        // analysers so the HUD wave resumes without waiting for the
        // next audio event.
        agentAnalyser = null;
        userAnalyser = null;
        if (!next && defaultBridge) {
          if (typeof defaultBridge.getAgentAnalyser === "function") {
            agentAnalyser = defaultBridge.getAgentAnalyser() || null;
          }
          if (typeof defaultBridge.getUserAnalyser === "function") {
            userAnalyser = defaultBridge.getUserAnalyser() || null;
          }
        }
      },
      notifyTranscript: handleTranscript,
      notifyEvent: handleBridgeEvent,
      setAgentAnalyser: (analyser) => { agentAnalyser = analyser; },
      setUserAnalyser: (analyser) => { userAnalyser = analyser; },
      pauseForGame: async () => {
        const voiceWasActive = !!(defaultBridge && defaultBridge.isVoiceActive());
        activeBridge = defaultBridge;
        if (voiceWasActive && defaultBridge) {
          await defaultBridge.stopVoice();
        }
        return { voiceWasActive: voiceWasActive };
      },
      resumeAfterGame: async (state) => {
        activeBridge = defaultBridge;
        const voiceWasActive = !!(state && state.voiceWasActive);
        if (voiceWasActive && defaultBridge && !defaultBridge.isVoiceActive()) {
          await defaultBridge.startVoice();
        }
      },
    };

    setVoiceState("disconnected");
    primeDashboardHistory();

    // Auto-connect to LiveKit when dashboard loads
    bridge.connect().catch((err) => {
      const msg = err && err.message ? err.message : "Auto-connect failed.";
      appendSystemMessage(msg);
      setPresence("error");
    });

    if (callStartBtn) {
      callStartBtn.addEventListener("click", async () => {
        callStartBtn.disabled = true;
        try {
          await ensureConnected();
          await activeBridge.startVoice();
        } catch (error) {
          const msg = error && error.message ? error.message : "Voice bridge failed.";
          appendSystemMessage(msg);
          setVoiceState(activeBridge.isConnected() ? "connected" : "disconnected");
          setPresence("error");
        } finally {
          callStartBtn.disabled = false;
        }
      });
    }

    if (hudMicToggle) {
      hudMicToggle.addEventListener("click", async () => {
        if (hudMicToggle.disabled) return;
        hudMicToggle.disabled = true;
        try {
          const turningOn = !activeBridge.isVoiceActive();
          playMicToggleSound(turningOn);
          await ensureConnected();
          if (turningOn) {
            await activeBridge.startVoice();
          } else {
            await activeBridge.stopVoice();
          }
        } catch (error) {
          const msg = error && error.message ? error.message : "Voice toggle failed.";
          appendSystemMessage(msg);
          setPresence("error");
        } finally {
          hudMicToggle.disabled = false;
        }
      });
    }

    window.addEventListener("beforeunload", () => {
      if (bridge) bridge.disconnect().catch(() => {});
    });
  }

  function updateActiveNav() {
    const nav = document.getElementById("dashboard-nav");
    if (!nav) return;
    const path = window.location.pathname;
    let activeLabel = "";
    nav.querySelectorAll(".nav-link").forEach((link) => {
      const href = link.getAttribute("href") || "";
      const isActive = href === path;
      link.classList.toggle("is-active", isActive);
      if (isActive) {
        link.setAttribute("aria-current", "page");
        activeLabel = (link.textContent || "").trim();
      } else {
        link.removeAttribute("aria-current");
      }
    });
    if (!activeLabel && path === "/dashboard/settings") {
      activeLabel = "Settings";
    }
    if (activeLabel) {
      document.title = activeLabel;
    }
  }

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail && event.detail.target;
    if (target && target.id === "page-content") {
      updateActiveNav();
    }
  });
  document.body.addEventListener("htmx:pushedIntoHistory", updateActiveNav);
  document.body.addEventListener("htmx:historyRestore", updateActiveNav);

  // --- HUD collapse ---
  if (hudCollapse && hudStripEl) {
    hudCollapse.addEventListener("click", () => {
      var collapsed = hudStripEl.dataset.collapsed === "1";
      hudStripEl.dataset.collapsed = collapsed ? "" : "1";
      hudCollapse.setAttribute("aria-expanded", collapsed ? "true" : "false");
      hudCollapse.innerHTML = collapsed ? "&#x25B4;" : "&#x25BE;";
    });
  }

  // --- Chat form ---

  if (!form || !input) return;

  function autoResize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 192) + "px";
  }
  input.addEventListener("input", autoResize);

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    input.value = "";
    input.style.height = "auto";
    rttTextInput();
    try {
      await ensureConnected();
      appendMessage("user", message, "text");
      if (!activeBridge || !activeBridge.isVoiceActive()) {
        showTypingIndicator();
      }
      hideToolIndicator();
      await activeBridge.sendChat(message, { pagePath: window.location.pathname });
    } catch (error) {
      appendSystemMessage(error && error.message ? error.message : "Failed to send message.");
      setPresence("error");
    }
  });

  // --- Gear menu ---
  if (gearToggle && gearDropdown) {
    gearToggle.addEventListener("click", function(e) {
      e.stopPropagation();
      var open = !gearDropdown.hidden;
      gearDropdown.hidden = open;
      gearToggle.setAttribute("aria-expanded", open ? "false" : "true");
    });
    document.addEventListener("click", function() {
      gearDropdown.hidden = true;
      gearToggle.setAttribute("aria-expanded", "false");
    });
  }

  // --- Sidebar tabs (Chat / Search) ---

  var tabBar = document.getElementById("sidebar-tabs");
  if (tabBar) {
    var tabs = tabBar.querySelectorAll(".sidebar-tab");
    var panels = chatSidebar
      ? chatSidebar.querySelectorAll(".sidebar-tab-panel")
      : document.querySelectorAll(".sidebar-tab-panel");

    tabBar.addEventListener("click", function (e) {
      var btn = e.target.closest(".sidebar-tab");
      if (!btn) return;
      var target = btn.getAttribute("data-tab");
      tabs.forEach(function (t) { t.classList.toggle("active", t === btn); });
      panels.forEach(function (p) { p.classList.toggle("active", p.getAttribute("data-panel") === target); });
    });
  }

  // --- Graph search ---

  var graphSearchInput = document.getElementById("graph-search-input");
  var graphSearchResults = document.getElementById("graph-search-results");

  if (graphSearchInput && graphSearchResults) {
    var searchDebounce = null;
    graphSearchInput.addEventListener("input", function () {
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(function () {
        var term = graphSearchInput.value.trim();
        if (!window.MysticGraph) {
          graphSearchResults.innerHTML = '<p class="graph-search-empty">Graph not loaded</p>';
          return;
        }
        var results = window.MysticGraph.search(term);
        if (!term) {
          graphSearchResults.innerHTML = "";
          return;
        }
        if (results.length === 0) {
          graphSearchResults.innerHTML = '<p class="graph-search-empty">No matches</p>';
          return;
        }
        graphSearchResults.innerHTML = results.map(function (n) {
          return '<button type="button" class="graph-search-item" data-node-id="' + n.id + '">'
            + '<span class="graph-search-type" data-type="' + n.type + '">' + n.type.toUpperCase() + '</span>'
            + '<span class="graph-search-label">' + escapeHtml(n.label || "") + '</span>'
            + '</button>';
        }).join("");
      }, 150);
    });

    graphSearchResults.addEventListener("click", function (e) {
      var btn = e.target.closest(".graph-search-item");
      if (!btn || !window.MysticGraph) return;
      var nodeId = btn.getAttribute("data-node-id");
      if (nodeId) window.MysticGraph.focusNode(nodeId);
    });

    function escapeHtml(str) {
      var div = document.createElement("div");
      div.textContent = str;
      return div.innerHTML;
    }
  }
})();
