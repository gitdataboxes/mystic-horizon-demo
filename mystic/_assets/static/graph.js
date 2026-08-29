/* Knowledge graph — 2D force-graph wired to /dashboard/api/graph + SSE live updates */
(function () {
  "use strict";

  var container = document.getElementById("knowledge-graph");
  if (!container || typeof ForceGraph === "undefined") return;

  // ── Design tokens (mirror DESIGN.md hue 165 monochrome) ─────────────────

  var VOID    = "hsl(165,12%,5%)";
  var SURFACE = "hsl(165,15%,10%)";
  var DIM     = "hsl(165,20%,24%)";
  var MID     = "hsl(165,35%,42%)";
  var GLOW    = "hsl(165,55%,65%)";
  var BRIGHT  = "hsl(165,65%,78%)";

  // Person nodes — GRAPH.md: "rendered in --glow." Scaled off GLOW/MID.
  var P_FILL   = "hsl(165,55%,65%)";
  var P_MED    = "hsl(165,45%,55%)";
  var P_DOT    = "hsl(165,40%,45%)";
  var P_HOT    = "hsl(165,65%,78%)";
  var P_TEXT   = "hsl(165,25%,8%)";
  var P_GLOW   = "hsla(165,60%,55%,";
  var LINK_CLR = "hsla(165,40%,42%,0.4)";

  var TYPE_SIZES = {
    agent: 20,
    person: 13,
  };

  // ── State ──────────────────────────────────────────────────────────────────

  var graphData = { nodes: [], links: [] };
  var graphMeta = {};
  var nodeById = {};
  var linkSet = new Set();
  var graph = null;
  var hoverNode = null;
  var hoverLink = null;
  var searchTerm = "";
  var newNodeIds = new Set();   // nodes added via SSE — glow pulse
  var signals = [];
  var lastSignalFrame = null;
  var SIGNAL_SPEED = 0.9;       // edge progress per second
  var AGENT_LINK_DISTANCE = 180;
  var ARTIFACT_LINK_DISTANCE = 48;
  var PERSON_RING_RADIUS = 180;
  var visibleLimit = Number(window.localStorage && window.localStorage.getItem("mh.graph.limit")) || 24;
  var activityWindowDays = Number(window.localStorage && window.localStorage.getItem("mh.graph.window")) || 30;
  var presenceState = "idle";

  // TEMP: tunable person/owner orbital knobs. Backed by localStorage so the
  // control panel survives refreshes; SAVE also copies a JS snippet of the
  // current values for pasting back into source. Remove this block and the
  // mountPersonPanel() call once the values are finalized.
  var PERSON_PARAM_DEFAULTS = {
    omega: 1.7, shellA: 7.0, shellRatio: 0.30, numShells: 5, perShell: 2,
    glowR: 0.48, glowA: 0.28, coreR: 0.22, nucR: 1.6, haloScale: 1.7
  };
  var personParams = (function () {
    var merged = {};
    for (var k in PERSON_PARAM_DEFAULTS) merged[k] = PERSON_PARAM_DEFAULTS[k];
    try {
      var raw = window.localStorage && window.localStorage.getItem("mh.graph.personParams");
      if (raw) {
        var parsed = JSON.parse(raw);
        for (var pk in parsed) if (pk in merged && isFinite(parsed[pk])) merged[pk] = parsed[pk];
      }
    } catch (_) {}
    return merged;
  })();
  var voiceStrandKey = null;   // cached key of the agent↔owner link that renders as a soundwave

  // Drag interaction — nodes hit-test against a small centered core (see
  // nodePointerAreaPaint), so halos/orbital shells aren't draggable; holding
  // ctrl or shift snaps drag positions to GRID_SNAP graph units.
  var GRID_SNAP = 40;
  var HIT_RADIUS_AGENT = 14;
  var HIT_RADIUS_NODE = 4;
  var snapActive = false;
  function syncSnapFromEvent(e) {
    snapActive = !!(e && (e.ctrlKey || e.shiftKey));
  }
  window.addEventListener("keydown", syncSnapFromEvent);
  window.addEventListener("keyup", syncSnapFromEvent);
  window.addEventListener("blur", function () { snapActive = false; });

  // ── Helpers ────────────────────────────────────────────────────────────────

  function endpointId(endpoint) {
    return endpoint && typeof endpoint === "object" ? endpoint.id : endpoint;
  }

  function linkKey(s, t, channel, modality) {
    return endpointId(s) + ">" + endpointId(t) + ":" + (channel || "link") + ":" + (modality || "any");
  }

  function isAgentLink(link) {
    var sid = endpointId(link.source);
    var tid = endpointId(link.target);
    return sid === "agent" || tid === "agent";
  }

  function endpointType(endpoint) {
    if (endpoint && typeof endpoint === "object") return endpoint.type;
    var node = nodeById[endpoint];
    return node ? node.type : null;
  }

  function isAgentTypeLink(link, type) {
    var sid = endpointId(link.source);
    var tid = endpointId(link.target);
    if (sid === "agent") return endpointType(link.target) === type;
    if (tid === "agent") return endpointType(link.source) === type;
    return false;
  }

  function preferredLinkDistance(link) {
    return isAgentLink(link) ? AGENT_LINK_DISTANCE : ARTIFACT_LINK_DISTANCE;
  }

  function preferredLinkStrength(link) {
    return isAgentLink(link) ? 0.08 : 0.36;
  }

  function preferredNodeCharge(node) {
    if (node.type === "agent") return -80;
    if (node.type === "person") return -30;
    return -35;
  }

  function configureForces() {
    if (!graph || typeof graph.d3Force !== "function") return;
    var linkForce = graph.d3Force("link");
    if (linkForce && typeof linkForce.distance === "function") {
      linkForce.distance(preferredLinkDistance);
    }
    if (linkForce && typeof linkForce.strength === "function") {
      linkForce.strength(preferredLinkStrength);
    }

    var chargeForce = graph.d3Force("charge");
    if (chargeForce && typeof chargeForce.strength === "function") {
      chargeForce.strength(preferredNodeCharge);
    }
  }

  function addNode(node) {
    if (nodeById[node.id]) return;
    nodeById[node.id] = node;
    graphData.nodes.push(node);
  }

  function addLink(source, target, channel, modality) {
    var key = linkKey(source, target, channel, modality);
    if (linkSet.has(key)) return;
    linkSet.add(key);
    graphData.links.push({ source: source, target: target, channel: channel || "dashboard", modality: modality || "text" });
  }

  function matchesSearch(node) {
    if (!searchTerm) return true;
    var term = searchTerm.toLowerCase();
    return (node.label || "").toLowerCase().indexOf(term) !== -1 ||
           (node.phone || "").toLowerCase().indexOf(term) !== -1 ||
           (node.type || "").toLowerCase().indexOf(term) !== -1;
  }

  function linkMatchesSearch(link) {
    if (!searchTerm) return true;
    var sid = endpointId(link.source);
    var tid = endpointId(link.target);
    var source = nodeById[sid];
    var target = nodeById[tid];
    return (source && matchesSearch(source)) ||
           (target && matchesSearch(target)) ||
           (link.strandLabel || link.interactionLabel || link.channelLabel || link.channel || "").toLowerCase().indexOf(searchTerm.toLowerCase()) !== -1 ||
           (link.modalityLabel || link.modality || "").toLowerCase().indexOf(searchTerm.toLowerCase()) !== -1;
  }

  function wrapLabel(text, maxChars) {
    if (text.length <= maxChars) return [text];
    var words = text.split(/\s+/);
    var lines = [];
    var cur = "";
    for (var i = 0; i < words.length; i++) {
      var test = cur ? cur + " " + words[i] : words[i];
      if (test.length > maxChars && cur) {
        lines.push(cur);
        cur = words[i];
      } else {
        cur = test;
      }
    }
    if (cur) lines.push(cur);
    return lines.length ? lines : [text];
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function strandOffset(link) {
    return Number(link && link.strandOffset != null ? link.strandOffset : 0);
  }

  function strandCurve(link) {
    return Number(link && link.strandCurve != null ? link.strandCurve : 0);
  }

  function normalizeSignalChannel(value) {
    var channel = (value || "dashboard").toLowerCase();
    return ["dashboard", "phone", "sms", "cli"].indexOf(channel) !== -1 ? channel : "dashboard";
  }

  function normalizeSignalModality(value) {
    var modality = (value || "text").toLowerCase();
    return ["voice", "text", "mixed"].indexOf(modality) !== -1 ? modality : "text";
  }

  function findStrandLink(personId, channel, modality) {
    return graphData.links.find(function (link) {
      return link.personId === personId && link.channel === channel && link.modality === modality;
    }) || graphData.links.find(function (link) {
      return link.personId === personId && link.modality === modality;
    }) || graphData.links.find(function (link) {
      return link.personId === personId;
    }) || null;
  }

  function offsetPoint(from, to, t, offset) {
    var x = from.x + (to.x - from.x) * t;
    var y = from.y + (to.y - from.y) * t;
    var dx = to.x - from.x;
    var dy = to.y - from.y;
    var len = Math.sqrt(dx * dx + dy * dy) || 1;
    return {
      x: x + (-dy / len) * offset,
      y: y + (dx / len) * offset,
    };
  }

  function getPresenceState() {
    var strip = document.querySelector(".presence-strip");
    presenceState = strip && strip.dataset && strip.dataset.presence ? strip.dataset.presence : "idle";
    return presenceState;
  }

  function getAgentEnergy() {
    var energy = Number(window.MysticHudAgentEnergy || 0);
    return isFinite(energy) ? clamp(energy, 0, 1) : 0;
  }

  function getUserEnergy() {
    var energy = Number(window.MysticHudUserEnergy || 0);
    return isFinite(energy) ? clamp(energy, 0, 1) : 0;
  }

  function getAgentScreenPosition() {
    var agent = nodeById.agent;
    if (!agent || !graph || typeof graph.graph2ScreenCoords !== "function" || !container) return null;
    var screen = graph.graph2ScreenCoords(agent.x || 0, agent.y || 0);
    if (!screen || !isFinite(screen.x) || !isFinite(screen.y)) return null;
    var rect = container.getBoundingClientRect();
    return {
      x: rect.left + screen.x,
      y: rect.top + screen.y,
    };
  }

  // Which side (if any) is audible right now. Gated on voice presence so the
  // waveform doesn't render after the session is torn down and the energy
  // globals go stale.
  function getVoiceSpeaker() {
    var state = getPresenceState();
    if (state !== "listening" && state !== "speaking") return null;
    var a = getAgentEnergy();
    var u = getUserEnergy();
    var threshold = 0.015;
    if (a < threshold && u < threshold) return null;
    if (a >= u) return { speaker: "agent", energy: a };
    return { speaker: "user", energy: u };
  }

  // Agent node — the only glyph painted on the force-graph canvas is the
  // label. The agent itself is an amorphous phosphor particle cloud rendered
  // on a dedicated overlay canvas with per-frame decay (see initAgentCloud).
  function drawAgentNode(node, ctx, globalScale, dimmed, isHover) {
    if (dimmed) return;
    var label = (node.label || "").toUpperCase();
    if (!label) return;
    var invS = 1 / Math.max(1, globalScale);
    var fs = 13 * invS;
    ctx.font = "700 " + fs + "px 'Share Tech Mono', monospace";
    ctx.letterSpacing = "0.08em";
    var padX = 11 * invS;
    var padY = 6 * invS;
    var textW = ctx.measureText(label).width;
    var boxW = textW + padX * 2;
    var boxH = fs + padY * 2;
    var boxX = node.x - boxW / 2;
    var boxY = node.y - 25 - 4 * invS - boxH;
    ctx.fillStyle = "hsla(165,30%,4%,0.55)";
    ctx.fillRect(boxX, boxY, boxW, boxH);
    ctx.strokeStyle = isHover ? "hsla(165,40%,40%,0.9)" : "hsl(165,20%,24%)";
    ctx.lineWidth = 0.5 * invS;
    ctx.strokeRect(boxX + 0.25 * invS, boxY + 0.25 * invS, boxW - 0.5 * invS, boxH - 0.5 * invS);
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = isHover ? "hsla(165,95%,88%,1)" : "hsla(165,85%,82%,0.95)";
    ctx.fillText(label, node.x, boxY + padY + fs * 0.82);
    ctx.letterSpacing = "0px";
  }

  // ── Agent cloud ────────────────────────────────────────────────────────
  //
  // An overlay canvas sits on top of the force-graph. A layered gradient
  // core anchors the emitter; particles spawn at the heart and spiral
  // outward (outward velocity + tangential spin + noise wobble), fading
  // in and out with age and cooling slightly as embers drift. State
  // modulates outward speed, core brightness, colour, and pulse waves;
  // tool events fire jagged arcs from the heart to random points.

  var CLOUD_PARTICLES = 1200;
  var CLOUD_BASE_RADIUS = 55;       // sim-space radius, scaled by zoom
  var CLOUD_MAX_DT = 0.05;

  var cloudCanvas = null;
  var cloudCtx = null;
  var cloudParticles = [];
  var cloudArcs = [];
  var cloudRaf = null;
  var cloudLastT = 0;

  var cloudTarget = { compress: 0, speed: 0.45, pulse: 0, align: 0, sat: 45, light: 58, energy: 0 };
  var cloudCurrent = { compress: 0, speed: 0.45, pulse: 0, align: 0, sat: 45, light: 58, energy: 0 };

  function initAgentCloud() {
    if (cloudCanvas) return;
    cloudCanvas = document.createElement("canvas");
    cloudCanvas.className = "agent-cloud";
    var s = cloudCanvas.style;
    s.position = "absolute";
    s.top = "0";
    s.left = "0";
    s.width = "100%";
    s.height = "100%";
    s.pointerEvents = "none";
    s.zIndex = "1";
    if (getComputedStyle(container).position === "static") {
      container.style.position = "relative";
    }
    container.appendChild(cloudCanvas);
    cloudCtx = cloudCanvas.getContext("2d");
    resizeAgentCloud();
    seedAgentParticles();
    cloudLastT = performance.now();
    cloudRaf = requestAnimationFrame(tickAgentCloud);
  }

  function resizeAgentCloud() {
    if (!cloudCanvas || !cloudCtx) return;
    var dpr = window.devicePixelRatio || 1;
    var w = container.clientWidth;
    var h = container.clientHeight;
    cloudCanvas.width = Math.max(1, Math.floor(w * dpr));
    cloudCanvas.height = Math.max(1, Math.floor(h * dpr));
    cloudCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function makeAgentParticle() {
    var p = {};
    resetAgentParticle(p);
    // Stagger initial ages AND radii so the cloud is already distributed on
    // first frame — otherwise every particle is stacked at r=0 and the halo
    // has to rebuild from scratch every time the page loads.
    p.age = Math.random() * p.life;
    p.radius = Math.random() * 1.2;
    return p;
  }

  function resetAgentParticle(p) {
    // Idle reads as a distant galaxy: slower drift, slower spin, slower
    // twinkle. Lifetime stretches so stars still reach the halo at the
    // reduced drift rate — otherwise the cloud collapses to a pinpoint.
    var idle = getPresenceState() === "idle";
    var driftK = idle ? 0.42 : 1.0;
    var spinK  = idle ? 0.40 : 1.0;
    var timeK  = idle ? 0.45 : 1.0;
    p.angle = Math.random() * Math.PI * 2;
    p.radius = 0;
    // Outward speed heavily biased low — dense slow dust near the nucleus,
    // a long tail of faster stars that actually reach the halo.
    p.outSpeed = (0.08 + Math.pow(Math.random(), 1.6) * 1.10) * driftK;
    p.spin = (Math.random() < 0.5 ? -1 : 1) * (0.10 + Math.random() * 0.40) * spinK;
    p.life = (6.0 + Math.random() * 9.0) / (idle ? 0.42 : 1.0);
    p.age = 0;
    // Star sizes — skewed small, with bright ones reading as ~2px.
    p.sz = 0.45 + Math.pow(Math.random(), 2.0) * 1.55;
    p.noise = Math.random() * Math.PI * 2;
    p.noiseSp = (0.15 + Math.random() * 0.55) * timeK;
    p.wobble = 0.02 + Math.random() * 0.05;
    p.twinklePhase = Math.random() * Math.PI * 2;
    p.twinkleSp = (1.2 + Math.random() * 3.2) * timeK;
  }

  function seedAgentParticles() {
    cloudParticles = [];
    for (var i = 0; i < CLOUD_PARTICLES; i++) cloudParticles.push(makeAgentParticle());
  }

  function updateCloudTarget() {
    var state = getPresenceState();
    var energy = getAgentEnergy();
    var t = cloudTarget;
    t.energy = energy;
    if (state === "listening") {
      t.compress = 0.18; t.speed = 0.9; t.pulse = 0; t.align = 0.5; t.sat = 72; t.light = 66;
    } else if (state === "thinking") {
      t.compress = 0.08; t.speed = 2.6; t.pulse = 0; t.align = 0; t.sat = 88; t.light = 72;
    } else if (state === "speaking") {
      t.compress = 0; t.speed = 1.3 + energy * 2.0; t.pulse = 0.22 + energy * 0.55; t.align = 0; t.sat = 96; t.light = 76 + energy * 10;
    } else {
      // Idle speed has to be high enough that particles reach the halo within
      // their 6–15s lifetime — particles always respawn at r=0, so if outward
      // drift is too slow the cloud collapses into a pinpoint as soon as the
      // thinking/listening burst's stars age out.
      t.compress = 0; t.speed = 0.45; t.pulse = 0; t.align = 0; t.sat = 45; t.light = 58;
    }
  }

  function smoothCloudState(dt) {
    var k = 1 - Math.exp(-dt * 4.2);
    cloudCurrent.compress += (cloudTarget.compress - cloudCurrent.compress) * k;
    cloudCurrent.speed    += (cloudTarget.speed    - cloudCurrent.speed)    * k;
    cloudCurrent.pulse    += (cloudTarget.pulse    - cloudCurrent.pulse)    * k;
    cloudCurrent.align    += (cloudTarget.align    - cloudCurrent.align)    * k;
    cloudCurrent.sat      += (cloudTarget.sat      - cloudCurrent.sat)      * k;
    cloudCurrent.light    += (cloudTarget.light    - cloudCurrent.light)    * k;
    cloudCurrent.energy   += (cloudTarget.energy   - cloudCurrent.energy)   * k;
  }

  function tickAgentCloud(now) {
    cloudRaf = requestAnimationFrame(tickAgentCloud);
    if (!cloudCanvas || !cloudCtx) return;
    var dt = Math.min(CLOUD_MAX_DT, Math.max(0, (now - cloudLastT) / 1000));
    cloudLastT = now;
    var w = cloudCanvas.clientWidth;
    var h = cloudCanvas.clientHeight;

    cloudCtx.clearRect(0, 0, w, h);

    var agent = nodeById.agent;
    if (!agent || !graph || typeof graph.graph2ScreenCoords !== "function") return;
    var sc = graph.graph2ScreenCoords(agent.x || 0, agent.y || 0);
    if (!sc || !isFinite(sc.x) || !isFinite(sc.y)) return;
    var zoom = typeof graph.zoom === "function" ? graph.zoom() : 1;
    var R = CLOUD_BASE_RADIUS * zoom;
    var pxScale = Math.max(0.7, zoom * 0.9);

    updateCloudTarget();
    smoothCloudState(dt);

    // Anchor the cloud exactly to the node so the voice strand never appears
    // to terminate off-center from the particle cluster. Per-particle wobble,
    // spin, and twinkle already carry the motion.
    var cx = sc.x;
    var cy = sc.y;

    var sat = cloudCurrent.sat;
    var light = cloudCurrent.light;
    var compress = cloudCurrent.compress;
    var speed = cloudCurrent.speed;
    var pulseAmp = cloudCurrent.pulse;
    var pulsePhase = now / 1000 * 5.2;
    var maxRadius = 1.6 * (1 - compress * 0.7);

    cloudCtx.globalCompositeOperation = "lighter";

    // Soft galactic-core haze with a 1px hot pinpoint — dense but never a
    // white disk. Pulse gently brightens during speaking.
    var coreBoost = 1 + (pulseAmp > 0 ? Math.sin(pulsePhase) * pulseAmp * 0.28 : 0);
    var coreR = R * 0.9;
    var coreInner = Math.min(90, (light + 22) * coreBoost);
    var coreGrad = cloudCtx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
    coreGrad.addColorStop(0.0, "hsla(165," + Math.min(99, sat + 6) + "%," + coreInner + "%,0.28)");
    coreGrad.addColorStop(0.10, "hsla(165," + sat + "%," + (light + 4) + "%,0.14)");
    coreGrad.addColorStop(0.40, "hsla(165," + sat + "%," + light + "%,0.05)");
    coreGrad.addColorStop(1.0, "hsla(165," + sat + "%," + Math.max(28, light - 12) + "%,0)");
    cloudCtx.fillStyle = coreGrad;
    cloudCtx.beginPath();
    cloudCtx.arc(cx, cy, coreR, 0, Math.PI * 2);
    cloudCtx.fill();

    // Hot pinpoint — the singular bright point at the heart of the galaxy.
    cloudCtx.fillStyle = "hsla(165," + Math.min(99, sat + 10) + "%," + Math.min(96, light + 28) + "%,0.72)";
    cloudCtx.beginPath();
    cloudCtx.arc(cx, cy, Math.max(1.1, 1.5 * pxScale * coreBoost), 0, Math.PI * 2);
    cloudCtx.fill();

    for (var i = 0; i < cloudParticles.length; i++) {
      var p = cloudParticles[i];
      p.age += dt;
      if (p.age >= p.life || p.radius > maxRadius) {
        resetAgentParticle(p);
        continue;
      }
      var t = p.age / p.life;

      // Linger near the core — outward velocity ramps with radius so stars
      // crowd the nucleus and thin into the halo, but the floor is high
      // enough that fast particles actually populate the outer reaches.
      var linger = 0.32 + p.radius * 0.68;
      var pulseWave = pulseAmp > 0 ? Math.sin(pulsePhase - p.radius * 3.2) * pulseAmp * 0.14 : 0;
      p.radius += p.outSpeed * (0.06 + speed * 0.42 + pulseWave) * linger * dt;

      // Tangential spin — attenuates with radius.
      p.angle += p.spin / (1 + p.radius * 1.9) * dt;

      // Perpendicular wobble + independent twinkle timer.
      p.noise += p.noiseSp * dt;
      p.twinklePhase += p.twinkleSp * dt;
      var wob = Math.sin(p.noise) * p.wobble * R;
      var twinkle = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(p.twinklePhase));

      var rad = p.radius * R;
      var ca = Math.cos(p.angle);
      var sa = Math.sin(p.angle);
      var x = cx + ca * rad - sa * wob;
      var y = cy + sa * rad + ca * wob;

      // Alpha envelope: ease in 15%, hold, ease out last 35%.
      var fade = t < 0.15 ? t / 0.15 : (t > 0.65 ? (1 - t) / 0.35 : 1);
      // Size scales mildly with radius — near-core stars smaller than outer.
      var radiusNorm = Math.min(1, p.radius / maxRadius);
      var sizeR = 0.40 + radiusNorm * 0.55;
      // Subtle hue cooling.
      var pl = light + 2 - t * 8;

      var core = p.sz * sizeR * pxScale;
      var alphaCore = (0.32 + p.sz * 0.20) * fade * twinkle;

      // Skip the gradient halo for sub-pixel particles — only the brightest
      // stars get a visible bloom.
      if (core >= 0.38) {
        var halo = core * 2.2;
        var grad = cloudCtx.createRadialGradient(x, y, 0, x, y, halo);
        grad.addColorStop(0, "hsla(165," + sat + "%," + pl + "%," + alphaCore + ")");
        grad.addColorStop(1, "hsla(165," + sat + "%," + Math.max(28, pl - 15) + "%,0)");
        cloudCtx.fillStyle = grad;
        cloudCtx.beginPath();
        cloudCtx.arc(x, y, halo, 0, Math.PI * 2);
        cloudCtx.fill();
      }

      cloudCtx.fillStyle = "hsla(165," + Math.min(99, sat + 4) + "%," + Math.min(94, pl + 14) + "%," + (0.85 * fade * twinkle) + ")";
      cloudCtx.beginPath();
      cloudCtx.arc(x, y, Math.max(0.25, core * 0.5), 0, Math.PI * 2);
      cloudCtx.fill();
    }

    // Lightning bolts — branched fractal polylines fired on tool events.
    var live = [];
    for (var a = 0; a < cloudArcs.length; a++) {
      var arc = cloudArcs[a];
      arc.age += dt;
      var boltT = arc.age / arc.life;
      var labelT = arc.labelLife > 0 ? arc.age / arc.labelLife : 1;
      if (boltT >= 1 && labelT >= 1) continue;
      drawAgentBolt(cloudCtx, arc, boltT, labelT, cx, cy, R, pxScale);
      live.push(arc);
    }
    cloudArcs = live;
  }

  // Deterministic PRNG so a bolt's branch geometry is baked at creation
  // time and rendered identically every frame — no per-frame shimmer.
  function makeRng(seed) {
    var s = (seed | 0) || 1;
    return function () {
      s = (s + 0x6D2B79F5) | 0;
      var t = Math.imul(s ^ (s >>> 15), 1 | s);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function buildBoltPolyline(depth, deflection, rng) {
    var pts = [[0, 0], [1, 0]];
    for (var iter = 0; iter < depth; iter++) {
      var next = [pts[0]];
      for (var i = 0; i < pts.length - 1; i++) {
        var a = pts[i], b = pts[i + 1];
        var mt = (a[0] + b[0]) / 2;
        var mp = (a[1] + b[1]) / 2;
        var segLen = Math.abs(b[0] - a[0]) || 0.001;
        mp += (rng() - 0.5) * deflection * segLen;
        next.push([mt, mp]);
        next.push(b);
      }
      pts = next;
      deflection *= 0.55;
    }
    return pts;
  }

  // Recursive bolt: main polyline + child bolts rooted on its vertices.
  // Each child carries its own local frame (origin on parent, rotated by
  // `angle`, length scaled by `scale`). Branches are forward-biased and
  // concentrate past the parent's midpoint, mimicking a stepped leader
  // that forks as it races outward rather than throwing off back-spurs.
  function buildBolt(depth, deflection, generationsLeft, rng, sproutProb) {
    var pts = buildBoltPolyline(depth, deflection, rng);
    var branches = [];
    var prob = sproutProb == null ? 0.28 : sproutProb;
    if (generationsLeft > 0) {
      for (var i = 1; i < pts.length - 1; i++) {
        var originT = pts[i][0];
        // Branches mostly appear past the midpoint.
        if (originT < 0.3 + rng() * 0.2) continue;
        if (rng() < prob) {
          branches.push({
            originT: originT,
            originP: pts[i][1],
            // Forward-bias: 14°–34° off-axis, never rearward.
            angle: (0.25 + rng() * 0.35) * (rng() < 0.5 ? -1 : 1),
            scale: 0.12 + rng() * 0.22,
            child: buildBolt(Math.max(2, depth - 1), deflection * 0.85, generationsLeft - 1, rng, prob * 0.6),
          });
        }
      }
    }
    return { pts: pts, branches: branches };
  }

  function drawBoltRec(ctx, bolt, ox, oy, dx, dy, length, alpha, width) {
    if (alpha <= 0.002) return;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    // Per-segment stroke so width tapers from trunk (full) toward tip (~10%).
    // One stroke per vertex-pair costs a few extra beginPath/stroke calls but
    // is the cheapest way to get a credible hot-to-hair profile in 2D canvas.
    var pts = bolt.pts;
    for (var i = 0; i < pts.length - 1; i++) {
      var t0 = pts[i][0], p0 = pts[i][1];
      var t1 = pts[i + 1][0], p1 = pts[i + 1][1];
      var x0 = ox + dx * t0 * length + (-dy) * p0 * length;
      var y0 = oy + dy * t0 * length + dx * p0 * length;
      var x1 = ox + dx * t1 * length + (-dy) * p1 * length;
      var y1 = oy + dy * t1 * length + dx * p1 * length;
      var tMid = (t0 + t1) * 0.5;
      ctx.lineWidth = Math.max(0.2, width * (1 - tMid * 0.9));
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }

    for (var b = 0; b < bolt.branches.length; b++) {
      var br = bolt.branches[b];
      var bx = ox + dx * br.originT * length + (-dy) * br.originP * length;
      var by = oy + dy * br.originT * length + dx * br.originP * length;
      var c = Math.cos(br.angle), s = Math.sin(br.angle);
      drawBoltRec(ctx, br.child, bx, by, dx * c - dy * s, dx * s + dy * c,
        length * br.scale, alpha * 0.7, Math.max(0.3, width * 0.45));
    }
  }

  function drawAgentBolt(ctx, arc, boltT, labelT, cx, cy, R, pxScale) {
    var length = arc.rf * R;
    var dx = Math.cos(arc.angle);
    var dy = Math.sin(arc.angle);
    var tipX = cx + dx * length;
    var tipY = cy + dy * length;

    // Bolt flash: brief hold at full brightness, sharp cubic drop, with a
    // stutter + re-strike in the first ~30ms so it reads as a real strike,
    // not a glow. Drawn in source-over so the thin line stays crisp instead
    // of blooming from the surrounding additive particle layer.
    if (boltT < 1) {
      var hold = 0.06;
      var fade = boltT < hold ? 1 : 1 - (boltT - hold) / (1 - hold);
      var baseAlpha = fade * fade * fade;
      // 1-bit stutter in the opening frames.
      var stutter = boltT < 0.08 ? (((arc.seed ^ Math.floor(boltT * 90)) & 1) ? 1 : 0.35) : 1;
      // Classic double-tap re-strike.
      var inRestrike = boltT > 0.14 && boltT < 0.22;
      var restrike = inRestrike ? 0.85 : 0;
      var a = Math.min(1, baseAlpha * stutter + restrike);

      var prev = ctx.globalCompositeOperation;
      ctx.globalCompositeOperation = "source-over";

      // Scene flash — brief full-canvas phosphor wash, strongest at t=0,
      // cubic drop inside 150ms. Reads as "the room lit up," which is the
      // thing that actually sells a lightning strike.
      if (boltT < 0.15) {
        var flashT = 1 - boltT / 0.15;
        var flashAlpha = flashT * flashT * flashT * 0.09;
        if (flashAlpha > 0.002) {
          ctx.fillStyle = "hsla(165,80%,55%," + flashAlpha + ")";
          ctx.fillRect(0, 0, ctx.canvas.clientWidth || ctx.canvas.width,
                             ctx.canvas.clientHeight || ctx.canvas.height);
        }
      }

      // The re-strike follows a slightly different leader path, so swap in
      // the second pre-baked bolt during the re-strike window.
      var bolt = inRestrike && arc.bolt2 ? arc.bolt2 : arc.bolt;

      // Thin outer halo — barely there.
      ctx.strokeStyle = "hsla(165,95%,80%," + (a * 0.22) + ")";
      drawBoltRec(ctx, bolt, cx, cy, dx, dy, length, a, 1.5 * pxScale);
      // White-hot core.
      ctx.strokeStyle = "hsla(165,100%,97%," + a + ")";
      drawBoltRec(ctx, bolt, cx, cy, dx, dy, length, a, 0.85 * pxScale);
      ctx.globalCompositeOperation = prev;
    }

    if (arc.label && labelT < 1 && !arc.labelSuppressed) {
      // Notification timing: snap in, hold opaque, fade at the end.
      var labelAlpha;
      if (labelT < 0.04) labelAlpha = labelT / 0.04;
      else if (labelT < 0.78) labelAlpha = 1;
      else labelAlpha = (1 - labelT) / 0.22;
      drawArcLabel(ctx, arc, tipX, tipY, dx, dy, labelAlpha, labelT, pxScale);
    }
  }

  function drawArcLabel(ctx, arc, tipX, tipY, dx, dy, alpha, labelT, pxScale) {
    if (alpha <= 0.01) return;
    var tag = arc.tag || "";
    var text = arc.label || "";
    var fontSize = Math.max(7.5, 8 * pxScale);
    ctx.font = "400 " + fontSize + "px 'Share Tech Mono', monospace";
    var tagPart = tag ? tag + " " : "";
    var tagWidth = tag ? ctx.measureText(tagPart).width : 0;
    var textWidth = ctx.measureText(text).width;
    var innerW = tagWidth + textWidth;

    var padX = 4 * pxScale;
    var padY = 2 * pxScale;
    var chipW = innerW + padX * 2;
    var chipH = fontSize + padY * 2;

    var gap = 10 * pxScale;
    var anchorX = tipX + dx * gap;
    var anchorY = tipY + dy * gap;
    var rightSide = dx >= 0;
    var chipX = rightSide ? anchorX : anchorX - chipW;
    var chipY = anchorY + (dy >= 0 ? 0 : -chipH);

    var prev = ctx.globalCompositeOperation;
    ctx.globalCompositeOperation = "source-over";

    // Barely-there backdrop — just enough to keep the readout legible
    // over the phosphor cloud. Hairline border, no glow, no fill chrome.
    ctx.fillStyle = "hsla(165,30%,4%," + (alpha * 0.55) + ")";
    ctx.fillRect(chipX, chipY, chipW, chipH);
    ctx.strokeStyle = "hsla(165,40%,55%," + (alpha * 0.7) + ")";
    ctx.lineWidth = Math.max(0.5, 0.5 * pxScale);
    ctx.strokeRect(chipX + 0.5, chipY + 0.5, chipW - 1, chipH - 1);

    var textY = chipY + padY + fontSize * 0.82;
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    if (tag) {
      ctx.fillStyle = "hsla(165,75%,72%," + (alpha * 0.95) + ")";
      ctx.fillText(tagPart, chipX + padX, textY);
    }
    ctx.fillStyle = "hsla(165,55%,68%," + (alpha * 0.88) + ")";
    ctx.fillText(text, chipX + padX + tagWidth, textY);

    ctx.globalCompositeOperation = prev;
  }

  var MAX_LABELED_ARCS = 3;
  var LABEL_LIFE = 5.0;
  var BOLT_LIFE = 0.26;
  var SNIPPET_MAX = 52;

  function truncateSnippet(s) {
    s = String(s || "").replace(/\s+/g, " ").trim();
    if (s.length <= SNIPPET_MAX) return s;
    return s.slice(0, SNIPPET_MAX - 1) + "\u2026";
  }

  function triggerAgentArc(payload) {
    payload = payload || {};
    var seed = (Math.random() * 2147483647) | 0;
    var rng = makeRng(seed);
    var bolt = buildBolt(4, 0.55, 2, rng, 0.28);
    // Second pre-baked leader path, used for the re-strike so it doesn't
    // look like the same frame flashed twice.
    var bolt2 = buildBolt(4, 0.55, 2, makeRng(seed ^ 0x9E3779B9), 0.28);

    var name = payload.name ? String(payload.name) : "";
    var snippet = truncateSnippet(payload.snippet || "");
    var hasLabel = !!(name || snippet);
    var kind = payload.kind || (hasLabel ? "call" : "");
    var tag = payload.error ? "[ERR]" : kind === "response" ? "[DONE]" : kind === "call" ? "[CALL]" : "";

    var labelText = "";
    if (hasLabel) {
      labelText = name;
      if (snippet) labelText += (labelText ? " \u2014 " : "") + snippet;
    }

    cloudArcs.push({
      angle: Math.random() * Math.PI * 2,
      rf: 0.85 + Math.random() * 0.55,
      life: BOLT_LIFE + Math.random() * 0.04,
      labelLife: hasLabel ? LABEL_LIFE : 0,
      age: 0,
      seed: seed,
      bolt: bolt,
      bolt2: bolt2,
      label: labelText,
      tag: tag,
      labelSuppressed: false,
    });

    // Cap concurrently-visible labels — suppress oldest first.
    var labeled = [];
    for (var i = 0; i < cloudArcs.length; i++) {
      var c = cloudArcs[i];
      if (c.label && !c.labelSuppressed && c.age < c.labelLife) labeled.push(c);
    }
    while (labeled.length > MAX_LABELED_ARCS) {
      labeled.shift().labelSuppressed = true;
    }
  }

  // Exposed for other scripts (e.g. shell.js) to fire arcs on tool events.
  window.MysticAgentArc = triggerAgentArc;

  // Persons render as a classic atom: bright nucleus, five evenly-spaced
  // orbital planes (36° apart), two electrons per shell moving fast in
  // tight synchrony. Pending actions light electrons up one by one, hot
  // slots spread across shells first so the glow reads around the atom.
  function drawPersonOrbital(node, ctx, globalScale, label, dimmed, isNew, isHover) {
    var invScale = 1 / Math.max(1, globalScale);
    var t = performance.now() / 1000;

    // Per-node phase shift so different atoms don't all tick in lockstep.
    var id = String(node.id || "");
    var seed = 2166136261 >>> 0;
    for (var si = 0; si < id.length; si++) {
      seed = Math.imul(seed ^ id.charCodeAt(si), 16777619) >>> 0;
    }
    var nodePhase = ((seed & 0xffff) / 0xffff) * Math.PI * 2;

    var factCount = Number(node.factCount || 0);
    var identified = node.identified !== false;

    var NUM_SHELLS = Math.max(1, Math.round(personParams.numShells));
    var PER_SHELL = Math.max(1, Math.round(personParams.perShell));
    var TOTAL = NUM_SHELLS * PER_SHELL;
    var SHELL_A = personParams.shellA;
    var SHELL_B = SHELL_A * personParams.shellRatio;
    // Person/owner orbitals read as quieter, more distant bodies — slower
    // orbit, smaller dimmer motes. The agent cloud is the busy thing on
    // screen; orbitals shouldn't compete with it.
    var OMEGA = personParams.omega;

    var hotCount = Math.max(0, Math.min(TOTAL, Number(node.pendingActionCount || 0)));

    // Halo — fact depth still lives here.
    if (!dimmed) {
      var factGlow = Math.min(1, Math.log(1 + factCount) / 3);
      var ga = isNew ? 0.38 : 0.14 + factGlow * 0.22;
      var gr = 10 * (personParams.haloScale + factGlow * 1.0);
      var grd = ctx.createRadialGradient(node.x, node.y, 2, node.x, node.y, gr);
      grd.addColorStop(0, P_GLOW + ga + ")");
      grd.addColorStop(1, P_GLOW + "0)");
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(node.x, node.y, gr, 0, 2 * Math.PI);
      ctx.fill();
    }

    // Electrons: iterate k outermost so the first hot slots spread across
    // all shells before doubling up — hotCount=3 lights three different
    // shells, not three electrons on one shell.
    if (!dimmed) {
      var ei = 0;
      for (var k = 0; k < PER_SHELL; k++) {
        for (var s = 0; s < NUM_SHELLS; s++) {
          var tilt = (s * Math.PI) / NUM_SHELLS;
          var cosT = Math.cos(tilt), sinT = Math.sin(tilt);
          var phase = k * Math.PI + s * 0.7;  // diametric pair + per-shell stagger
          var theta = OMEGA * t + phase + nodePhase;
          var lx = SHELL_A * Math.cos(theta);
          var ly = SHELL_B * Math.sin(theta);
          var mx = node.x + lx * cosT - ly * sinT;
          var my = node.y + lx * sinT + ly * cosT;

          var hot = ei < hotCount;
          ei++;

          // Uniform geometry/alpha across all orbital motes. Hot state (a
          // pending action) reads as a brighter white core only — the size
          // and glow match the rest of the swarm.
          var mg = ctx.createRadialGradient(mx, my, 0, mx, my, personParams.glowR);
          mg.addColorStop(0, "hsla(165,95%,92%," + personParams.glowA + ")");
          mg.addColorStop(1, "hsla(165,85%,60%,0)");
          ctx.fillStyle = mg;
          ctx.beginPath();
          ctx.arc(mx, my, personParams.glowR, 0, 2 * Math.PI);
          ctx.fill();

          ctx.fillStyle = hot ? "hsla(165,95%,92%,0.95)" : "hsla(165,88%,82%,0.72)";
          ctx.beginPath();
          ctx.arc(mx, my, personParams.coreR, 0, 2 * Math.PI);
          ctx.fill();
        }
      }
    }

    // Nucleus last so it sits on top when electrons swing through center.
    var nucR = personParams.nucR;
    if (!dimmed) {
      var nucGlow = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, nucR * 5);
      var nucAlpha = identified ? 0.60 : 0.30;
      nucGlow.addColorStop(0, "hsla(165,90%,85%," + nucAlpha + ")");
      nucGlow.addColorStop(1, "hsla(165,85%,55%,0)");
      ctx.fillStyle = nucGlow;
      ctx.beginPath();
      ctx.arc(node.x, node.y, nucR * 5, 0, 2 * Math.PI);
      ctx.fill();
    }
    if (dimmed) ctx.fillStyle = "hsla(165,20%,45%,0.5)";
    else if (isNew) ctx.fillStyle = P_HOT;
    else if (identified) ctx.fillStyle = P_FILL;
    else ctx.fillStyle = "hsla(165,35%,58%,0.75)";
    ctx.beginPath();
    ctx.arc(node.x, node.y, nucR, 0, 2 * Math.PI);
    ctx.fill();

    // Name floats above, clear of the outermost shell's upper arc.
    if (!dimmed) {
      var displayLabel = label.trim().toUpperCase() === "OWNER" ? "YOU" : label;
      var nameFs = 12 * invScale;
      ctx.font = nameFs + "px 'Share Tech Mono', monospace";
      ctx.letterSpacing = "0.08em";
      var nPadX = 11 * invScale;
      var nPadY = 6 * invScale;
      var nTextW = ctx.measureText(displayLabel).width;
      var nBoxW = nTextW + nPadX * 2;
      var nBoxH = nameFs + nPadY * 2;
      var nBoxX = node.x - nBoxW / 2;
      var nBoxY = node.y - 25 - 4 * invScale - nBoxH;
      ctx.fillStyle = "hsla(165,30%,4%,0.55)";
      ctx.fillRect(nBoxX, nBoxY, nBoxW, nBoxH);
      ctx.strokeStyle = "hsl(165,20%,24%)";
      ctx.lineWidth = 0.5 * invScale;
      ctx.strokeRect(nBoxX + 0.25 * invScale, nBoxY + 0.25 * invScale, nBoxW - 0.5 * invScale, nBoxH - 0.5 * invScale);
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = GLOW;
      ctx.fillText(displayLabel, node.x, nBoxY + nPadY + nameFs * 0.82);
      ctx.letterSpacing = "0px";
    }
  }

  function drawSignalDot(ctx, x, y, radius, voiceSignal) {
    var glowRadius = radius * (voiceSignal ? 6 : 5);
    var grd = ctx.createRadialGradient(x, y, 0, x, y, glowRadius);
    grd.addColorStop(0, "hsla(165,90%,82%,0.75)");
    grd.addColorStop(0.35, "hsla(165,85%,62%,0.28)");
    grd.addColorStop(1, "hsla(165,85%,55%,0)");
    ctx.fillStyle = grd;
    ctx.beginPath();
    ctx.arc(x, y, glowRadius, 0, 2 * Math.PI);
    ctx.fill();

    ctx.fillStyle = "hsl(165,90%,88%)";
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 2 * Math.PI);
    ctx.fill();
  }

  function drawSignals(ctx, globalScale) {
    if (!signals.length) {
      lastSignalFrame = null;
      return;
    }

    var now = performance.now();
    var dt = lastSignalFrame === null ? 0.016 : Math.min(0.05, (now - lastSignalFrame) / 1000);
    lastSignalFrame = now;

    var remaining = [];
    signals.forEach(function (sig) {
      var from = nodeById[sig.fromId];
      var to = nodeById[sig.toId];
      if (!from || !to || from.x == null || from.y == null || to.x == null || to.y == null) return;

      sig.t += SIGNAL_SPEED * dt;
      if (sig.t >= 1) {
        return;
      }

      var t = sig.t;
      var eased = t * t * (3 - 2 * t);
      var point = offsetPoint(from, to, eased, (sig.offset || 0) / Math.max(1, globalScale));
      var x = point.x;
      var y = point.y;
      var radius = (sig.modality === "voice" ? 3.4 : 2.7) / Math.max(1, globalScale);
      drawSignalDot(ctx, x, y, radius, sig.modality === "voice");
      remaining.push(sig);
    });
    signals = remaining;

    if (signals.length && graph && typeof graph.resumeAnimation === "function") {
      graph.resumeAnimation();
    }
  }

  // ── Voice-reactive edge ────────────────────────────────────────────────
  //
  // When a dashboard voice session is live, the agent↔owner strand swaps
  // its flat stroke for a sine wave driven by the active speaker's analyser
  // amplitude. Phase travels speaker→listener; color encodes who's talking
  // (agent = base hue 165, owner = DESIGN.md --owner-hue 140).

  var WAVE_PHASE = 0;       // global phase accumulator
  var WAVE_LAST_FRAME = 0;

  // Intertwined sine layers matching the HUD waveform aesthetic: one primary
  // line plus harmonic siblings at different speeds and alphas so the strand
  // reads as layered ribbons instead of a single rope.
  var VOICE_WAVE_LAYERS = [
    { freqMul: 1.00, ampMul: 0.95, speedMul: 0.8, alpha: 0.90, width: 1.6, offset: 0.0 },
    { freqMul: 1.60, ampMul: 0.72, speedMul: 1.2, alpha: 0.55, width: 1.2, offset: 1.8 },
    { freqMul: 2.40, ampMul: 0.52, speedMul: 1.7, alpha: 0.35, width: 1.0, offset: 3.6 },
    { freqMul: 0.65, ampMul: 0.42, speedMul: 0.5, alpha: 0.28, width: 0.8, offset: 5.4 },
  ];

  function drawVoiceWaveformEdge(ctx, agentNode, farNode, info, globalScale, link) {
    var dx = farNode.x - agentNode.x;
    var dy = farNode.y - agentNode.y;
    var len = Math.sqrt(dx * dx + dy * dy);
    if (len < 1) return;
    var ux = dx / len, uy = dy / len;
    var nx = -uy, ny = ux;

    var speaker = info && info.speaker ? info.speaker : null;
    var energy = info ? clamp(info.energy, 0, 1) : 0;

    var now = performance.now();
    var dt = WAVE_LAST_FRAME === 0 ? 0.016 : Math.min(0.05, (now - WAVE_LAST_FRAME) / 1000);
    WAVE_LAST_FRAME = now;
    // Phase travels speaker→listener. With y = sin(k·segT + φ), peaks move in
    // −segT when φ grows and +segT when φ shrinks. Agent sits at segT=0, so
    // agent-speaking wants φ decreasing (peaks roll toward person), and
    // user-speaking wants φ increasing. Layer speedMuls preserve direction.
    // Rest state still advances slowly so resumed speech doesn't snap.
    WAVE_PHASE += dt * 7 * (speaker === "user" ? 1 : -1);

    var shapedE = energy > 0 ? Math.pow(energy, 0.6) : 0;
    var invScale = 1 / Math.max(1, globalScale);
    // Flat at rest: amp fully scales with energy (matches HUD behavior).
    var baseAmp = shapedE * 20 * invScale;
    // More cycles on longer edges so wavelength stays roughly constant on-screen.
    var baseCycles = Math.max(3, Math.round(len / 38));
    var steps = Math.max(60, Math.min(140, Math.round(len / 3)));

    // Color: speaker-tinted when audible, else recency-weighted base phosphor
    // matching the default agent-link stroke so the strand reads consistently.
    var recency = link ? Number(link.recency || 0) : 0;
    var restLit = 26 + Math.round(30 * recency);
    var restAlpha = 0.18 + recency * 0.38;
    var hue = speaker === "user" ? 140 : 165;
    var sat = speaker ? 80 + Math.round(shapedE * 15) : 75;
    var lit = speaker ? 60 + Math.round(shapedE * 12) : restLit;
    var alphaBase = speaker ? 0.40 + shapedE * 0.45 : restAlpha;

    // Geometry runs end-to-end so the strand anchors in the agent's center
    // and the person's center; the envelope handles the soft terminations.
    var TWO_PI = Math.PI * 2;

    // Precompute the parametric sample points so both glow and crisp passes
    // per layer reuse the same geometry without recomputing sin().
    var xs = new Float32Array(steps + 1);
    var ys = new Float32Array(steps + 1);
    var envs = new Float32Array(steps + 1);
    var segTs = new Float32Array(steps + 1);
    for (var s = 0; s <= steps; s++) {
      var segT = s / steps;
      segTs[s] = segT;
      envs[s] = Math.sin(segT * Math.PI);     // 0 at ends, 1 in middle
      xs[s] = agentNode.x + ux * len * segT;
      ys[s] = agentNode.y + uy * len * segT;
    }

    for (var li = 0; li < VOICE_WAVE_LAYERS.length; li++) {
      var L = VOICE_WAVE_LAYERS[li];
      var layerAmp = baseAmp * L.ampMul;
      var layerCycles = baseCycles * L.freqMul;
      var layerPhase = WAVE_PHASE * L.speedMul + L.offset;
      var layerAlpha = alphaBase * L.alpha;
      var glowAlpha = layerAlpha * 0.35;
      var coreAlpha = Math.min(1, layerAlpha + 0.12);

      // Glow pass.
      ctx.save();
      ctx.shadowColor = "hsla(" + hue + "," + sat + "%," + lit + "%," + (layerAlpha * 0.85) + ")";
      ctx.shadowBlur = (5 + shapedE * 4) * invScale;
      ctx.beginPath();
      for (var i = 0; i <= steps; i++) {
        var wave = Math.sin(segTs[i] * layerCycles * TWO_PI + layerPhase) * layerAmp * envs[i];
        var bx = xs[i] + nx * wave;
        var by = ys[i] + ny * wave;
        if (i === 0) ctx.moveTo(bx, by); else ctx.lineTo(bx, by);
      }
      ctx.lineWidth = (L.width + 0.4) * invScale;
      ctx.strokeStyle = "hsla(" + hue + "," + Math.max(50, sat - 15) + "%," + Math.max(40, lit - 6) + "%," + glowAlpha + ")";
      ctx.stroke();
      ctx.restore();

      // Crisp line on top.
      ctx.beginPath();
      for (var j = 0; j <= steps; j++) {
        var wavej = Math.sin(segTs[j] * layerCycles * TWO_PI + layerPhase) * layerAmp * envs[j];
        var bxj = xs[j] + nx * wavej;
        var byj = ys[j] + ny * wavej;
        if (j === 0) ctx.moveTo(bxj, byj); else ctx.lineTo(bxj, byj);
      }
      ctx.lineWidth = (L.width * 0.6) * invScale;
      ctx.strokeStyle = "hsla(" + hue + ",95%,88%," + coreAlpha + ")";
      ctx.stroke();
    }
  }

  // ── Graph init ─────────────────────────────────────────────────────────────

  function initGraph() {
    graph = ForceGraph()(container)
      .backgroundColor(VOID)
      .nodeRelSize(4)
      .nodeVal(function (n) {
        var base = TYPE_SIZES[n.type] || 4;
        return Math.pow(base * Math.max(1, n.weight || 1), 1.5);
      })
      .nodeCanvasObjectMode(function () { return "replace"; })
      .nodeCanvasObject(function (node, ctx, globalScale) {
        var type = node.type;
        var dimmed = searchTerm && !matchesSearch(node);
        var isNew = newNodeIds.has(node.id);
        var isHover = node === hoverNode;

        // Agent has its own renderer — a dark phosphor well, not a filled glyph
        if (type === "agent") {
          drawAgentNode(node, ctx, globalScale, dimmed, isHover);
          return;
        }

        var label = (node.label || "").toUpperCase();

        // Persons render as an orbital nucleus — drawPersonOrbital handles
        // halo, orbits, motes, and name label in one pass.
        if (type === "person") {
          drawPersonOrbital(node, ctx, globalScale, label, dimmed, isNew, isHover);
          return;
        }

        // ── Compute radius + text layout ──
        var r, textLines, fontSize;

        if (type === "call" || type === "action") {
          fontSize = 2.5;
          ctx.font = fontSize + "px 'Share Tech Mono', monospace";
          textLines = wrapLabel(label, 12);
          var maxW2 = 0;
          textLines.forEach(function (l) {
            var w = ctx.measureText(l).width;
            if (w > maxW2) maxW2 = w;
          });
          var textH2 = textLines.length * fontSize * 1.35;
          r = Math.max(maxW2 / 2 + 2, textH2 / 2 + 2, 5);
        } else {
          // fact — small phosphor dot
          r = 1.8;
          textLines = null;
          fontSize = 0;
        }

        // ── Phosphorescent glow halo ──
        if (!dimmed) {
          var ga = isHover ? 0.5 : isNew ? 0.45
            : type === "fact" ? 0.12 : 0.2;
          var gr = r * (type === "fact" ? 3 : 2);
          var grd = ctx.createRadialGradient(node.x, node.y, r * 0.6, node.x, node.y, gr);
          grd.addColorStop(0, P_GLOW + ga + ")");
          grd.addColorStop(1, P_GLOW + "0)");
          ctx.fillStyle = grd;
          ctx.beginPath();
          ctx.arc(node.x, node.y, gr, 0, 2 * Math.PI);
          ctx.fill();
        }

        // ── Main filled circle ──
        var fill;
        if (dimmed) fill = "hsla(165,15%,15%,0.25)";
        else if (isNew || isHover) fill = P_HOT;
        else if (type === "call" || type === "action") fill = P_MED;
        else fill = P_DOT;

        ctx.fillStyle = fill;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
        ctx.fill();

        // ── Inner highlight (CRT hot-spot) ──
        if (!dimmed && type !== "fact") {
          var hl = ctx.createRadialGradient(
            node.x - r * 0.15, node.y - r * 0.15, 0,
            node.x, node.y, r
          );
          hl.addColorStop(0, "hsla(165,90%,95%,0.3)");
          hl.addColorStop(0.4, "hsla(165,90%,95%,0.06)");
          hl.addColorStop(1, "hsla(165,90%,95%,0)");
          ctx.fillStyle = hl;
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
          ctx.fill();
        }

        // ── Label ──
        if (textLines && textLines.length > 0) {
          if (!dimmed) {
            var lh = fontSize * 1.35;
            ctx.fillStyle = P_TEXT;
            ctx.font = fontSize + "px 'Share Tech Mono', monospace";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            var sy = node.y - ((textLines.length - 1) * lh) / 2;
            textLines.forEach(function (line, i) {
              ctx.fillText(line, node.x, sy + i * lh);
            });
          }
        } else if (type === "fact" && (isHover || (searchTerm && matchesSearch(node)))) {
          var fs = 10 / globalScale;
          ctx.font = fs + "px 'Share Tech Mono', monospace";
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = isHover ? BRIGHT : GLOW;
          ctx.fillText(label, node.x, node.y + r + 2 / globalScale);
        }
      })
      .linkColor(function (link) {
        if (searchTerm && !linkMatchesSearch(link)) return "hsla(165,15%,20%,0.12)";
        if (!isAgentLink(link)) return LINK_CLR;
        var recency = Number(link.recency || 0);
        var lightness = 26 + Math.round(30 * recency);
        var alpha = 0.18 + recency * 0.38;
        return "hsla(165,75%," + lightness + "%," + alpha + ")";
      })
      .linkWidth(function (link) {
        if (searchTerm && !linkMatchesSearch(link)) return 0.3;
        if (!isAgentLink(link)) return 0.6;
        return 0.5 + Math.min(5, Number(link.weight || 1)) * 0.55;
      })
      .linkCurvature(function (link) {
        return strandCurve(link);
      })
      .linkCanvasObjectMode(function (link) {
        return isAgentLink(link) ? "replace" : undefined;
      })
      .linkCanvasObject(function (link, ctx, globalScale) {
        if (!isAgentLink(link)) return;
        var src = link.source;
        var tgt = link.target;
        if (!src || !tgt || src.x == null || tgt.x == null) return;

        var agentNode = endpointId(src) === "agent" ? src : tgt;
        var farNode = endpointId(src) === "agent" ? tgt : src;

        // Voice-reactive soundwave: the owner's dashboard strand is always
        // rendered as a waveform during a voice session — flat at rest, rising
        // with speaker energy — so the strand behaves like the HUD waveform.
        if (voiceStrandKey) {
          var key = linkKey(link.source, link.target, link.channel, link.modality);
          if (key === voiceStrandKey) {
            drawVoiceWaveformEdge(ctx, agentNode, farNode, getVoiceSpeaker(), globalScale, link);
            return;
          }
        }

        var dx = farNode.x - agentNode.x;
        var dy = farNode.y - agentNode.y;
        var len = Math.sqrt(dx * dx + dy * dy) || 1;

        // Color + width match linkColor/linkWidth callbacks so directional
        // particles (on hover) stay consistent with the main stroke.
        var recency = Number(link.recency || 0);
        var lightness = 26 + Math.round(30 * recency);
        var alphaFull = 0.18 + recency * 0.38;
        var hsl = "hsla(165,75%," + lightness + "%,";
        var dimmed = searchTerm && !linkMatchesSearch(link);
        if (dimmed) { alphaFull *= 0.3; }
        var width = (0.5 + Math.min(5, Number(link.weight || 1)) * 0.55) / Math.max(1, globalScale);

        // Fade from transparent at the agent center to full alpha once the line
        // clears the cloud's outer edge.
        var fadeEnd = Math.min(0.55, (CLOUD_BASE_RADIUS * 1.15) / len);
        var grad = ctx.createLinearGradient(agentNode.x, agentNode.y, farNode.x, farNode.y);
        grad.addColorStop(0, hsl + "0)");
        grad.addColorStop(fadeEnd, hsl + alphaFull + ")");
        grad.addColorStop(1, hsl + alphaFull + ")");

        ctx.strokeStyle = grad;
        ctx.lineWidth = width;
        ctx.beginPath();
        var sc = Number(link.strandCurve || 0);
        if (Math.abs(sc) > 0.001) {
          var mx = (agentNode.x + farNode.x) / 2;
          var my = (agentNode.y + farNode.y) / 2;
          mx += (-dy / len) * sc * len * 0.5;
          my += (dx / len) * sc * len * 0.5;
          ctx.moveTo(agentNode.x, agentNode.y);
          ctx.quadraticCurveTo(mx, my, farNode.x, farNode.y);
        } else {
          ctx.moveTo(agentNode.x, agentNode.y);
          ctx.lineTo(farNode.x, farNode.y);
        }
        ctx.stroke();
      })
      .linkDirectionalParticles(function (link) {
        var sid = typeof link.source === "object" ? link.source.id : link.source;
        var tid = typeof link.target === "object" ? link.target.id : link.target;
        if (sid === "agent" || tid === "agent") {
          if (hoverLink === link) return 1;
          return 0;
        }
        if (hoverNode && (sid === hoverNode.id || tid === hoverNode.id)) return 1;
        if (newNodeIds.has(sid) || newNodeIds.has(tid)) return 1;
        return 0;
      })
      .linkDirectionalParticleWidth(2)
      .linkDirectionalParticleColor(function () { return GLOW; })
      .linkDirectionalParticleSpeed(0.005)
      .nodePointerAreaPaint(function (node, color, ctx) {
        if (!node) return;
        var r = node.type === "agent" ? HIT_RADIUS_AGENT : HIT_RADIUS_NODE;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
        ctx.fill();
      })
      .onNodeDrag(function (node) {
        if (!node || !snapActive) return;
        if (typeof node.fx === "number") {
          node.fx = Math.round(node.fx / GRID_SNAP) * GRID_SNAP;
          node.x = node.fx;
        }
        if (typeof node.fy === "number") {
          node.fy = Math.round(node.fy / GRID_SNAP) * GRID_SNAP;
          node.y = node.fy;
        }
      })
      .onNodeDragEnd(function (node) {
        if (!node) return;
        node._userMoved = true;
      })
      .onNodeHover(function (node) {
        hoverNode = node || null;
        hoverLink = null;
        container.style.cursor = node ? "pointer" : "default";
      })
      .onLinkHover(function (link) {
        hoverLink = link || null;
        if (!hoverNode) container.style.cursor = link ? "pointer" : "default";
      })
      .onNodeClick(function (node, event) {
        if (!node) return;
        // Alt+click on the agent hub warps the user into the asteroids
        // easter egg. We compute the node's screen position here and pass
        // it along so the game can anchor its warp transition on that
        // exact point. Don't do any of the normal click handling.
        if (event && event.altKey && (node.id === "agent" || node.type === "agent")) {
          var rect = container.getBoundingClientRect();
          var screen = graph.graph2ScreenCoords(node.x, node.y);
          try {
            window.dispatchEvent(new CustomEvent("mh:game-start", {
              detail: {
                originX: rect.left + screen.x,
                originY: rect.top + screen.y,
              },
            }));
          } catch (err) {
            console.warn("mh.game.dispatch.failed", err);
          }
          return;
        }
        graph.centerAt(node.x, node.y, 400);
        graph.zoom(4, 400);
        showNodeDetail(node);
      })
      .onLinkClick(function (link) {
        if (!link) return;
        hoverLink = link;
        showEdgeDetail(link);
      })
      .onBackgroundClick(function () {
        hoverNode = null;
        hoverLink = null;
        clearNodeDetail();
      })
      .d3AlphaDecay(0.02)
      .d3VelocityDecay(0.3)
      .warmupTicks(80)
      .cooldownTime(4000)
      // Keep redrawing every frame even after the force sim settles, so the
      // agent's breathing, signal pulses, and link-heat decay animate without
      // requiring user interaction to re-kick the render loop.
      .autoPauseRedraw(false)
      .onRenderFramePost(drawSignals);

    configureForces();
    initAgentCloud();
    // mountPersonPanel();  // TEMP tuning panel — re-enable to adjust person orbital params
  }

  // TEMP: floating control panel for tuning person/owner orbital params live.
  // Remove along with the personParams block once values are finalized.
  function mountPersonPanel() {
    if (document.getElementById("person-param-panel")) return;
    var KNOBS = [
      { key: "omega",       label: "orbit speed",  min: 0,    max: 8,    step: 0.05 },
      { key: "shellA",      label: "shell radius", min: 2,    max: 14,   step: 0.1  },
      { key: "shellRatio",  label: "ellipticity",  min: 0.05, max: 1,    step: 0.01 },
      { key: "numShells",   label: "# shells",     min: 1,    max: 8,    step: 1    },
      { key: "perShell",    label: "per shell",    min: 1,    max: 4,    step: 1    },
      { key: "glowR",       label: "glow radius",  min: 0.1,  max: 3,    step: 0.02 },
      { key: "glowA",       label: "glow alpha",   min: 0,    max: 1,    step: 0.02 },
      { key: "coreR",       label: "core radius",  min: 0.05, max: 1,    step: 0.01 },
      { key: "nucR",        label: "nucleus r",    min: 0.4,  max: 4,    step: 0.05 },
      { key: "haloScale",   label: "halo scale",   min: 0.5,  max: 3.5,  step: 0.05 }
    ];
    var panel = document.createElement("div");
    panel.id = "person-param-panel";
    panel.setAttribute("style",
      "position:absolute;top:12px;right:12px;z-index:40;" +
      "background:hsla(165,18%,8%,0.92);border:1px solid hsl(165,20%,24%);" +
      "padding:10px 12px;font:10px/1.3 'Share Tech Mono',monospace;color:hsl(165,55%,65%);" +
      "width:240px;max-height:80%;overflow-y:auto;letter-spacing:0.06em;" +
      "user-select:none;");
    var title = document.createElement("div");
    title.textContent = "PERSON / OWNER TUNING";
    title.setAttribute("style", "margin-bottom:8px;color:hsl(165,65%,78%);letter-spacing:0.12em;cursor:pointer;");
    var body = document.createElement("div");
    title.addEventListener("click", function () {
      body.style.display = body.style.display === "none" ? "" : "none";
    });
    panel.appendChild(title);
    panel.appendChild(body);

    var rows = {};
    KNOBS.forEach(function (knob) {
      var row = document.createElement("label");
      row.setAttribute("style", "display:block;margin:4px 0;");
      var head = document.createElement("div");
      head.setAttribute("style", "display:flex;justify-content:space-between;gap:8px;");
      var name = document.createElement("span"); name.textContent = knob.label;
      var val = document.createElement("span");
      val.setAttribute("style", "color:hsl(165,65%,78%);");
      head.appendChild(name); head.appendChild(val);
      var input = document.createElement("input");
      input.type = "range";
      input.min = String(knob.min); input.max = String(knob.max); input.step = String(knob.step);
      input.value = String(personParams[knob.key]);
      input.setAttribute("style", "width:100%;accent-color:hsl(165,55%,55%);");
      val.textContent = formatKnob(knob, personParams[knob.key]);
      input.addEventListener("input", function () {
        var v = Number(input.value);
        if (!isFinite(v)) return;
        personParams[knob.key] = v;
        val.textContent = formatKnob(knob, v);
      });
      row.appendChild(head); row.appendChild(input);
      body.appendChild(row);
      rows[knob.key] = { input: input, val: val, knob: knob };
    });

    var btnRow = document.createElement("div");
    btnRow.setAttribute("style", "display:flex;gap:6px;margin-top:10px;");
    var saveBtn = makePanelButton("SAVE");
    var resetBtn = makePanelButton("RESET");
    btnRow.appendChild(saveBtn); btnRow.appendChild(resetBtn);
    body.appendChild(btnRow);
    var status = document.createElement("div");
    status.setAttribute("style", "margin-top:6px;font-size:9px;color:hsl(165,35%,42%);min-height:12px;");
    body.appendChild(status);

    saveBtn.addEventListener("click", function () {
      try {
        window.localStorage.setItem("mh.graph.personParams", JSON.stringify(personParams));
      } catch (_) {}
      var snippet = "personParams = " + JSON.stringify(personParams, null, 2) + ";";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(snippet).then(
          function () { status.textContent = "saved + copied to clipboard"; },
          function () { status.textContent = "saved (clipboard blocked)"; console.log(snippet); }
        );
      } else {
        status.textContent = "saved (no clipboard api)";
      }
      console.log("[person-params]", personParams);
    });
    resetBtn.addEventListener("click", function () {
      for (var key in PERSON_PARAM_DEFAULTS) {
        personParams[key] = PERSON_PARAM_DEFAULTS[key];
        if (rows[key]) {
          rows[key].input.value = String(personParams[key]);
          rows[key].val.textContent = formatKnob(rows[key].knob, personParams[key]);
        }
      }
      try { window.localStorage.removeItem("mh.graph.personParams"); } catch (_) {}
      status.textContent = "reset to defaults";
    });

    container.appendChild(panel);
  }

  function formatKnob(knob, v) {
    return knob.step >= 1 ? String(Math.round(v)) : v.toFixed(2);
  }

  function makePanelButton(text) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = text;
    btn.setAttribute("style",
      "flex:1;background:hsl(165,25%,14%);border:1px solid hsl(165,30%,32%);" +
      "color:hsl(165,65%,78%);font:10px/1 'Share Tech Mono',monospace;" +
      "letter-spacing:0.12em;padding:6px 0;cursor:pointer;");
    return btn;
  }

  function setGraphOption(kind, value) {
    if (kind === "limit") {
      visibleLimit = value;
      if (window.localStorage) window.localStorage.setItem("mh.graph.limit", String(value));
    } else if (kind === "window") {
      activityWindowDays = value;
      if (window.localStorage) window.localStorage.setItem("mh.graph.window", String(value));
    }
    renderControls();
    loadGraphData();
  }

  // Deterministic zoom that frames agent-at-origin + person ring + artifact
  // reach. Using zoomToFit drifts when artifacts haven't settled yet.
  function fitView(duration) {
    if (!graph || !container) return;
    var rect = container.getBoundingClientRect();
    var targetRadius = PERSON_RING_RADIUS + ARTIFACT_LINK_DISTANCE + 40;
    var fitZoom = Math.min(rect.width, rect.height) / (2 * targetRadius);
    var d = duration == null ? 400 : duration;
    if (typeof graph.centerAt === "function") graph.centerAt(0, 0, d);
    if (typeof graph.zoom === "function" && isFinite(fitZoom) && fitZoom > 0) {
      graph.zoom(fitZoom, d);
    }
  }

  function resetNodePositions() {
    if (!graph) return;
    graphData.nodes.forEach(function (n) {
      n._userMoved = false;
      if (n.type === "agent") {
        n.x = 0; n.y = 0;
        n.fx = 0; n.fy = 0;
        n.vx = 0; n.vy = 0;
      } else if (n.type === "person") {
        var radius = Number(n.radius || PERSON_RING_RADIUS);
        var angle = Number(n.angle || 0);
        n.x = Math.cos(angle) * radius;
        n.y = Math.sin(angle) * radius;
        n.fx = n.x;
        n.fy = n.y;
        n.vx = 0; n.vy = 0;
      } else {
        n.fx = null;
        n.fy = null;
        n.vx = 0; n.vy = 0;
      }
    });
    graph.graphData(graphData);
    if (typeof graph.d3ReheatSimulation === "function") graph.d3ReheatSimulation();
    fitView();
  }

  function controlButton(kind, value, label, current) {
    return '<button type="button" class="graph-control-chip' + (value === current ? ' is-active' : '') + '"'
      + ' data-kind="' + kind + '" data-value="' + value + '">' + label + '</button>';
  }

  function actionButton(action, label) {
    return '<button type="button" class="graph-control-chip"'
      + ' data-action="' + action + '">' + label + '</button>';
  }

  function renderControls() {
    var controls = container.querySelector(".graph-controls");
    if (!controls) return;
    controls.innerHTML = ''
      + '<div class="graph-control-header">FLTR</div>'
      + '<div class="graph-control-row">'
      + '<span class="graph-control-label">NODES</span>'
      + controlButton("limit", 12, "12", visibleLimit)
      + controlButton("limit", 24, "24", visibleLimit)
      + controlButton("limit", 48, "48", visibleLimit)
      + '</div>'
      + '<div class="graph-control-row">'
      + '<span class="graph-control-label">RANGE</span>'
      + controlButton("window", 7, "7D", activityWindowDays)
      + controlButton("window", 30, "30D", activityWindowDays)
      + controlButton("window", 90, "90D", activityWindowDays)
      + '</div>'
      + '<div class="graph-control-row">'
      + '<span class="graph-control-label">VIEW</span>'
      + actionButton("fit", "FIT")
      + actionButton("reset", "RESET")
      + '</div>'
      + (graphMeta.hiddenPeople
          ? '<div class="graph-control-meta">' + graphMeta.hiddenPeople + ' hidden</div>'
          : '');
  }

  function ensureControls() {
    if (container.querySelector(".graph-controls")) return;
    var controls = document.createElement("div");
    controls.className = "graph-controls";
    controls.addEventListener("click", function (event) {
      if (!event.target || typeof event.target.closest !== "function") return;
      var btn = event.target.closest(".graph-control-chip");
      if (!btn) return;
      var action = btn.getAttribute("data-action");
      if (action === "fit") { fitView(); return; }
      if (action === "reset") { resetNodePositions(); return; }
      var value = Number(btn.getAttribute("data-value"));
      var kind = btn.getAttribute("data-kind");
      if (!isFinite(value) || !kind) return;
      setGraphOption(kind, value);
    });
    container.appendChild(controls);
    renderControls();
  }

  // ── Timestamp formatting ────────────────────────────────────────────────

  function formatTs(epoch) {
    if (!epoch) return null;
    var d = new Date(epoch);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return d.getFullYear() + "." + pad(d.getMonth() + 1) + "." + pad(d.getDate())
      + " // " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function formatDate(epoch) {
    if (!epoch) return null;
    var d = new Date(epoch);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return d.getFullYear() + "." + pad(d.getMonth() + 1) + "." + pad(d.getDate());
  }

  // ── Detail rendering helpers ──────────────────────────────────────────

  function fieldRow(label, value) {
    if (value == null || value === "") return "";
    return '<div class="node-detail-row">'
      + '<span class="node-detail-field">' + label + '</span> '
      + '<span class="node-detail-value">' + escapeHtml("" + value) + '</span>'
      + '</div>';
  }

  function fieldBlock(label, value) {
    if (value == null || value === "") return "";
    return '<div class="node-detail-block">'
      + '<div class="node-detail-field">' + label + '</div>'
      + '<p class="node-detail-block-text">' + escapeHtml("" + value) + '</p>'
      + '</div>';
  }

  function statusClass(status) {
    if (status === "completed") return "status-completed";
    if (status === "failed") return "status-failed";
    if (status === "cancelled") return "status-cancelled";
    return "status-pending";
  }

  function renderPersonDetail(data) {
    var h = fieldRow("PHONE", data.phone)
      + fieldRow("CALLS", data.callCount)
      + fieldRow("FACTS", data.factCount)
      + fieldRow("OPEN THREADS", data.pendingActionCount)
      + fieldRow("FIRST CONTACT", formatDate(data.firstSeen))
      + fieldRow("LAST CONTACT", formatDate(data.lastSeen));
    if (data.identified === false) h += renderRenameForm(data.id);
    var strands = data.strands || data.channels || [];
    if (strands.length) {
      h += '<div class="node-detail-block">'
        + '<div class="node-detail-field">STRANDS</div>'
        + '<div class="graph-thread-list">'
        + strands.map(function (strand) {
          return '<button type="button" class="graph-thread-pill" data-person-id="' + escapeHtml(data.id) + '" data-channel="' + escapeHtml(strand.channel) + '" data-modality="' + escapeHtml(strand.modality || "") + '">'
            + escapeHtml(strand.strandLabel || strand.channelLabel || strand.channel) + ' / ' + escapeHtml(String(strand.total || 0))
            + '</button>';
        }).join("")
        + '</div></div>';
    }
    if (data.summary) h += fieldBlock("SUMMARY", data.summary);
    return h;
  }

  function renderAgentDetail(data) {
    return fieldRow("CREATURE", data.creature)
      + fieldRow("IDENTITY", data.identityReady ? "written" : "empty")
      + fieldRow("SOUL", data.soulReady ? "written" : "empty")
      + fieldRow("JOURNAL", data.journalDepth)
      + fieldRow("PEOPLE", data.personCount)
      + fieldRow("CALLS", data.callCount)
      + fieldBlock("VIBE", data.vibe);
  }

  function renderRenameForm(personId) {
    return '<form class="graph-rename-form" data-person-id="' + escapeHtml(personId) + '">'
      + '<label class="node-detail-field" for="graph-rename-' + escapeHtml(personId) + '">IDENTIFY</label>'
      + '<div class="graph-rename-row">'
      + '<input id="graph-rename-' + escapeHtml(personId) + '" name="name" class="graph-rename-input" placeholder="Name this person" autocomplete="off">'
      + '<button type="submit" class="graph-rename-submit">SAVE</button>'
      + '</div>'
      + '</form>';
  }

  function renderCallDetail(data) {
    return fieldRow("CHANNEL", data.channelLabel)
      + fieldRow("DIRECTION", data.directionLabel || data.direction)
      + fieldRow("MODALITY", data.modalityLabel || data.modality)
      + fieldRow("AUDIENCE", data.audience)
      + fieldRow("STARTED", formatTs(data.startedAt))
      + fieldRow("ANSWERED", formatTs(data.answeredAt))
      + (data.summary ? fieldBlock("SUMMARY", data.summary) : "");
  }

  function renderFactDetail(data) {
    return fieldRow("TYPE", data.factType)
      + fieldRow("CONFIDENCE", data.confidence != null ? Math.round(data.confidence * 100) + "%" : null)
      + fieldRow("SOURCE", data.source)
      + fieldRow("RECORDED", formatTs(data.createdAt))
      + fieldBlock("CONTENT", data.content);
  }

  function renderActionDetail(data) {
    var statusHtml = '<span class="' + statusClass(data.status) + '">'
      + escapeHtml(data.status || "") + '</span>';
    var h = '<div class="node-detail-row">'
      + '<span class="node-detail-field">STATUS</span>'
      + '<span class="node-detail-value">' + statusHtml + '</span>'
      + '</div>';
    h += fieldRow("URGENCY", data.urgency)
      + fieldRow("DUE", formatTs(data.dueAt))
      + fieldRow("ATTEMPTS", data.attempts + " / " + data.maxAttempts)
      + fieldRow("CREATED", formatTs(data.createdAt));
    if (data.intent) h += fieldBlock("INTENT", data.intent);
    if (data.context) h += fieldBlock("CONTEXT", data.context);
    return h;
  }

  function renderConnections(node) {
    var connected = graphData.links
      .filter(function (l) {
        var sid = typeof l.source === "object" ? l.source.id : l.source;
        var tid = typeof l.target === "object" ? l.target.id : l.target;
        return sid === node.id || tid === node.id;
      })
      .map(function (l) {
        var sid = typeof l.source === "object" ? l.source.id : l.source;
        var tid = typeof l.target === "object" ? l.target.id : l.target;
        var otherId = sid === node.id ? tid : sid;
        return nodeById[otherId];
      })
      .filter(Boolean)
      .filter(function (n, index, list) {
        return list.findIndex(function (candidate) { return candidate.id === n.id; }) === index;
      });

    if (connected.length === 0) return "";

    // Group by type
    var groups = {};
    var order = ["agent", "person", "call", "action", "fact"];
    connected.forEach(function (n) {
      if (!groups[n.type]) groups[n.type] = [];
      groups[n.type].push(n);
    });

    var html = '<div class="node-detail-connections">'
      + '<div class="node-detail-section-head">CONNECTIONS \u2500\u2500 ' + connected.length + '</div>';

    order.forEach(function (type) {
      var items = groups[type];
      if (!items) return;
      html += '<div class="node-detail-conn-group">'
        + '<span class="node-detail-conn-type" data-type="' + type + '">'
        + type.toUpperCase() + ' (' + items.length + ')</span>';
      items.forEach(function (n) {
        html += '<button type="button" class="graph-search-item" data-node-id="' + n.id + '">'
          + '<span class="graph-search-label">' + escapeHtml(n.label || "") + '</span>'
          + '</button>';
      });
      html += '</div>';
    });

    html += '</div>';
    return html;
  }

  function renderThreadCalls(data) {
    var calls = data.calls || [];
    if (!calls.length) {
      return '<p class="graph-search-empty">No interactions in this thread yet.</p>';
    }
    return '<div class="graph-thread-calls">' + calls.map(function (call) {
      var title = call.summary || call.interactionLabel || "Interaction";
      var preview = call.transcriptPreview ? fieldBlock("THREAD", call.transcriptPreview) : "";
      return '<article class="graph-thread-call">'
        + '<div class="node-detail-section-head">' + escapeHtml(call.directionLabel || call.direction || "") + ' / ' + escapeHtml(call.interactionLabel || "") + '</div>'
        + '<div class="graph-thread-call-title">' + escapeHtml(title) + '</div>'
        + fieldRow("STARTED", formatTs(call.startedAt))
        + preview
        + '</article>';
    }).join("") + '</div>';
  }

  function renderEdgeSummary(link) {
    var personId = link.personId || "";
    var person = nodeById["p:" + personId] || nodeById[endpointId(link.target)] || {};
    return fieldRow("PERSON", person.label || person.phone)
      + fieldRow("STRAND", link.strandLabel || link.interactionLabel)
      + fieldRow("CHANNEL", link.channelLabel || link.channel)
      + fieldRow("MODALITY", link.modalityLabel || link.modality)
      + fieldRow("INTERACTIONS", link.total)
      + fieldRow("RECENT", link.recentCount)
      + fieldRow("INBOUND", link.inboundCount)
      + fieldRow("OUTBOUND", link.outboundCount)
      + fieldRow("LAST SIGNAL", formatTs(link.lastInteraction));
  }

  // ── Node detail panel ─────────────────────────────────────────────────

  function switchToSearchTab() {
    var tabs = document.querySelectorAll(".sidebar-tab");
    var panels = document.querySelectorAll(".sidebar-tab-panel");
    tabs.forEach(function (t) { t.classList.toggle("active", t.getAttribute("data-tab") === "search"); });
    panels.forEach(function (p) { p.classList.toggle("active", p.getAttribute("data-panel") === "search"); });
  }

  function showNodeDetail(node) {
    var results = document.getElementById("graph-search-results");
    if (!results) return;
    switchToSearchTab();

    // Immediate skeleton from graph data
    var typeLabel = (node.type || "").toUpperCase();
    results.innerHTML = '<div class="graph-node-detail" data-type="' + node.type + '">'
      + '<div class="graph-node-detail-type">' + typeLabel + '</div>'
      + '<div class="graph-node-detail-label">' + escapeHtml(node.label || "") + '</div>'
      + '<div class="node-detail-fields node-detail-loading">LOADING\u2026</div>'
      + '</div>';

    // Extract type prefix and entity ID from node.id (e.g. "p:uuid")
    var parts = node.id.split(":");
    var ntype = node.type === "agent" ? "agent" : { p: "person", c: "call", f: "fact", a: "action" }[parts[0]];
    var entityId = node.type === "agent" ? "agent" : parts.slice(1).join(":");
    if (!ntype || !entityId) return;

    fetch("/dashboard/api/graph/node/" + ntype + "/" + encodeURIComponent(entityId), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function (data) {
        var fields = "";
        if (data.type === "agent") fields = renderAgentDetail(data);
        else if (data.type === "person") fields = renderPersonDetail(data);
        else if (data.type === "call") fields = renderCallDetail(data);
        else if (data.type === "fact") fields = renderFactDetail(data);
        else if (data.type === "action") fields = renderActionDetail(data);

        var detail = '<div class="graph-node-detail" data-type="' + node.type + '">'
          + '<div class="graph-node-detail-type">' + typeLabel + '</div>'
          + '<div class="graph-node-detail-label">' + escapeHtml(data.name || data.intent || data.content || node.label || "") + '</div>'
          + '<div class="node-detail-fields">' + fields + '</div>'
          + renderConnections(node)
          + '</div>';
        results.innerHTML = detail;
      })
      .catch(function () {
        // Fallback: show connections from graph data only
        var detail = '<div class="graph-node-detail" data-type="' + node.type + '">'
          + '<div class="graph-node-detail-type">' + typeLabel + '</div>'
          + '<div class="graph-node-detail-label">' + escapeHtml(node.label || "") + '</div>'
          + renderConnections(node)
          + '</div>';
        results.innerHTML = detail;
      });
  }

  function showEdgeDetail(link) {
    var results = document.getElementById("graph-search-results");
    if (!results) return;
    switchToSearchTab();
    var sourceId = endpointId(link.source);
    var targetId = endpointId(link.target);
    var personId = link.personId || (sourceId === "agent" ? String(targetId).slice(2) : String(sourceId).slice(2));
    var channel = link.channel || "dashboard";
    var modality = link.modality || "text";
    var person = nodeById["p:" + personId] || {};
    var title = (person.label || "Thread") + " / " + (link.strandLabel || link.interactionLabel || link.channelLabel || channel);
    results.innerHTML = '<div class="graph-node-detail" data-type="thread">'
      + '<div class="graph-node-detail-type">THREAD</div>'
      + '<div class="graph-node-detail-label">' + escapeHtml(title) + '</div>'
      + '<div class="node-detail-fields">' + renderEdgeSummary(link) + '</div>'
      + '<div class="node-detail-fields node-detail-loading">LOADING...</div>'
      + '</div>';

    fetch("/dashboard/api/graph/thread/" + encodeURIComponent(personId) + "/" + encodeURIComponent(channel) + "/" + encodeURIComponent(modality), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function (data) {
        results.innerHTML = '<div class="graph-node-detail" data-type="thread">'
          + '<div class="graph-node-detail-type">THREAD</div>'
          + '<div class="graph-node-detail-label">' + escapeHtml(title) + '</div>'
          + '<div class="node-detail-fields">' + renderEdgeSummary(link) + '</div>'
          + renderThreadCalls(data)
          + '</div>';
      })
      .catch(function () {
        results.innerHTML = '<div class="graph-node-detail" data-type="thread">'
          + '<div class="graph-node-detail-type">THREAD</div>'
          + '<div class="graph-node-detail-label">' + escapeHtml(title) + '</div>'
          + '<div class="node-detail-fields">' + renderEdgeSummary(link) + '</div>'
          + '<p class="graph-search-empty">Thread unavailable.</p>'
          + '</div>';
      });
  }

  function clearNodeDetail() {
    var results = document.getElementById("graph-search-results");
    if (results && results.querySelector(".graph-node-detail")) {
      results.innerHTML = '<div class="node-detail-empty">'
        + '<div class="node-detail-empty-line">SELECT NODE</div>'
        + '<div class="node-detail-empty-line">TO INSPECT</div>'
        + '</div>';
    }
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ── New-node glow decay ────────────────────────────────────────────────────

  function scheduleGlowDecay(nodeId) {
    newNodeIds.add(nodeId);
    setTimeout(function () {
      newNodeIds.delete(nodeId);
    }, 2000);
  }

  // ── Load initial data ─────────────────────────────────────────────────────

  var _initialLoadDone = false;

  function loadGraphData() {
    var url = "/dashboard/api/graph?n=" + encodeURIComponent(visibleLimit)
      + "&window=" + encodeURIComponent(activityWindowDays);
    fetch(url, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function (data) {
        var incomingNodes = data.nodes || [];
        var incomingLinks = data.links || [];
        graphMeta = data.meta || {};
        renderControls();
        var newNodeById = {};
        var addedIds = new Set();

        // Stable graph slots: agent at origin; people at permanent chronological angles.
        // User-dragged nodes keep their position across refreshes until RESET.
        incomingNodes.forEach(function (n) {
          var existing = nodeById[n.id];
          if (!existing) {
            addedIds.add(n.id);
          }
          if (existing && existing._userMoved) {
            n.x = existing.x;
            n.y = existing.y;
            n.fx = existing.fx;
            n.fy = existing.fy;
            n.vx = 0;
            n.vy = 0;
            n._userMoved = true;
          } else if (n.type === "agent") {
            n.fx = 0;
            n.fy = 0;
            if (n.x == null) n.x = 0;
            if (n.y == null) n.y = 0;
          } else if (n.type === "person") {
            var radius = Number(n.radius || PERSON_RING_RADIUS);
            var angle = Number(n.angle || 0);
            n.x = Math.cos(angle) * radius;
            n.y = Math.sin(angle) * radius;
            n.fx = n.x;
            n.fy = n.y;
            n.vx = 0;
            n.vy = 0;
          }
          newNodeById[n.id] = n;
        });

        // Deduplicate parallel channel strands by channel.
        var newLinkSet = new Set();
        var mergedLinks = [];
        incomingLinks.forEach(function (l) {
          var key = linkKey(l.source, l.target, l.channel, l.modality);
          if (newLinkSet.has(key)) return;
          newLinkSet.add(key);
          mergedLinks.push(l);
        });

        var linksByPerson = {};
        mergedLinks.forEach(function (link) {
          var personId = link.personId || endpointId(link.target);
          if (!linksByPerson[personId]) linksByPerson[personId] = [];
          linksByPerson[personId].push(link);
        });
        Object.keys(linksByPerson).forEach(function (personId) {
          var group = linksByPerson[personId];
          group.sort(function (a, b) {
            return String(a.strandLabel || a.channel || "").localeCompare(String(b.strandLabel || b.channel || ""));
          });
          group.forEach(function (link, index) {
            var centered = index - (group.length - 1) / 2;
            link.strandOffset = centered * 8;
            link.strandCurve = centered * 0.08;
          });
        });

        var wasInitial = !_initialLoadDone;
        // Glow new nodes (skip initial load)
        if (_initialLoadDone) {
          addedIds.forEach(function (id) { scheduleGlowDecay(id); });
        }
        _initialLoadDone = true;

        // Update state
        graphData = { nodes: incomingNodes, links: mergedLinks };
        nodeById = newNodeById;
        linkSet = newLinkSet;
        graph.graphData(graphData);
        recomputeVoiceStrandKey();
        configureForces();
        if (typeof graph.d3ReheatSimulation === "function") {
          graph.d3ReheatSimulation();
        }

        // Frame the constellation on first load — see fitView() above.
        if (wasInitial) {
          setTimeout(function () { fitView(600); }, 50);
        }
      })
      .catch(function () {});
  }

  // ── SSE live updates ──────────────────────────────────────────────────────

  function wireSSE() {
    // Listen to activity events dispatched by shell.js SSE listener
    document.addEventListener("mh:activity", function (event) {
      handleActivityEvent(event.detail);
    });
    document.addEventListener("mh:signal", handleSignalEvent);
  }

  function wireDetailEvents() {
    document.addEventListener("submit", function (event) {
      if (!event.target || typeof event.target.closest !== "function") return;
      var form = event.target.closest(".graph-rename-form");
      if (!form) return;
      event.preventDefault();
      var personId = form.getAttribute("data-person-id");
      var input = form.querySelector("input[name='name']");
      var name = input ? input.value.trim() : "";
      if (!personId || !name) return;
      fetch("/dashboard/api/graph/person/" + encodeURIComponent(personId) + "/name", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name }),
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      }).then(function () {
        loadGraphData();
        var node = nodeById["p:" + personId];
        if (node) {
          node.label = name;
          node.identified = true;
          showNodeDetail(node);
        }
      }).catch(function () {
        if (input) input.setAttribute("aria-invalid", "true");
      });
    });

    document.addEventListener("click", function (event) {
      if (!event.target || typeof event.target.closest !== "function") return;
      var pill = event.target.closest(".graph-thread-pill");
      if (!pill) return;
      var personId = pill.getAttribute("data-person-id");
      var channel = pill.getAttribute("data-channel");
      var modality = pill.getAttribute("data-modality");
      var link = graphData.links.find(function (l) {
        return l.personId === personId && l.channel === channel && l.modality === modality;
      });
      if (link) showEdgeDetail(link);
    });
  }

  function recomputeVoiceStrandKey() {
    voiceStrandKey = null;
    var owner = findOwnerPersonNode();
    if (!owner) return;
    var personId = owner.entityId || String(owner.id).slice(2);
    var strand = findStrandLink(personId, "dashboard", "voice")
              || findStrandLink(personId, "dashboard", "mixed")
              || findStrandLink(personId, "dashboard", "text")
              || findStrandLink(personId, "dashboard", null);
    if (!strand) return;
    voiceStrandKey = linkKey(strand.source, strand.target, strand.channel, strand.modality);
  }

  function findOwnerPersonNode() {
    var people = graphData.nodes.filter(function (n) { return n.type === "person"; });
    var namedOwner = people.find(function (n) {
      return (n.label || "").trim().toLowerCase() === "owner";
    });
    if (namedOwner) return namedOwner;

    var agentLink = graphData.links.find(function (l) {
      var sid = endpointId(l.source);
      var tid = endpointId(l.target);
      return sid === "agent" || tid === "agent";
    });
    if (agentLink) {
      var otherId = endpointId(agentLink.source) === "agent"
        ? endpointId(agentLink.target)
        : endpointId(agentLink.source);
      if (nodeById[otherId] && nodeById[otherId].type === "person") {
        return nodeById[otherId];
      }
    }

    return people[0] || null;
  }

  function handleSignalEvent(event) {
    if (!graph) return;
    var detail = event.detail || {};
    var speaker = detail.speaker === "agent" ? "agent" : detail.speaker === "user" ? "user" : null;
    if (!speaker) return;

    var agentNode = nodeById.agent;
    var personNode = findOwnerPersonNode();
    if (!agentNode || !personNode) return;

    var channel = normalizeSignalChannel(detail.channel);
    var modality = normalizeSignalModality(detail.modality);
    var strand = findStrandLink(personNode.entityId || String(personNode.id).slice(2), channel, modality);
    var fromId = speaker === "agent" ? agentNode.id : personNode.id;
    var toId = speaker === "agent" ? personNode.id : agentNode.id;
    signals.push({
      fromId: fromId,
      toId: toId,
      t: 0,
      channel: strand ? strand.channel : channel,
      modality: strand ? strand.modality : modality,
      offset: strand ? strandOffset(strand) : 0,
    });
    lastSignalFrame = lastSignalFrame === null ? null : performance.now();
    if (typeof graph.resumeAnimation === "function") graph.resumeAnimation();
  }

  var _pendingRefetch = null;
  var _refetchDeadline = 0;

  function scheduleRefetch(delayMs) {
    var now = Date.now();
    if (_pendingRefetch) {
      clearTimeout(_pendingRefetch);
      // If we've been deferring too long, fire immediately
      if (_refetchDeadline && now >= _refetchDeadline) {
        _pendingRefetch = null;
        _refetchDeadline = 0;
        loadGraphData();
        return;
      }
    } else {
      _refetchDeadline = now + 3000;
    }
    _pendingRefetch = setTimeout(function () {
      _pendingRefetch = null;
      _refetchDeadline = 0;
      loadGraphData();
    }, delayMs || 500);
  }

  function handleActivityEvent(d) {
    if (!graph) return;

    var type = d.type;

    if (type === "facts_extracted" || type === "actions_extracted") {
      scheduleRefetch(300);
      return;
    }
    if (type === "live_voice_started" || type === "live_voice_connected") {
      // Dashboard session started — call now exists in active_calls
      scheduleRefetch(300);
      return;
    }
    if (type === "call_started") {
      scheduleRefetch(250);
      return;
    }
    if (type === "call_ended") {
      // Refetch after delay — extraction runs shortly after call ends
      scheduleRefetch(3000);
      return;
    }
    if (type === "action_completed" || type === "action_cancelled") {
      if (type === "action_completed") triggerAgentArc();
      scheduleRefetch(500);
      return;
    }
    // Any other activity event — refetch to stay current
    scheduleRefetch(1000);
  }

  // ── Search integration ─────────────────────────────────────────────────────

  // Expose search function for sidebar
  window.MysticGraph = {
    search: function (term) {
      searchTerm = (term || "").trim();
      if (graph) graph.nodeCanvasObject(graph.nodeCanvasObject()); // force repaint
      return getSearchResults(term);
    },
    focusNode: function (nodeId) {
      if (!graph) return;
      var node = nodeById[nodeId];
      if (!node) return;
      graph.centerAt(node.x, node.y, 600);
      graph.zoom(4, 600);
      hoverNode = node;
      scheduleGlowDecay(nodeId);
      showNodeDetail(node);
    },
    getAgentScreenPosition: getAgentScreenPosition,
    getNodes: function () { return graphData.nodes; },
  };

  function getSearchResults(term) {
    if (!term) return [];
    var t = term.toLowerCase();
    return graphData.nodes.filter(function (n) {
      return (n.label || "").toLowerCase().indexOf(t) !== -1;
    }).slice(0, 30);
  }

  // ── Resize ─────────────────────────────────────────────────────────────────

  function handleResize() {
    if (!graph || !container) return;
    graph.width(container.clientWidth);
    graph.height(container.clientHeight);
    resizeAgentCloud();
  }

  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(handleResize, 100);
  });

  // Also observe the container for sidebar toggle resizes
  if (window.ResizeObserver) {
    new ResizeObserver(function () { handleResize(); }).observe(container);
  }

  // ── Boot ───────────────────────────────────────────────────────────────────

  initGraph();
  ensureControls();
  loadGraphData();
  wireSSE();
  wireDetailEvents();
})();
