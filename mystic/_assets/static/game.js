(function () {
  "use strict";

  // ── Constants ─────────────────────────────────────────────────────────
  var TAU = Math.PI * 2;

  var SHIP_SIZE = 14;
  var SHIP_THRUST = 260;
  var SHIP_FRICTION = 0.992;
  var SHIP_ROTATE_RATE = 4.5;
  var SHIP_FIRE_COOLDOWN_MS = 120;
  var SHIP_HYPER_COOLDOWN_MS = 4500;
  var SHIP_RESPAWN_INVULN_MS = 2500;
  var SHIP_MAX_SPEED = 420;

  var BULLET_SPEED = 720;
  var BULLET_TTL_MS = 1300;
  var BULLET_MAX_IN_FLIGHT = 28;
  var BULLET_SIZE = 3.4;
  var BULLET_DESPAWN_MARGIN = BULLET_SIZE;
  var BULLET_HIT_PADDING = 8;
  var RAPID_FIRE_COOLDOWN_MS = 55;
  var SPREAD_FIRE_COOLDOWN_MS = 135;

  var ASTEROID_SIZES = { L: 42, M: 24, S: 12 };
  var ASTEROID_SCORES = { L: 20, M: 50, S: 100 };
  var ASTEROID_SPLITS = { L: ["M", "M"], M: ["S", "S"], S: [] };
  var ASTEROID_VERT_COUNT = 11;
  var ASTEROID_JAG = 0.38;
  var ASTEROID_SPEED_MIN = 30;
  var ASTEROID_SPEED_MAX = 95;

  var WAVE_FIRST = 1;
  var WAVE_BASE_LARGE = 4;
  var WAVE_STEP = 1;
  var WAVE_INTERMISSION_MS = 1800;
  var NEAR_MISS_RADIUS = 38;

  var STAR_LAYERS = [
    { count: 90, speed: 0.08, brightness: 0.35 },
    { count: 60, speed: 0.22, brightness: 0.55 },
    { count: 24, speed: 0.45, brightness: 0.85 },
  ];

  var WARP_IN_MS = 1500;
  var WARP_OUT_MS = 1800;
  var WARP_STAR_COUNT = 180;
  var WARP_SCENE_MIN_SCALE = 0.055;

  var COLOR_BG = "#000";
  var COLOR_GLOW = "hsl(165, 55%, 65%)";
  var COLOR_GLOW_SOFT = "hsl(165, 55%, 75%)";
  var COLOR_MID = "hsl(165, 35%, 42%)";
  var COLOR_DIM = "hsl(165, 20%, 24%)";
  var COLOR_WARN = "hsl(30, 55%, 55%)";
  var COLOR_ALERT = "hsl(0, 55%, 55%)";

  var KEY_MAP = {
    ArrowLeft: "left", KeyA: "left",
    ArrowRight: "right", KeyD: "right",
    ArrowUp: "thrust", KeyW: "thrust",
    Space: "fire",
    ShiftLeft: "hyper", ShiftRight: "hyper",
  };

  // ── DOM refs (resolved on first start) ────────────────────────────────
  var overlay = null, canvas = null, ctx = null, modal = null, modalBox = null;
  var hudScoreEl = null, hudWaveEl = null, hudLivesEl = null;
  var narratorEl = null, narratorTextEl = null;
  var gameHudAgentWave = null, gameHudUserWave = null, gameHudRttEl = null;
  var gameHudAgentTxt = null, gameHudUserTxt = null, gameHudTraceEl = null;
  var gameHudSysDot = null, gameHudSysState = null, gameHudSysTimer = null, gameHudSysMode = null;
  var gameHudMicBtn = null;
  var gameMusicVolumeInput = null, gameSfxVolumeInput = null;
  var gameMusicVolumeValue = null, gameSfxVolumeValue = null;
  var narratorHideAt = 0;
  var micListenerBound = false;
  var audioControlsBound = false;

  // ── Runtime state ─────────────────────────────────────────────────────
  var mode = "idle";              // "idle" | "warp_in" | "playing" | "paused" | "intermission" | "game_over" | "warp_out"
  var rafId = null;
  var lastFrameMs = 0;
  var width = 0, height = 0;

  var ship = null;
  var asteroids = [];
  var bullets = [];
  var particles = [];
  var weaponEffects = [];
  var stars = [];

  var warp = null;                // { phase: "in"|"out", t: 0..1, originX, originY, stars: [...] }
  var warpPortalPoint = null;     // Last known dashboard AGENT node position in game-canvas coordinates.
  var inputState = { left: false, right: false, thrust: false, fire: false, hyper: false };
  var lastFireMs = 0;
  var lastHyperMs = 0;
  var invulnUntilMs = 0;
  var nearMissFiredAtMs = 0;

  var score = 0;
  var wave = 0;
  var lives = 3;
  var highScore = 0;
  var highScoreFired = false;
  var waveEndAtMs = 0;
  var gameOverAtMs = 0;

  // Streak + nudge-gate tracking. All reset on ship hit / wave change / game_over
  // so the Harbormaster only fires these once per (life|wave|run).
  var killStreak = 0;
  var killStreakThresholds = [5, 10, 15];
  var killStreakNextThresholdIdx = 0;
  var waveStartMs = 0;
  var waveStalledFired = false;
  var lastInputMs = 0;
  var idleFiredForLife = false;

  // ── Ship systems ──────────────────────────────────────────────────────
  // The Copilot grants ship systems via LLM tool calls → data-channel
  // packets. The client is authoritative on cooldowns, mechanics, and
  // visuals. The copilot kit ships with:
  //   shield       — atomic-orbit deflector cloud (finite hits, 2-wave recharge)
  //   shift-jump   — teleport + short invuln + blast-clear (6s cooldown)
  //   force field  — 10s repulsor + full invulnerability (1-wave cooldown)
  //   lightning    — chain weapon, unlocked from wave 1
  //   voice weapon — shockwave weapon, unlocks at wave 2
  //   shot pattern — single / spread / rapid (persists until changed)

  var SHIELD_MAX_PARTICLES = 10;      // matches graph person-node: 5 shells × 2 motes
  var SHIELD_HITS_BY_SIZE = { L: 3, M: 2, S: 1 };
  var SHIELD_COOLDOWN_WAVES = 2;

  var SHIFT_JUMP_COOLDOWN_MS = 6000;
  var SHIFT_JUMP_INVULN_MS = 2500;
  var SHIFT_JUMP_BLAST_RADIUS = SHIP_SIZE * 7.5;

  var FORCE_FIELD_DURATION_MS = 10000;
  var FORCE_FIELD_COOLDOWN_WAVES = 1;
  var FORCE_FIELD_RADIUS = SHIP_SIZE * 7.2;
  var FORCE_FIELD_REPEL_ACCEL = 980;
  var FORCE_FIELD_BOUNCE = 1.65;
  var FORCE_FIELD_POSITION_PUSH = 0.36;
  var FORCE_FIELD_ASTEROID_MAX_SPEED = 280;

  var LIGHTNING_UNLOCK_WAVE = 1;
  var LIGHTNING_COOLDOWN_MS = 5500;
  var LIGHTNING_RANGE = 430;
  var LIGHTNING_MAX_TARGETS = 8;

  var VOICE_WEAPON_UNLOCK_WAVE = 2;
  var VOICE_WEAPON_COOLDOWN_MS = 7500;
  var VOICE_WEAPON_BASE_RADIUS = 310;
  var VOICE_WEAPON_ENERGY_RADIUS = 230;

  var SHOT_PATTERNS = { single: true, spread: true, rapid: true };

  // Shield state
  var shieldParticlesLeft = 0;
  var shieldOrbitals = [];            // array of {theta, phase, shell, paired} — shell+pair drive the atomic orbit
  var shieldCooldownWaves = 0;
  var shieldHitFlashUntil = 0;        // monotonic-ms; brightens the envelope after absorb

  // Shift-jump state — tracks the last jump's end-time for cooldown.
  var shiftJumpReadyAt = 0;

  // Force-field state — full-invuln window + per-wave cooldown.
  var forceFieldUntil = 0;
  var forceFieldCooldownWaves = 0;

  // Weapon cooldown state.
  var lightningReadyAt = 0;
  var voiceWeaponReadyAt = 0;

  // Active shot pattern. Persists across waves until the Copilot changes it.
  var shotPattern = "single";

  var initials = ["A", "A", "A"];
  var initialIdx = 0;
  var leaderboard = [];
  var justSubmittedScoreId = null;
  var scoreLocked = false;
  var modalKeyHandler = null;

  // ── Utility ───────────────────────────────────────────────────────────
  function rand(min, max) { return min + Math.random() * (max - min); }
  function randSign() { return Math.random() < 0.5 ? -1 : 1; }
  function wrap(v, max) { return ((v % max) + max) % max; }
  function dist2(ax, ay, bx, by) { var dx = ax - bx, dy = ay - by; return dx * dx + dy * dy; }
  function isFiniteNumber(v) { return typeof v === "number" && isFinite(v); }
  function clamp(v, min, max) {
    if (!isFiniteNumber(v)) return min;
    return Math.max(min, Math.min(max, v));
  }
  function easeInOutCubic(t) {
    t = clamp(t, 0, 1);
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }
  function smoothstep(edge0, edge1, x) {
    if (edge0 === edge1) return x >= edge1 ? 1 : 0;
    var t = clamp((x - edge0) / (edge1 - edge0), 0, 1);
    return t * t * (3 - 2 * t);
  }
  function wrappedDelta(from, to, max) {
    var d = to - from;
    if (d > max / 2) d -= max;
    else if (d < -max / 2) d += max;
    return d;
  }
  function wrappedVector(ax, ay, bx, by) {
    return {
      dx: wrappedDelta(ax, bx, width),
      dy: wrappedDelta(ay, by, height),
    };
  }
  function wrappedDist2(ax, ay, bx, by) {
    var v = wrappedVector(ax, ay, bx, by);
    return v.dx * v.dx + v.dy * v.dy;
  }
  function clampBodySpeed(body, maxSpeed) {
    var speed = Math.sqrt(body.vx * body.vx + body.vy * body.vy);
    if (speed > maxSpeed) {
      body.vx = body.vx / speed * maxSpeed;
      body.vy = body.vy / speed * maxSpeed;
    }
  }

  // ── Self-contained LiveKit bridge ─────────────────────────────────────
  // The game owns its own LiveKit room. It is unrelated to the dashboard
  // session — a fresh room is created on warp-in via /dashboard/api/game/token
  // and torn down on warp-out by disconnecting the local participant.
  //
  // The overlay carries a game-scoped clone of the dashboard HUD strip.
  // Game transcripts and analysers only touch those nodes; the normal
  // dashboard shell keeps its own LiveKit room and DOM state.
  var gameRoom = null;
  var gameConnected = false;
  var gameMicActive = false;
  var gameRemoteAudioEls = [];
  var gameAudioCtx = null;
  var gameAgentAnalyser = null;
  var gameAgentAnalyserTrack = null;
  var gameUserAnalyser = null;
  var gameUserAnalyserTrack = null;
  var gameWaveformRaf = null;
  var gameAgentWavePulse = 0;
  var gameUserWavePulse = 0;
  var gameSessionStart = 0;
  var gameSessionTimerInterval = null;
  var resumeDashboardAfterGame = false;
  var MUSIC_BPM = 92;
  var MUSIC_STEP_SEC = 60 / MUSIC_BPM / 2;
  var MUSIC_LOOKAHEAD_SEC = 0.65;
  var MUSIC_TIMER_MS = 80;
  var MUSIC_FULL_GAIN = 2.15;
  var MUSIC_PAUSED_GAIN = 0.38;
  var MUSIC_GAME_OVER_GAIN = 0.72;
  var MUSIC_WARP_OUT_GAIN = 0.42;
  var MUSIC_DUCK_FACTOR = 0.68;
  var SFX_OUTPUT_GAIN = 7.25;
  var MUSIC_VOLUME_KEY = "mh.game.musicVolume";
  var SFX_VOLUME_KEY = "mh.game.sfxVolume";
  var gameMusicCtx = null;
  var gameMusicMaster = null;
  var gameSfxMaster = null;
  var gameMusicFilter = null;
  var gameMusicDelay = null;
  var gameMusicFeedback = null;
  var gameSfxCompressor = null;
  var gameMusicTimer = null;
  var gameMusicStep = 0;
  var gameMusicNextTime = 0;
  var gameMusicTargetGain = 0;
  var gameMusicPlaying = false;
  var gameMusicNoiseBuffer = null;
  var gameMusicVolume = readStoredVolume(MUSIC_VOLUME_KEY, 0.85);
  var gameSfxVolume = readStoredVolume(SFX_VOLUME_KEY, 0.78);

  function ensureGameAudioContext() {
    if (!gameAudioCtx) {
      gameAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (gameAudioCtx.state === "suspended") {
      gameAudioCtx.resume().catch(function () {});
    }
    return gameAudioCtx;
  }

  function ensureGameMusicContext() {
    if (!gameMusicCtx) {
      gameMusicCtx = new (window.AudioContext || window.webkitAudioContext)();
      gameMusicMaster = gameMusicCtx.createGain();
      gameMusicMaster.gain.value = 0;
      gameSfxMaster = gameMusicCtx.createGain();
      gameSfxMaster.gain.value = gameSfxVolume * SFX_OUTPUT_GAIN;
      gameSfxCompressor = gameMusicCtx.createDynamicsCompressor();
      gameSfxCompressor.threshold.value = -10;
      gameSfxCompressor.knee.value = 14;
      gameSfxCompressor.ratio.value = 3.5;
      gameSfxCompressor.attack.value = 0.004;
      gameSfxCompressor.release.value = 0.12;
      gameMusicFilter = gameMusicCtx.createBiquadFilter();
      gameMusicFilter.type = "lowpass";
      gameMusicFilter.frequency.value = 3600;
      gameMusicFilter.Q.value = 0.7;
      gameMusicDelay = gameMusicCtx.createDelay(0.6);
      gameMusicDelay.delayTime.value = 0.285;
      gameMusicFeedback = gameMusicCtx.createGain();
      gameMusicFeedback.gain.value = 0.18;
      gameMusicFilter.connect(gameMusicDelay);
      gameMusicDelay.connect(gameMusicFeedback);
      gameMusicFeedback.connect(gameMusicDelay);
      gameMusicDelay.connect(gameMusicMaster);
      gameMusicFilter.connect(gameMusicMaster);
      gameMusicMaster.connect(gameMusicCtx.destination);
      gameSfxMaster.connect(gameSfxCompressor);
      gameSfxCompressor.connect(gameMusicCtx.destination);
    }
    if (gameMusicCtx.state === "suspended") {
      gameMusicCtx.resume().catch(function () {});
    }
    return gameMusicCtx;
  }

  function resumeGameMusicContext() {
    if (!gameMusicCtx || gameMusicCtx.state !== "suspended") return;
    gameMusicCtx.resume().catch(function () {});
  }

  function gameMusicRamp(value, when) {
    if (!gameMusicMaster || !gameMusicCtx) return;
    var t = when || gameMusicCtx.currentTime;
    gameMusicMaster.gain.cancelScheduledValues(t);
    gameMusicMaster.gain.setTargetAtTime(value * gameMusicVolume, t, 0.22);
  }

  function startGameMusic() {
    var ctx = ensureGameMusicContext();
    gameMusicPlaying = true;
    gameMusicStep = 0;
    gameMusicNextTime = ctx.currentTime + 0.05;
    gameMusicTargetGain = MUSIC_FULL_GAIN;
    gameMusicRamp(gameMusicTargetGain, ctx.currentTime);
    if (!gameMusicTimer) {
      gameMusicTimer = window.setInterval(scheduleGameMusic, MUSIC_TIMER_MS);
    }
    scheduleGameMusic();
  }

  function stopGameMusic(fadeSec) {
    if (!gameMusicCtx || !gameMusicMaster) return;
    var ctx = gameMusicCtx;
    gameMusicPlaying = false;
    gameMusicTargetGain = 0;
    if (gameMusicTimer) {
      window.clearInterval(gameMusicTimer);
      gameMusicTimer = null;
    }
    gameMusicMaster.gain.cancelScheduledValues(ctx.currentTime);
    gameMusicMaster.gain.setTargetAtTime(0, ctx.currentTime, Math.max(0.03, fadeSec || 0.18));
  }

  function closeGameMusic() {
    stopGameMusic(0.08);
    if (gameMusicCtx) {
      gameMusicCtx.close().catch(function () {});
      gameMusicCtx = null;
    }
    gameMusicMaster = null;
    gameSfxMaster = null;
    gameMusicFilter = null;
    gameMusicDelay = null;
    gameMusicFeedback = null;
    gameSfxCompressor = null;
    gameMusicNoiseBuffer = null;
  }

  function setGameMusicPaused(paused) {
    if (!gameMusicCtx || !gameMusicMaster) return;
    resumeGameMusicContext();
    gameMusicTargetGain = paused ? MUSIC_PAUSED_GAIN : MUSIC_FULL_GAIN;
    gameMusicRamp(gameMusicTargetGain, gameMusicCtx.currentTime);
  }

  function readStoredVolume(key, fallback) {
    try {
      var raw = window.localStorage ? window.localStorage.getItem(key) : null;
      if (raw == null || raw === "") return fallback;
      return clamp(Number(raw), 0, 1);
    } catch (_e) {
      return fallback;
    }
  }

  function saveStoredVolume(key, value) {
    try {
      if (window.localStorage) window.localStorage.setItem(key, String(clamp(value, 0, 1)));
    } catch (_e) {}
  }

  function volumePercent(value) {
    return String(Math.round(clamp(value, 0, 1) * 100));
  }

  function setGameMusicVolume(value) {
    gameMusicVolume = clamp(value, 0, 1);
    saveStoredVolume(MUSIC_VOLUME_KEY, gameMusicVolume);
    if (gameMusicVolumeInput) gameMusicVolumeInput.value = volumePercent(gameMusicVolume);
    if (gameMusicVolumeValue) gameMusicVolumeValue.textContent = volumePercent(gameMusicVolume);
    if (gameMusicMaster && gameMusicCtx) gameMusicRamp(gameMusicTargetGain, gameMusicCtx.currentTime);
  }

  function setGameSfxVolume(value) {
    gameSfxVolume = clamp(value, 0, 1);
    saveStoredVolume(SFX_VOLUME_KEY, gameSfxVolume);
    if (gameSfxVolumeInput) gameSfxVolumeInput.value = volumePercent(gameSfxVolume);
    if (gameSfxVolumeValue) gameSfxVolumeValue.textContent = volumePercent(gameSfxVolume);
    if (gameSfxMaster && gameMusicCtx) {
      gameSfxMaster.gain.cancelScheduledValues(gameMusicCtx.currentTime);
      gameSfxMaster.gain.setTargetAtTime(gameSfxVolume * SFX_OUTPUT_GAIN, gameMusicCtx.currentTime, 0.04);
    }
  }

  function refreshGameAudioControls() {
    setGameMusicVolume(gameMusicVolume);
    setGameSfxVolume(gameSfxVolume);
  }

  function updateGameMusicMix() {
    if (!gameMusicCtx || !gameMusicMaster || !gameMusicPlaying) return;
    resumeGameMusicContext();
    var voiceEnergy = Math.max(gameRmsEnergy(gameAgentAnalyser), gameRmsEnergy(gameUserAnalyser));
    var desired = mode === "paused" ? MUSIC_PAUSED_GAIN : MUSIC_FULL_GAIN;
    if (mode === "game_over") desired = MUSIC_GAME_OVER_GAIN;
    if (mode === "warp_out") desired = MUSIC_WARP_OUT_GAIN;
    if (voiceEnergy > 0.025 || narratorHideAt > performance.now()) desired *= MUSIC_DUCK_FACTOR;
    if (Math.abs(desired - gameMusicTargetGain) > 0.006) {
      gameMusicTargetGain = desired;
      gameMusicRamp(desired, gameMusicCtx.currentTime);
    }
  }

  function scheduleGameMusic() {
    if (!gameMusicCtx || !gameMusicFilter || !gameMusicPlaying) return;
    var ctx = gameMusicCtx;
    if (gameMusicNextTime < ctx.currentTime - MUSIC_STEP_SEC) {
      gameMusicNextTime = ctx.currentTime + 0.02;
    }
    var horizon = ctx.currentTime + MUSIC_LOOKAHEAD_SEC;
    while (gameMusicNextTime < horizon) {
      scheduleMusicStep(gameMusicStep, gameMusicNextTime);
      gameMusicStep = (gameMusicStep + 1) % 64;
      gameMusicNextTime += currentMusicStepSec();
    }
  }

  function scheduleMusicStep(step, time) {
    var beatSec = currentMusicStepSec();
    var tension = gameMusicTension();
    var root = 73.42; // D2: still ominous, but audible on laptop speakers.
    var pulse = step % 2 === 0 ? root : root * Math.pow(2, 1 / 12);
    var accent = step % 8 === 0;
    scheduleMusicTone(pulse, pulse * 0.985, Math.min(0.34, beatSec * 0.76), "sawtooth", accent ? 0.34 : 0.26, 980 + tension * 620);
    scheduleMusicTone(pulse * 0.5, pulse * 0.49, Math.min(0.46, beatSec * 0.92), "sine", accent ? 0.22 : 0.15, 420);
    scheduleMusicTone(pulse * 2, pulse * 1.98, Math.min(0.09, beatSec * 0.25), "triangle", accent ? 0.075 : 0.045, 2200);
    if (step % 4 === 2) {
      scheduleMusicNoise(time + beatSec * 0.38, 0.045, 0.065 + tension * 0.04);
    }
    if (step % 16 === 14) {
      scheduleMusicTone(root * 4 * Math.pow(2, 7 / 12), time + beatSec * 0.12, 0.12, "triangle", 0.075 + tension * 0.05, 1800);
    }
  }

  function gameMusicTension() {
    if (mode !== "playing" && mode !== "intermission") return 0;
    var wavePressure = clamp((wave - 1) / 7, 0, 0.55);
    var countPressure = asteroids.length > 0 ? clamp(1 - asteroids.length / 14, 0, 0.65) : 0.2;
    var lifePressure = clamp((3 - lives) * 0.16, 0, 0.32);
    return clamp(wavePressure + countPressure + lifePressure, 0, 1);
  }

  function currentMusicStepSec() {
    return MUSIC_STEP_SEC * (1 - gameMusicTension() * 0.46);
  }

  function scheduleMusicTone(freq, time, dur, type, gain, cutoff) {
    var ctx = gameMusicCtx;
    if (!ctx || !gameMusicFilter) return;
    var osc = ctx.createOscillator();
    var env = ctx.createGain();
    var filt = ctx.createBiquadFilter();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, time);
    filt.type = "lowpass";
    filt.frequency.setValueAtTime(cutoff, time);
    filt.Q.value = 0.55;
    env.gain.setValueAtTime(0.0001, time);
    env.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), time + 0.018);
    env.gain.exponentialRampToValueAtTime(0.0001, time + dur);
    osc.connect(filt);
    filt.connect(env);
    env.connect(gameMusicFilter);
    osc.start(time);
    osc.stop(time + dur + 0.04);
  }

  function getGameMusicNoiseBuffer() {
    if (gameMusicNoiseBuffer || !gameMusicCtx) return gameMusicNoiseBuffer;
    var sr = gameMusicCtx.sampleRate;
    var len = Math.floor(sr * 0.08);
    var buffer = gameMusicCtx.createBuffer(1, len, sr);
    var data = buffer.getChannelData(0);
    for (var i = 0; i < len; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 1.8);
    }
    gameMusicNoiseBuffer = buffer;
    return buffer;
  }

  function scheduleMusicNoise(time, dur, gain) {
    var ctx = gameMusicCtx;
    var buffer = getGameMusicNoiseBuffer();
    if (!ctx || !buffer || !gameMusicFilter) return;
    var src = ctx.createBufferSource();
    var env = ctx.createGain();
    var hp = ctx.createBiquadFilter();
    src.buffer = buffer;
    hp.type = "highpass";
    hp.frequency.setValueAtTime(4200, time);
    env.gain.setValueAtTime(Math.max(0.0002, gain), time);
    env.gain.exponentialRampToValueAtTime(0.0001, time + dur);
    src.connect(hp);
    hp.connect(env);
    env.connect(gameMusicFilter);
    src.start(time);
    src.stop(time + dur + 0.02);
  }

  function playGameSfx(kind, intensity, x, y) {
    if (gameSfxVolume <= 0.001) return;
    var ctx = ensureGameMusicContext();
    resumeGameMusicContext();
    var t = ctx.currentTime;
    var force = clamp(intensity == null ? 1 : intensity, 0.15, 2.2);
    var pan = sfxPanFrom(x);
    if (kind === "hit") {
      sfxNoise(t, 0.035, 0.19 * force, 5200, pan, "highpass", 0.7, 0.55);
      sfxNoise(t + 0.012, 0.18, 0.18 * force, 850, pan, "bandpass", 1.8, 1.25);
      sfxTone(92, 38, 0.22, "sine", 0.15 * force, 0, 520, pan, 0.7);
      sfxTone(280, 116, 0.105, "square", 0.05 * force, 0.018, 1400, pan, 1.4);
    } else if (kind === "ship_hit") {
      sfxNoise(t, 0.38, 0.4 * force, 520, pan, "lowpass", 0.8, 1.15);
      sfxNoise(t + 0.025, 0.22, 0.22 * force, 920, pan, "bandpass", 1.6, 1.35);
      sfxTone(98, 38, 0.5, "sawtooth", 0.28 * force, 0, 520, pan, 0.75);
      sfxTone(62, 29, 0.72, "sine", 0.2 * force, 0.05, 260, pan, 0.5);
    } else if (kind === "shield") {
      sfxTone(220, 790, 0.32, "sine", 0.13 * force, 0, 1900, pan, 1.2);
      sfxTone(440, 1320, 0.25, "triangle", 0.08 * force, 0.035, 3200, -pan * 0.5, 1.1);
      sfxNoise(t + 0.02, 0.12, 0.055 * force, 3400, pan, "bandpass", 4.5, 1.1);
    } else if (kind === "shield_hit") {
      sfxTone(1180, 260, 0.18, "triangle", 0.14 * force, 0, 2600, pan, 1.6);
      sfxTone(590, 860, 0.12, "sine", 0.075 * force, 0.035, 3400, -pan * 0.4, 2.2);
      sfxNoise(t, 0.105, 0.12 * force, 2800, pan, "bandpass", 3.2, 1.0);
    } else if (kind === "shield_down") {
      sfxTone(520, 92, 0.55, "sawtooth", 0.18 * force, 0, 900, pan, 0.9);
      sfxNoise(t + 0.04, 0.24, 0.16 * force, 1250, pan, "bandpass", 1.7, 1.3);
    } else if (kind === "jump" || kind === "hyper") {
      sfxTone(74, 1180, 0.42, "sawtooth", 0.2 * force, 0, 2600, pan, 1.0);
      sfxTone(1480, 220, 0.32, "triangle", 0.08 * force, 0.08, 3400, -pan * 0.7, 1.1);
      sfxNoise(t + 0.03, 0.3, 0.16 * force, 1900, pan, "bandpass", 1.4, 1.1);
    } else if (kind === "force") {
      sfxTone(82, 210, 0.62, "sine", 0.18 * force, 0, 820, pan, 0.8);
      sfxTone(380, 720, 0.42, "triangle", 0.09 * force, 0.04, 1900, -pan * 0.45, 1.5);
      sfxNoise(t + 0.07, 0.22, 0.065 * force, 1600, pan, "bandpass", 3.6, 1.2);
    } else if (kind === "lightning") {
      sfxNoise(t, 0.035, 0.38 * force, 6200, pan, "highpass", 0.65, 0.45);
      sfxTone(2400, 720, 0.08, "square", 0.12 * force, 0, 5200, pan, 1.8);
      sfxNoiseSweep(t + 0.035, 0.78, 0.32 * force, 1250, 170, pan, "lowpass", 0.8, 1.55);
      sfxTone(82, 31, 0.82, "sine", 0.24 * force, 0.055, 360, pan, 0.5);
    } else if (kind === "voice") {
      sfxTone(155, 76, 0.56, "sine", 0.22 * force, 0, 640, pan, 0.7);
      sfxTone(310, 520, 0.44, "triangle", 0.12 * force, 0.015, 1700, -pan * 0.4, 1.1);
      sfxNoise(t + 0.05, 0.22, 0.055 * force, 980, pan, "bandpass", 4.2, 1.4);
    } else if (kind === "wave") {
      sfxNoiseSweep(t, 0.68, 0.24 * force, 2200, 180, 0, "lowpass", 0.7, 1.25);
      sfxTone(112, 48, 0.58, "sine", 0.16 * force, 0.02, 420, 0, 0.6);
      sfxNoise(t + 0.16, 0.3, 0.08 * force, 720, 0, "bandpass", 1.4, 1.5);
    } else if (kind === "game_over") {
      sfxTone(180, 82, 0.54, "sawtooth", 0.17 * force, 0, 840, pan, 0.8);
      sfxTone(135, 44, 0.78, "sine", 0.14 * force, 0.12, 520, -pan * 0.5, 0.7);
      sfxNoise(t + 0.04, 0.38, 0.13 * force, 620, pan, "bandpass", 1.4, 1.4);
    } else if (kind === "switch") {
      sfxTone(520, 780, 0.08, "triangle", 0.075 * force, 0, 1900, pan, 1.0);
    }
  }

  function sfxPanFrom(x) {
    if (!isFiniteNumber(x) || !width) return 0;
    return clamp((x / width) * 2 - 1, -0.72, 0.72);
  }

  function connectSfxNode(node, pan) {
    if (!gameSfxMaster) return;
    if (gameMusicCtx && typeof gameMusicCtx.createStereoPanner === "function" && Math.abs(pan || 0) > 0.001) {
      var panner = gameMusicCtx.createStereoPanner();
      panner.pan.value = clamp(pan || 0, -1, 1);
      node.connect(panner);
      panner.connect(gameSfxMaster);
      return;
    }
    node.connect(gameSfxMaster);
  }

  function sfxTone(startFreq, endFreq, dur, type, gain, delay, cutoff, pan, q) {
    var ctx = gameMusicCtx;
    if (!ctx || !gameSfxMaster) return;
    var t = ctx.currentTime + (delay || 0);
    var osc = ctx.createOscillator();
    var env = ctx.createGain();
    var filt = ctx.createBiquadFilter();
    osc.type = type || "sine";
    osc.frequency.setValueAtTime(Math.max(1, startFreq), t);
    osc.frequency.exponentialRampToValueAtTime(Math.max(1, endFreq), t + dur);
    filt.type = "lowpass";
    filt.frequency.setValueAtTime(cutoff || 1800, t);
    filt.Q.value = q == null ? 0.85 : q;
    env.gain.setValueAtTime(0.0001, t);
    env.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t + 0.008);
    env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    osc.connect(filt);
    filt.connect(env);
    connectSfxNode(env, pan || 0);
    osc.start(t);
    osc.stop(t + dur + 0.03);
  }

  function sfxNoise(time, dur, gain, cutoff, pan, filterType, q, decay) {
    var ctx = gameMusicCtx;
    if (!ctx || !gameSfxMaster) return;
    var len = Math.max(1, Math.floor(ctx.sampleRate * dur));
    var buffer = ctx.createBuffer(1, len, ctx.sampleRate);
    var data = buffer.getChannelData(0);
    for (var i = 0; i < len; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay == null ? 1.4 : decay);
    }
    var src = ctx.createBufferSource();
    var env = ctx.createGain();
    var filt = ctx.createBiquadFilter();
    src.buffer = buffer;
    filt.type = filterType || "bandpass";
    filt.frequency.setValueAtTime(cutoff || 1200, time);
    filt.Q.value = q == null ? 1.1 : q;
    env.gain.setValueAtTime(Math.max(0.0002, gain), time);
    env.gain.exponentialRampToValueAtTime(0.0001, time + dur);
    src.connect(filt);
    filt.connect(env);
    connectSfxNode(env, pan || 0);
    src.start(time);
    src.stop(time + dur + 0.02);
  }

  function sfxNoiseSweep(time, dur, gain, startCutoff, endCutoff, pan, filterType, q, decay) {
    var ctx = gameMusicCtx;
    if (!ctx || !gameSfxMaster) return;
    var len = Math.max(1, Math.floor(ctx.sampleRate * dur));
    var buffer = ctx.createBuffer(1, len, ctx.sampleRate);
    var data = buffer.getChannelData(0);
    for (var i = 0; i < len; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay == null ? 1.3 : decay);
    }
    var src = ctx.createBufferSource();
    var env = ctx.createGain();
    var filt = ctx.createBiquadFilter();
    src.buffer = buffer;
    filt.type = filterType || "lowpass";
    filt.frequency.setValueAtTime(Math.max(20, startCutoff || 1200), time);
    filt.frequency.exponentialRampToValueAtTime(Math.max(20, endCutoff || 200), time + dur);
    filt.Q.value = q == null ? 0.9 : q;
    env.gain.setValueAtTime(0.0001, time);
    env.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), time + 0.02);
    env.gain.exponentialRampToValueAtTime(0.0001, time + dur);
    src.connect(filt);
    filt.connect(env);
    connectSfxNode(env, pan || 0);
    src.start(time);
    src.stop(time + dur + 0.02);
  }

  function createGameAnalyser(mediaStreamTrack) {
    var ctx = ensureGameAudioContext();
    var stream = new MediaStream([mediaStreamTrack]);
    var source = ctx.createMediaStreamSource(stream);
    var analyser = ctx.createAnalyser();
    analyser.fftSize = 64;
    analyser.smoothingTimeConstant = 0.6;
    source.connect(analyser);
    return { analyser: analyser, source: source };
  }

  function isLocalGameParticipant(participant) {
    if (!participant || !gameRoom || !gameRoom.localParticipant) return false;
    if (participant.isLocal) return true;
    return participant.identity === gameRoom.localParticipant.identity;
  }

  function attachGameAudio(track) {
    if (!track || typeof track.attach !== "function") return;
    var attached;
    try { attached = track.attach(); } catch (_e) { return; }
    var list = Array.isArray(attached) ? attached : [attached];
    list.forEach(function (el) {
      if (!el || el.nodeType !== 1) return;
      if (String(el.tagName || "").toLowerCase() !== "audio") return;
      el.autoplay = true;
      el.playsInline = true;
      el.style.display = "none";
      if (!el.parentNode) document.body.appendChild(el);
      gameRemoteAudioEls.push(el);
    });
    // Wire the agent's audio into the game HUD waveform.
    if (track.mediaStreamTrack && gameAgentAnalyserTrack !== track.mediaStreamTrack) {
      var pair = createGameAnalyser(track.mediaStreamTrack);
      gameAgentAnalyser = pair.analyser;
      gameAgentAnalyserTrack = track.mediaStreamTrack;
      startGameWaveformLoop();
    }
  }

  function attachGameLocalMic(publication) {
    var track = publication && publication.track;
    if (!track || !track.mediaStreamTrack) return;
    if (gameUserAnalyserTrack === track.mediaStreamTrack) return;
    var pair = createGameAnalyser(track.mediaStreamTrack);
    gameUserAnalyser = pair.analyser;
    gameUserAnalyserTrack = track.mediaStreamTrack;
    startGameWaveformLoop();
  }

  function cleanupGameAudio() {
    gameRemoteAudioEls.forEach(function (el) {
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
    gameRemoteAudioEls = [];
    gameAgentAnalyser = null;
    gameAgentAnalyserTrack = null;
    gameUserAnalyser = null;
    gameUserAnalyserTrack = null;
    stopGameWaveformLoop();
    if (gameAudioCtx) {
      gameAudioCtx.close().catch(function () {});
      gameAudioCtx = null;
    }
  }

  function appendGameHudTrace(tag, text) {
    if (!gameHudTraceEl) return;
    var line = document.createElement("div");
    line.className = "hud-trace-line";
    var span = document.createElement("span");
    span.className = "hud-trace-tag hud-trace-tag--" + tag;
    span.textContent = "[" + tag + "]";
    line.appendChild(span);
    line.appendChild(document.createTextNode(" " + text));
    gameHudTraceEl.appendChild(line);
    while (gameHudTraceEl.children.length > 30) gameHudTraceEl.removeChild(gameHudTraceEl.firstChild);
    gameHudTraceEl.scrollTop = gameHudTraceEl.scrollHeight;
  }

  function prepareGameHudLine(line) {
    if (!line) return;
    line.style.textAlign = "right";
    line.style.textOverflow = "clip";
  }

  function updateGameHudText(speaker, text, isStreaming) {
    var el = speaker === "user" ? gameHudUserTxt : gameHudAgentTxt;
    if (!el || !text) return;
    var partial = el.querySelector(".hud-stt-partial");
    if (isStreaming) {
      if (!partial) {
        partial = document.createElement("div");
        partial.className = "hud-stt-partial";
        prepareGameHudLine(partial);
        el.appendChild(partial);
      }
      partial.textContent = text;
      partial.title = text;
    } else {
      if (partial) {
        partial.className = "";
        partial.textContent = text;
        partial.title = text;
        prepareGameHudLine(partial);
      } else {
        var last = el.lastElementChild;
        if (last && last.textContent === text) {
          last.title = text;
          prepareGameHudLine(last);
          el.scrollTop = el.scrollHeight;
          return;
        }
        var d = document.createElement("div");
        d.textContent = text;
        d.title = text;
        prepareGameHudLine(d);
        el.appendChild(d);
      }
      while (el.children.length > 4) el.removeChild(el.firstChild);
    }
    el.scrollTop = el.scrollHeight;
  }

  function emitGameVoicePulse(speaker, streaming) {
    if (speaker === "user") {
      gameUserWavePulse = Math.max(gameUserWavePulse, 0.04);
    } else if (speaker === "agent") {
      gameAgentWavePulse = Math.max(gameAgentWavePulse, streaming ? 0.08 : 0.04);
    }
  }

  function setGameVoiceState(state) {
    appendGameHudTrace("agt", state);
    if (gameHudSysDot) gameHudSysDot.dataset.state = state;
    if (gameHudSysState) {
      var labels = {
        disconnected: "OFFLINE",
        connecting: "CONNECTING",
        connected: "CONNECTED",
        requesting: "CONNECTING",
        listening: "CONNECTED",
      };
      gameHudSysState.textContent = labels[state] || String(state || "").toUpperCase();
    }
    if (gameHudSysMode) {
      var modes = {
        disconnected: "IDLE",
        connecting: "THINKING",
        connected: "READY",
        requesting: "CONNECTING",
        listening: "LISTENING",
        error: "ERROR",
      };
      gameHudSysMode.textContent = modes[state] || "IDLE";
    }

    var active = state === "listening";
    if (gameHudMicBtn) {
      gameHudMicBtn.disabled = state === "connecting" || state === "requesting";
      gameHudMicBtn.setAttribute("data-active", active ? "1" : "0");
      gameHudMicBtn.textContent = active ? "LIVE" : "MIC";
    }

    if ((state === "connected" || state === "listening") && !gameSessionTimerInterval) {
      gameSessionStart = Date.now();
      gameSessionTimerInterval = window.setInterval(tickGameSessionTimer, 1000);
      tickGameSessionTimer();
    } else if (state === "disconnected" || state === "error") {
      if (gameSessionTimerInterval) {
        window.clearInterval(gameSessionTimerInterval);
        gameSessionTimerInterval = null;
      }
      if (gameHudSysTimer) gameHudSysTimer.textContent = "00:00";
    }

    if (state === "connected" || state === "listening") {
      resizeGameHudCanvases();
      startGameWaveformLoop();
    } else if (state === "disconnected" || state === "error") {
      stopGameWaveformLoop();
    }
  }

  function tickGameSessionTimer() {
    if (!gameHudSysTimer) return;
    var s = Math.floor((Date.now() - gameSessionStart) / 1000);
    gameHudSysTimer.textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  function resizeGameHudCanvases() {
    [gameHudAgentWave, gameHudUserWave, gameHudRttEl].forEach(function (canvasEl) {
      if (!canvasEl) return;
      var rect = canvasEl.getBoundingClientRect();
      if (rect.width === 0) return;
      var dpr = window.devicePixelRatio || 1;
      canvasEl.width = rect.width * dpr;
      canvasEl.height = rect.height * dpr;
    });
  }

  // ── RTT gauge ─────────────────────────────────────────────────────────
  // Self-contained copy of the dashboard's RTT phase machine (shell.js).
  // Prefers agent-side provider network samples when the worker publishes them;
  // falls back to user voice → agent reply timings while the room is warming.
  var RTT_MAX = 10000;
  var RTT_PEAK_DECAY = 0.97;
  var RTT_PHASE_TIMEOUT = 15000;

  var gameRtt = {
    phase: "idle", phaseTime: 0, speechEnd: 0, inputTime: 0, llmFirst: 0,
    stt: 0, llm: 0, tts: 0,
    pStt: 0, pLlm: 0, pTts: 0,
    speaking: false, silent: 0,
    network: false, lastProviderAt: 0,
    samples: { stt: null, llm: null, tts: null },
  };

  function gameRttSetPhase(p) {
    gameRtt.phase = p;
    gameRtt.phaseTime = performance.now();
  }

  function gameRttClearTurn() {
    gameRtt.stt = 0; gameRtt.llm = 0; gameRtt.tts = 0;
  }

  function gameRttUserVoice() {
    var now = performance.now();
    gameRtt.stt = gameRtt.speechEnd > 0 ? now - gameRtt.speechEnd : 0;
    gameRtt.llm = 0; gameRtt.tts = 0;
    gameRtt.inputTime = now;
    gameRttSetPhase("waiting_llm");
    gameRtt.speechEnd = 0;
  }

  function gameRttAgentToken(modality) {
    if (gameRtt.phase !== "waiting_llm") return;
    var now = performance.now();
    gameRtt.llmFirst = now;
    gameRtt.llm = now - gameRtt.inputTime;
    if (modality === "voice") { gameRttSetPhase("waiting_tts"); }
    else { gameRtt.tts = 0; gameRttSetPhase("idle"); }
  }

  function gameRttReset() {
    gameRttSetPhase("idle");
    gameRtt.speechEnd = 0; gameRtt.inputTime = 0; gameRtt.llmFirst = 0;
    gameRtt.stt = 0; gameRtt.llm = 0; gameRtt.tts = 0;
    gameRtt.pStt = 0; gameRtt.pLlm = 0; gameRtt.pTts = 0;
    gameRtt.speaking = false; gameRtt.silent = 0;
    gameRtt.network = false; gameRtt.lastProviderAt = 0;
    gameRtt.samples = { stt: null, llm: null, tts: null };
  }

  function gameRttProviderLatency(payload) {
    if (!payload || !payload.samples) return;
    gameRtt.network = true;
    gameRtt.lastProviderAt = performance.now();
    gameRtt.samples = {
      stt: payload.samples.stt || null,
      llm: payload.samples.llm || null,
      tts: payload.samples.tts || null,
    };
    gameRtt.stt = gameSampleLatency(gameRtt.samples.stt);
    gameRtt.llm = gameSampleLatency(gameRtt.samples.llm);
    gameRtt.tts = gameSampleLatency(gameRtt.samples.tts);
  }

  function gameSampleLatency(sample) {
    var value = sample && typeof sample.latencyMs === "number" ? sample.latencyMs : 0;
    return value > 0 ? value : 0;
  }

  function gameSampleText(sample) {
    if (!sample) return "--";
    if (sample.status === "ok" && typeof sample.latencyMs === "number") return Math.round(sample.latencyMs) + "ms";
    if (sample.status === "local") return "LOCAL";
    if (sample.status === "timeout") return "TIME";
    if (sample.status === "error") return "ERR";
    if (sample.status === "unconfigured") return "OFF";
    return "--";
  }

  function gameRttTick() {
    var now = performance.now();
    if (gameRtt.network && now - gameRtt.lastProviderAt > 20000) {
      gameRtt.network = false;
      gameRtt.samples = { stt: null, llm: null, tts: null };
      gameRttClearTurn();
    }
    if (gameRtt.phase !== "idle" && now - gameRtt.phaseTime > RTT_PHASE_TIMEOUT) {
      gameRttSetPhase("idle");
    }

    var userE = gameRmsEnergy(gameUserAnalyser);
    if (userE > 0.05) {
      gameRtt.silent = 0;
      if (!gameRtt.speaking) gameRttClearTurn();
      gameRtt.speaking = true;
    } else if (gameRtt.speaking) {
      gameRtt.silent++;
      if (gameRtt.silent > 10) {
        gameRtt.speaking = false;
        gameRtt.speechEnd = now;
        if (gameRtt.phase === "idle") gameRttSetPhase("waiting_stt");
      }
    }

    if (gameRtt.phase === "waiting_tts") {
      if (gameRmsEnergy(gameAgentAnalyser) > 0.03) {
        gameRtt.tts = now - gameRtt.llmFirst;
        gameRttSetPhase("idle");
      }
    }
  }

  function drawGameRtt(canvasEl) {
    if (!canvasEl) return;
    var c = canvasEl.getContext("2d");
    if (!c) return;
    var w = canvasEl.width, h = canvasEl.height;
    if (w === 0 || h === 0) return;
    var dpr = window.devicePixelRatio || 1;

    gameRtt.pStt = Math.max(gameRtt.pStt * RTT_PEAK_DECAY, gameRtt.stt);
    gameRtt.pLlm = Math.max(gameRtt.pLlm * RTT_PEAK_DECAY, gameRtt.llm);
    gameRtt.pTts = Math.max(gameRtt.pTts * RTT_PEAK_DECAY, gameRtt.tts);

    c.fillStyle = "hsl(165, 12%, 5%)";
    c.fillRect(0, 0, w, h);

    var pad = Math.round(2 * dpr);
    var rowGap = Math.round(3 * dpr);
    var isActive = gameConnected;

    var fontSize = Math.round(8 * dpr);
    c.font = fontSize + "px 'Share Tech Mono', monospace";
    var valueW = Math.ceil(c.measureText("0000ms").width) + Math.round(3 * dpr);
    var labelW = Math.ceil(c.measureText("LLM").width) + Math.round(4 * dpr);

    var barX = pad + labelW;
    var barAreaW = Math.max(1, w - barX - pad - valueW);
    var totalH = h - pad * 2 - rowGap * 2;
    var rowH = Math.floor(totalH / 3);
    var segW = Math.max(1, Math.round(2 * dpr));
    var segGap = Math.max(1, Math.round(1 * dpr));
    var segCount = Math.floor((barAreaW + segGap) / (segW + segGap));

    var vals = [gameRtt.stt, gameRtt.llm, gameRtt.tts];
    var peaks = [gameRtt.pStt, gameRtt.pLlm, gameRtt.pTts];
    var rowLabels = ["STT", "LLM", "TTS"];
    var samples = [gameRtt.samples.stt, gameRtt.samples.llm, gameRtt.samples.tts];
    var logMax = Math.log10(1 + RTT_MAX / 100);

    for (var i = 0; i < 3; i++) {
      var y = pad + i * (rowH + rowGap);
      var ratio = Math.min(Math.log10(1 + vals[i] / 100) / logMax, 1);
      var pRatio = Math.min(Math.log10(1 + peaks[i] / 100) / logMax, 1);
      var litCount = Math.round(ratio * segCount);
      var peakSeg = Math.round(pRatio * segCount) - 1;
      if (isActive && !gameRtt.network) litCount = Math.max(1, litCount);

      c.fillStyle = litCount > 0 ? "hsl(165, 55%, 65%)" : "hsl(165, 20%, 24%)";
      c.font = fontSize + "px 'Share Tech Mono', monospace";
      c.textAlign = "left";
      c.textBaseline = "middle";
      c.fillText(rowLabels[i], pad, y + rowH / 2);

      for (var s = 0; s < segCount; s++) {
        var sx = barX + s * (segW + segGap);
        if (s < litCount) {
          c.fillStyle = "hsl(165, 55%, 65%)";
        } else if (s === peakSeg && peakSeg >= litCount) {
          c.fillStyle = "hsla(165, 55%, 65%, 0.45)";
        } else {
          c.fillStyle = "hsl(165, 15%, 8%)";
        }
        c.fillRect(sx, y, segW, rowH);
      }

      c.fillStyle = samples[i] && samples[i].status === "ok" ? "hsl(165, 55%, 65%)" : "hsl(165, 20%, 42%)";
      c.textAlign = "right";
      c.textBaseline = "middle";
      c.fillText(gameRtt.network ? gameSampleText(samples[i]) : "--", w - pad, y + rowH / 2);
    }
  }

  var gameWaveState = new WeakMap();
  var GAME_WAVE_LAYERS = [
    { freq: 2.0, amp: 0.38, speed: 0.8,  alpha: 0.9,  width: 1.6 },
    { freq: 3.2, amp: 0.30, speed: 1.2,  alpha: 0.55, width: 1.2 },
    { freq: 4.8, amp: 0.22, speed: 1.7,  alpha: 0.35, width: 1.0 },
    { freq: 1.3, amp: 0.18, speed: 0.5,  alpha: 0.25, width: 0.8 },
  ];

  function gameRmsEnergy(analyser) {
    if (!analyser || typeof analyser.getByteTimeDomainData !== "function") return 0;
    var n = Math.floor(Number(analyser.frequencyBinCount || 0));
    if (!isFiniteNumber(n) || n <= 0) return 0;
    var buf = new Uint8Array(n);
    try {
      analyser.getByteTimeDomainData(buf);
    } catch (_e) {
      return 0;
    }
    var sum = 0;
    for (var i = 0; i < n; i++) {
      var v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    var rms = Math.sqrt(sum / n);
    return isFiniteNumber(rms) ? rms : 0;
  }

  function drawGameWaveform(canvasEl, analyser) {
    if (!canvasEl) return;
    var c = canvasEl.getContext("2d");
    if (!c) return;
    var w = canvasEl.width;
    var h = canvasEl.height;
    if (w === 0 || h === 0) return;
    var mid = h / 2;
    var dpr = window.devicePixelRatio || 1;
    var rms = gameRmsEnergy(analyser);
    if (canvasEl === gameHudAgentWave) {
      if (!isFiniteNumber(gameAgentWavePulse)) gameAgentWavePulse = 0;
      rms = Math.max(rms, gameAgentWavePulse);
      gameAgentWavePulse *= 0.9;
    } else if (canvasEl === gameHudUserWave) {
      if (!isFiniteNumber(gameUserWavePulse)) gameUserWavePulse = 0;
      rms = Math.max(rms, gameUserWavePulse);
      gameUserWavePulse *= 0.9;
    }
    if (!isFiniteNumber(rms)) rms = 0;

    var st = gameWaveState.get(canvasEl);
    if (!st) {
      st = { energy: 0, phase: 0 };
      gameWaveState.set(canvasEl, st);
    }
    if (!isFiniteNumber(st.energy)) st.energy = 0;
    if (!isFiniteNumber(st.phase)) st.phase = 0;
    st.energy += (rms - st.energy) * 0.12;
    st.phase += 0.015;

    c.fillStyle = "hsla(165, 12%, 5%, 0.28)";
    c.fillRect(0, 0, w, h);

    for (var li = 0; li < GAME_WAVE_LAYERS.length; li++) {
      var L = GAME_WAVE_LAYERS[li];
      var amplitude = L.amp * st.energy * mid * 8;
      var phase = st.phase * L.speed + li * 1.8;
      c.save();
      c.shadowColor = "hsla(165, 60%, 55%, " + (0.4 * L.alpha) + ")";
      c.shadowBlur = 6 * dpr;
      c.beginPath();
      for (var i = 0; i <= 200; i++) {
        var t = i / 200;
        var x = t * w;
        var y = mid + Math.sin(t * Math.PI * 2 * L.freq + phase) * amplitude;
        if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
      }
      c.lineWidth = (L.width + 1.0) * dpr;
      c.strokeStyle = "hsla(165, 50%, 55%, " + (L.alpha * 0.3) + ")";
      c.stroke();
      c.restore();

      c.beginPath();
      for (var j = 0; j <= 200; j++) {
        var tj = j / 200;
        var xj = tj * w;
        var yj = mid + Math.sin(tj * Math.PI * 2 * L.freq + phase) * amplitude;
        if (j === 0) c.moveTo(xj, yj); else c.lineTo(xj, yj);
      }
      c.lineWidth = L.width * dpr;
      c.strokeStyle = "hsla(165, 55%, 65%, " + L.alpha + ")";
      c.stroke();
    }
  }

  function gameWaveformTick() {
    drawGameWaveform(gameHudAgentWave, gameAgentAnalyser);
    drawGameWaveform(gameHudUserWave, gameUserAnalyser);
    gameRttTick();
    drawGameRtt(gameHudRttEl);
    gameWaveformRaf = requestAnimationFrame(gameWaveformTick);
  }

  function startGameWaveformLoop() {
    if (gameWaveformRaf) return;
    resizeGameHudCanvases();
    gameWaveformRaf = requestAnimationFrame(gameWaveformTick);
  }

  function stopGameWaveformLoop() {
    if (!gameWaveformRaf) return;
    cancelAnimationFrame(gameWaveformRaf);
    gameWaveformRaf = null;
  }

  function resetGameChrome() {
    [gameHudAgentTxt, gameHudUserTxt, gameHudTraceEl].forEach(function (el) {
      if (el) el.innerHTML = "";
    });
    gameAgentWavePulse = 0;
    gameUserWavePulse = 0;
    gameRttReset();
    setGameVoiceState("disconnected");
    resizeGameHudCanvases();
  }

  function handleGameAgentEvent(text) {
    var payload;
    try { payload = JSON.parse(text); } catch (_e) { return; }
    var type = payload.type;
    if (type === "provider_latency") {
      gameRttProviderLatency(payload);
      return;
    }
    if (type === "user_input_transcribed" && payload.is_final) {
      var ut = (payload.transcript || "").trim();
      if (!ut) return;
      updateGameHudText("user", ut, false);
      emitGameVoicePulse("user", false);
      gameRttUserVoice();
      return;
    }
    if (type === "agent_chat_response") {
      var ct = (payload.text || "").trim();
      if (!ct) return;
      showNarrator(ct, false);
      updateGameHudText("agent", ct, false);
      gameRttAgentToken("text");
      return;
    }
    if (type === "agent_voice_transcribed") {
      var vt = (payload.transcript || payload.text || "").trim();
      if (!vt) return;
      var streaming = payload.is_final === false;
      showNarrator(vt, streaming);
      updateGameHudText("agent", vt, streaming);
      emitGameVoicePulse("agent", streaming);
      gameRttAgentToken("voice");
    }
  }

  async function handleGameTranscriptionStream(reader, participant) {
    if (!reader) return;
    if (isLocalGameParticipant(participant)) return;
    var text = "";
    var sawFirstChunk = false;
    for await (var chunk of reader) {
      text = mergeGameTextStreamChunk(text, chunk);
      if (!sawFirstChunk) {
        gameRttAgentToken("voice");
        sawFirstChunk = true;
      }
      showNarrator(text, true);
      updateGameHudText("agent", text, true);
      emitGameVoicePulse("agent", true);
    }
    showNarrator(text, false);
    updateGameHudText("agent", text, false);
    emitGameVoicePulse("agent", false);
  }

  function normalizeGameStreamText(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function mergeGameTextStreamChunk(current, chunk) {
    var existing = String(current || "");
    var next = String(chunk || "");
    var existingNorm = normalizeGameStreamText(existing);
    var nextNorm = normalizeGameStreamText(next);
    if (!nextNorm) return existing;
    if (!existingNorm) return next;
    if (next.indexOf(existing) === 0 || nextNorm.indexOf(existingNorm) === 0) return next.trimStart();
    if (existing.indexOf(next) === 0 || existingNorm.indexOf(nextNorm) === 0) return existing;
    if (existingNorm === nextNorm) return existing;

    var maxOverlap = Math.min(existingNorm.length, nextNorm.length);
    for (var size = maxOverlap; size >= 8; size--) {
      if (existingNorm.slice(-size) === nextNorm.slice(0, size)) {
        return existing + nextNorm.slice(size);
      }
    }
    return existing + next;
  }

  async function gameConnect() {
    if (gameConnected && gameRoom) {
      setGameVoiceState(gameMicActive ? "listening" : "connected");
      return true;
    }
    setGameVoiceState("connecting");
    try {
      var resp = await fetch("/dashboard/api/game/token", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!resp.ok) {
        setGameVoiceState("error");
        appendGameHudTrace("err", "copilot channel failed to open");
        return false;
      }
      var data = await resp.json();
      gameRoom = new LivekitClient.Room();
      gameRoom.registerTextStreamHandler("lk.transcription", handleGameTranscriptionStream);
      gameRoom.on(LivekitClient.RoomEvent.DataReceived, function (bytes, _participant, _kind, topic) {
        var text;
        try { text = new TextDecoder().decode(bytes); } catch (_e) { return; }
        if (topic === "lk.agent.events") {
          handleGameAgentEvent(text);
          return;
        }
        if (topic === "mh.game") {
          handleServerGameEvent(text);
          return;
        }
      });
      gameRoom.on(LivekitClient.RoomEvent.TrackSubscribed, function (track, _publication, participant) {
        if (isLocalGameParticipant(participant)) return;
        if (track && (track.kind === "audio" ||
            (LivekitClient.Track && LivekitClient.Track.Kind && track.kind === LivekitClient.Track.Kind.Audio))) {
          attachGameAudio(track);
        }
      });
      gameRoom.on(LivekitClient.RoomEvent.TrackUnsubscribed, function (track) {
        if (track && typeof track.detach === "function") {
          try { track.detach().forEach(function (el) {
            if (el && el.parentNode) el.parentNode.removeChild(el);
          }); } catch (_e) {}
        }
      });
      gameRoom.on(LivekitClient.RoomEvent.LocalTrackPublished, function (publication) {
        attachGameLocalMic(publication);
      });
      gameRoom.on(LivekitClient.RoomEvent.Disconnected, function () {
        gameConnected = false;
        gameMicActive = false;
        cleanupGameAudio();
        setGameVoiceState("disconnected");
      });
      await gameRoom.connect(data.url, data.token);
      gameConnected = true;
      setGameVoiceState("connected");
      startGameTickInterval();
      return true;
    } catch (e) {
      gameRoom = null;
      gameConnected = false;
      gameMicActive = false;
      cleanupGameAudio();
      setGameVoiceState("error");
      appendGameHudTrace("err", e && e.message ? e.message : "copilot channel failed");
      return false;
    }
  }

  async function gameStartMic() {
    resumeGameMusicContext();
    if (!gameRoom || !gameConnected) {
      var ok = await gameConnect();
      if (!ok) return;
    }
    setGameVoiceState("requesting");
    try {
      await gameRoom.localParticipant.setMicrophoneEnabled(true);
      if (typeof gameRoom.startAudio === "function") {
        try { await gameRoom.startAudio(); } catch (_e) {}
      }
      await gameRoom.localParticipant.publishData(
        new TextEncoder().encode(JSON.stringify({ action: "start" })),
        { reliable: true, topic: "mh.voice_control" }
      );
      gameMicActive = true;
      setGameVoiceState("listening");
    } catch (e) {
      gameMicActive = false;
      setGameVoiceState(gameConnected ? "connected" : "disconnected");
      appendGameHudTrace("err", e && e.message ? e.message : "mic failed");
    }
  }

  async function gameStopMic() {
    if (!gameRoom || !gameConnected) return;
    try {
      await gameRoom.localParticipant.setMicrophoneEnabled(false);
      await gameRoom.localParticipant.publishData(
        new TextEncoder().encode(JSON.stringify({ action: "stop" })),
        { reliable: true, topic: "mh.voice_control" }
      );
    } catch (_e) {}
    gameMicActive = false;
    setGameVoiceState("connected");
  }

  async function publishGame(type, payload) {
    if (!gameRoom || !gameConnected) return;
    try {
      await gameRoom.localParticipant.publishData(
        new TextEncoder().encode(JSON.stringify({ type: type, payload: payload || {} })),
        { reliable: true, topic: "mh.game" }
      );
    } catch (_e) {}
  }

  // Periodic game-state snapshot → server ambient state. The Copilot's
  // system prompt is refreshed from these ticks, so when he speaks he has
  // fresh wave/score/lives/shield context even between alarm beats.
  var gameTickInterval = null;
  function startGameTickInterval() {
    if (gameTickInterval) return;
    gameTickInterval = window.setInterval(function () {
      if (mode !== "playing" && mode !== "intermission") return;
      var now = performance.now();
      publishGame("game_tick", {
        wave: wave,
        score: score,
        lives: lives,
        asteroids: asteroids.length,
        shield_particles: shieldParticlesLeft,
        force_field_active: isForceFieldActive(now),
        lightning_cooldown_ms: Math.max(0, Math.ceil(lightningReadyAt - now)),
        voice_weapon_cooldown_ms: Math.max(0, Math.ceil(voiceWeaponReadyAt - now)),
        shot_pattern: shotPattern,
      });
    }, 2000);
  }
  function stopGameTickInterval() {
    if (gameTickInterval) {
      window.clearInterval(gameTickInterval);
      gameTickInterval = null;
    }
  }

  // Snapshot of the top 3 leaderboard rows as {name, score} pairs. Used to
  // seed the Harbormaster's greeting so Hades can name the ghosts on the board.
  function leaderboardTop3() {
    return leaderboard.slice(0, 3).map(function (row) {
      return { name: String(row.name || ""), score: Number(row.score || 0) };
    });
  }

  // ── Server-origin game packets (Copilot tool calls) ──────────────────
  // The Copilot has five tools; four of them publish server→client packets
  // handled here. (`read_ship_status` stays server-side.) Client is
  // authoritative on cooldowns and mechanics — if the ship can't honour a
  // request, we publish a `*_refused` event so the Copilot narrates the
  // refusal in-character.
  function handleServerGameEvent(text) {
    var parsed;
    try { parsed = JSON.parse(text); } catch (_e) { return; }
    if (!parsed || typeof parsed !== "object") return;
    var type = String(parsed.type || "").toLowerCase();
    var payload = (parsed.payload && typeof parsed.payload === "object") ? parsed.payload : {};
    if (type === "agent_enable_shield") onAgentEnableShield();
    else if (type === "agent_shift_jump") onAgentShiftJump();
    else if (type === "agent_force_field") onAgentForceField();
    else if (type === "agent_lightning_weapon") onAgentLightningWeapon();
    else if (type === "agent_voice_weapon") onAgentVoiceWeapon();
    else if (type === "agent_shot_pattern") onAgentShotPattern(payload);
  }

  // ── Shield ────────────────────────────────────────────────────────────
  function onAgentEnableShield() {
    if (shieldCooldownWaves > 0 || shieldParticlesLeft > 0) {
      publishGame("shield_refused", { waves: shieldCooldownWaves });
      return;
    }
    armShield();
    playGameSfx("shield", 1, ship ? ship.x : null, ship ? ship.y : null);
    publishGame("shield_online", { particles: SHIELD_MAX_PARTICLES });
  }

  function armShield() {
    shieldParticlesLeft = SHIELD_MAX_PARTICLES;
    spawnShieldOrbitals();
    shieldCooldownWaves = SHIELD_COOLDOWN_WAVES;
  }

  // Ported from graph.js drawPersonOrbital: 5 tilted elliptical shells × 2
  // motes each. Each orbital is pre-assigned a (shell, paired) identity so
  // absorbs can collapse whole shells inward instead of popping random dots.
  function spawnShieldOrbitals() {
    shieldOrbitals = [];
    var NUM_SHELLS = 5;
    var PER_SHELL = 2;
    for (var k = 0; k < PER_SHELL; k++) {
      for (var s = 0; s < NUM_SHELLS; s++) {
        shieldOrbitals.push({
          shell: s,
          paired: k,                       // 0 or 1 — diametric pair index
          phase: k * Math.PI + s * 0.7,    // stagger so motes don't line up
        });
      }
    }
  }

  // ── Shift-jump ────────────────────────────────────────────────────────
  function onAgentShiftJump() {
    var now = performance.now();
    if (!ship) {
      publishGame("shift_jump_refused", { reason: "no_ship" });
      return;
    }
    if (now < shiftJumpReadyAt) {
      publishGame("shift_jump_refused", { reason: "cooldown" });
      return;
    }
    // Teleport to a random point, clear nearby rocks, grant a long invuln.
    var oldX = ship.x, oldY = ship.y;
    ship.x = Math.random() * width;
    ship.y = Math.random() * height;
    ship.vx = 0; ship.vy = 0;
    invulnUntilMs = now + SHIFT_JUMP_INVULN_MS;
    shiftJumpReadyAt = now + SHIFT_JUMP_COOLDOWN_MS;
    spawnExplosion(oldX, oldY, 0.8, "hsl(165,90%,82%)");
    spawnExplosion(ship.x, ship.y, 1.1, "hsl(165,90%,82%)");
    playGameSfx("jump", 1.15, oldX, oldY);

    // Blast-radius rock clear at the ARRIVAL point.
    var r2 = SHIFT_JUMP_BLAST_RADIUS * SHIFT_JUMP_BLAST_RADIUS;
    for (var i = asteroids.length - 1; i >= 0; i--) {
      var a = asteroids[i];
      if (dist2(a.x, a.y, ship.x, ship.y) <= r2) {
        spawnExplosion(a.x, a.y, a.size === "L" ? 1.1 : a.size === "M" ? 0.8 : 0.5, "hsl(165,90%,82%)");
        asteroids.splice(i, 1);
      }
    }
    if (asteroids.length === 0 && mode === "playing") onWaveCleared();
  }

  // ── Force field ──────────────────────────────────────────────────────
  function onAgentForceField() {
    var now = performance.now();
    if (!ship) {
      publishGame("force_field_refused", { reason: "no_ship" });
      return;
    }
    if (forceFieldCooldownWaves > 0 || now < forceFieldUntil) {
      publishGame("force_field_refused", { waves: forceFieldCooldownWaves });
      return;
    }
    forceFieldUntil = now + FORCE_FIELD_DURATION_MS;
    forceFieldCooldownWaves = FORCE_FIELD_COOLDOWN_WAVES;
    playGameSfx("force", 1, ship.x, ship.y);
  }

  function isForceFieldActive(now) {
    return now < forceFieldUntil;
  }

  function weaponCooldownMs(readyAt, now) {
    return Math.max(0, Math.ceil(readyAt - now));
  }

  // ── Lightning weapon ─────────────────────────────────────────────────
  function onAgentLightningWeapon() {
    var now = performance.now();
    if (!ship) {
      publishGame("lightning_weapon_refused", { reason: "no_ship" });
      return;
    }
    if (wave < LIGHTNING_UNLOCK_WAVE) {
      publishGame("lightning_weapon_refused", {
        reason: "locked",
        wave: wave,
        unlock_wave: LIGHTNING_UNLOCK_WAVE,
      });
      return;
    }
    if (now < lightningReadyAt) {
      publishGame("lightning_weapon_refused", {
        reason: "cooldown",
        cooldown_ms: weaponCooldownMs(lightningReadyAt, now),
      });
      return;
    }
    if (!fireLightningWeapon(now)) {
      publishGame("lightning_weapon_refused", { reason: "no_target" });
      return;
    }
    lightningReadyAt = now + LIGHTNING_COOLDOWN_MS;
    publishGame("lightning_weapon_fired", {
      cooldown_ms: LIGHTNING_COOLDOWN_MS,
    });
  }

  function lightningTargets() {
    if (!ship) return [];
    var range2 = LIGHTNING_RANGE * LIGHTNING_RANGE;
    var candidates = [];
    for (var i = 0; i < asteroids.length; i++) {
      var a = asteroids[i];
      var d2 = wrappedDist2(ship.x, ship.y, a.x, a.y);
      if (d2 <= range2) candidates.push({ index: i, d2: d2, asteroid: a });
    }
    candidates.sort(function (a, b) { return a.d2 - b.d2; });
    return candidates.slice(0, LIGHTNING_MAX_TARGETS);
  }

  function fireLightningWeapon(now) {
    var targets = lightningTargets();
    if (targets.length === 0) return false;
    var points = [{ x: ship.x, y: ship.y }];
    for (var i = 0; i < targets.length; i++) {
      points.push({ x: targets[i].asteroid.x, y: targets[i].asteroid.y });
    }
    playGameSfx("lightning", 0.8 + Math.min(0.8, targets.length * 0.08), ship.x, ship.y);
    spawnLightningEffect(points, now);

    targets.sort(function (a, b) { return b.index - a.index; });
    for (var j = 0; j < targets.length; j++) {
      var target = targets[j];
      destroyAsteroid(target.index, target.asteroid.x, target.asteroid.y, "hsl(195,95%,78%)", true);
    }
    if (asteroids.length === 0 && mode === "playing") onWaveCleared();
    return true;
  }

  // ── Voice weapon ─────────────────────────────────────────────────────
  function onAgentVoiceWeapon() {
    var now = performance.now();
    if (!ship) {
      publishGame("voice_weapon_refused", { reason: "no_ship" });
      return;
    }
    if (wave < VOICE_WEAPON_UNLOCK_WAVE) {
      publishGame("voice_weapon_refused", {
        reason: "locked",
        wave: wave,
        unlock_wave: VOICE_WEAPON_UNLOCK_WAVE,
      });
      return;
    }
    if (now < voiceWeaponReadyAt) {
      publishGame("voice_weapon_refused", {
        reason: "cooldown",
        cooldown_ms: weaponCooldownMs(voiceWeaponReadyAt, now),
      });
      return;
    }
    var hitCount = fireVoiceWeapon(now);
    voiceWeaponReadyAt = now + VOICE_WEAPON_COOLDOWN_MS;
    publishGame("voice_weapon_fired", {
      hits: hitCount,
      cooldown_ms: VOICE_WEAPON_COOLDOWN_MS,
    });
  }

  function fireVoiceWeapon(now) {
    var pulseEnergy = isFiniteNumber(gameUserWavePulse) ? gameUserWavePulse : 0;
    var energy = Math.max(gameRmsEnergy(gameUserAnalyser), pulseEnergy);
    var radius = VOICE_WEAPON_BASE_RADIUS + clamp(energy * 8, 0, 1) * VOICE_WEAPON_ENERGY_RADIUS;
    playGameSfx("voice", 0.85 + clamp(energy * 6, 0, 0.8), ship.x, ship.y);
    spawnVoiceWaveEffect(now, radius);

    var hitIndices = [];
    for (var i = 0; i < asteroids.length; i++) {
      var a = asteroids[i];
      var r = radius + a.radius;
      if (wrappedDist2(ship.x, ship.y, a.x, a.y) <= r * r) {
        hitIndices.push({ index: i, asteroid: a });
      }
    }
    hitIndices.sort(function (a, b) { return b.index - a.index; });
    for (var j = 0; j < hitIndices.length; j++) {
      var hit = hitIndices[j];
      destroyAsteroid(hit.index, hit.asteroid.x, hit.asteroid.y, "hsl(50,90%,78%)", true);
    }
    if (asteroids.length === 0 && mode === "playing") onWaveCleared();
    return hitIndices.length;
  }

  // ── Shot pattern ─────────────────────────────────────────────────────
  function onAgentShotPattern(payload) {
    var pattern = String(payload.pattern || "").toLowerCase();
    if (!SHOT_PATTERNS[pattern]) return;
    shotPattern = pattern;
  }

  // Names from `board` whose score is strictly less than `playerScore`, up to 3.
  // Call with a pre-submit board snapshot so the player's own new row isn't
  // counted. The Harbormaster reads these back as ghosts the pilot passed.
  function computeBeatNames(board, playerScore) {
    if (!Array.isArray(board) || !(playerScore > 0)) return [];
    var out = [];
    for (var i = 0; i < board.length && out.length < 3; i++) {
      var row = board[i];
      if (!row) continue;
      if (Number(row.score || 0) < playerScore && row.name) {
        out.push(String(row.name));
      }
    }
    return out;
  }

  async function gameDisconnect() {
    if (gameRoom) {
      try { gameRoom.disconnect(); } catch (_e) {}
    }
    gameRoom = null;
    gameConnected = false;
    gameMicActive = false;
    stopGameTickInterval();
    cleanupGameAudio();
    closeGameMusic();
    gameRttReset();
    setGameVoiceState("disconnected");
  }

  async function suspendDashboardVoiceForGame() {
    resumeDashboardAfterGame = false;
    if (!window.MysticShell || typeof window.MysticShell.pauseForGame !== "function") return;
    try {
      var state = await window.MysticShell.pauseForGame();
      resumeDashboardAfterGame = !!(state && state.voiceWasActive);
    } catch (_e) {
      resumeDashboardAfterGame = false;
    }
  }

  function resumeDashboardVoiceFromGame() {
    if (!window.MysticShell || typeof window.MysticShell.resumeAfterGame !== "function") return;
    window.MysticShell.resumeAfterGame({ voiceWasActive: resumeDashboardAfterGame }).catch(function () {});
    resumeDashboardAfterGame = false;
  }

  // ── Canvas setup + resizing ───────────────────────────────────────────
  function resolveDom() {
    overlay = document.getElementById("game-overlay");
    canvas = document.getElementById("game-canvas");
    modal = document.getElementById("game-modal");
    modalBox = document.getElementById("game-modal-box");
    hudScoreEl = document.getElementById("game-score");
    hudWaveEl = document.getElementById("game-wave");
    hudLivesEl = document.getElementById("game-lives");
    narratorEl = document.getElementById("game-narrator");
    narratorTextEl = document.getElementById("game-narrator-text");
    gameHudAgentWave = document.getElementById("game-hud-agent-wave");
    gameHudUserWave = document.getElementById("game-hud-user-wave");
    gameHudRttEl = document.getElementById("game-hud-rtt");
    gameHudAgentTxt = document.getElementById("game-hud-agent-txt");
    gameHudUserTxt = document.getElementById("game-hud-user-txt");
    gameHudTraceEl = document.getElementById("game-hud-trace");
    gameHudSysDot = document.getElementById("game-hud-sys-dot");
    gameHudSysState = document.getElementById("game-hud-sys-state");
    gameHudSysTimer = document.getElementById("game-hud-sys-timer");
    gameHudSysMode = document.getElementById("game-hud-sys-mode");
    gameHudMicBtn = document.getElementById("game-hud-mic");
    gameMusicVolumeInput = document.getElementById("game-music-volume");
    gameSfxVolumeInput = document.getElementById("game-sfx-volume");
    gameMusicVolumeValue = document.getElementById("game-music-volume-value");
    gameSfxVolumeValue = document.getElementById("game-sfx-volume-value");
    if (canvas) ctx = canvas.getContext("2d");
  }

  function resizeCanvas() {
    if (!canvas || !overlay) return;
    var rect = overlay.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    width = Math.max(320, Math.floor(rect.width));
    height = Math.max(240, Math.floor(rect.height));
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    canvas.style.right = "auto";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function setOverlayWarpState(state) {
    if (!overlay) return;
    if (state) overlay.setAttribute("data-warp", state);
    else overlay.removeAttribute("data-warp");
  }

  function viewportToCanvasPoint(x, y) {
    var rect = overlay ? overlay.getBoundingClientRect() : { left: 0, top: 0 };
    return {
      x: clamp(Number(x) - rect.left, 0, width),
      y: clamp(Number(y) - rect.top, 0, height),
    };
  }

  function getDashboardAgentPoint() {
    if (
      !window.MysticGraph ||
      typeof window.MysticGraph.getAgentScreenPosition !== "function"
    ) {
      return null;
    }
    var point = window.MysticGraph.getAgentScreenPosition();
    if (!point || !isFinite(point.x) || !isFinite(point.y)) return null;
    return viewportToCanvasPoint(point.x, point.y);
  }

  function resolveWarpPoint(originX, originY) {
    var point = null;
    if (originX != null && originY != null && isFinite(originX) && isFinite(originY)) {
      point = viewportToCanvasPoint(originX, originY);
    }
    if (!point) point = getDashboardAgentPoint();
    if (!point && warpPortalPoint) point = { x: warpPortalPoint.x, y: warpPortalPoint.y };
    if (!point) point = { x: width / 2, y: height / 2 };
    warpPortalPoint = { x: point.x, y: point.y };
    return point;
  }

  // ── Stars ─────────────────────────────────────────────────────────────
  function initStars() {
    stars = STAR_LAYERS.map(function (layer) {
      var list = [];
      for (var i = 0; i < layer.count; i++) {
        list.push({
          x: Math.random() * width,
          y: Math.random() * height,
          b: layer.brightness * (0.4 + Math.random() * 0.6),
        });
      }
      return { layer: layer, list: list };
    });
  }

  function updateStars(dt) {
    var driftX = ship ? -ship.vx : 0;
    var driftY = ship ? -ship.vy : 0;
    for (var i = 0; i < stars.length; i++) {
      var layer = stars[i].layer;
      var list = stars[i].list;
      for (var j = 0; j < list.length; j++) {
        var s = list[j];
        s.x = wrap(s.x + driftX * layer.speed * dt, width);
        s.y = wrap(s.y + driftY * layer.speed * dt, height);
      }
    }
  }

  function drawStars() {
    ctx.save();
    for (var i = 0; i < stars.length; i++) {
      var list = stars[i].list;
      for (var j = 0; j < list.length; j++) {
        var s = list[j];
        ctx.fillStyle = "hsla(165, 35%, 70%, " + s.b.toFixed(3) + ")";
        ctx.fillRect(s.x, s.y, 1.2, 1.2);
      }
    }
    ctx.restore();
  }

  // ── Asteroids ────────────────────────────────────────────────────────
  function makeAsteroid(size, x, y, baseAngle) {
    var radius = ASTEROID_SIZES[size];
    var verts = [];
    for (var i = 0; i < ASTEROID_VERT_COUNT; i++) {
      var angle = (i / ASTEROID_VERT_COUNT) * TAU;
      var r = radius * (1 - ASTEROID_JAG + Math.random() * ASTEROID_JAG * 2);
      verts.push({ x: Math.cos(angle) * r, y: Math.sin(angle) * r });
    }
    var speed = rand(ASTEROID_SPEED_MIN, ASTEROID_SPEED_MAX) * (size === "L" ? 1 : size === "M" ? 1.2 : 1.45);
    var theta = baseAngle == null ? Math.random() * TAU : baseAngle + rand(-0.6, 0.6);
    return {
      size: size,
      x: x, y: y,
      vx: Math.cos(theta) * speed,
      vy: Math.sin(theta) * speed,
      angle: Math.random() * TAU,
      spin: rand(-1.5, 1.5),
      radius: radius,
      verts: verts,
    };
  }

  function spawnWaveAsteroids(count) {
    asteroids = [];
    for (var i = 0; i < count; i++) {
      // Spawn from outside the safe zone around the ship
      var x, y, safe = false, tries = 0;
      do {
        x = Math.random() * width;
        y = Math.random() * height;
        tries++;
        if (!ship) { safe = true; break; }
        var d2 = dist2(x, y, ship.x, ship.y);
        safe = d2 > 180 * 180;
      } while (!safe && tries < 20);
      asteroids.push(makeAsteroid("L", x, y, null));
    }
  }

  function drawAsteroid(a) {
    ctx.save();
    ctx.translate(a.x, a.y);
    ctx.rotate(a.angle);
    ctx.strokeStyle = COLOR_GLOW;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    for (var i = 0; i < a.verts.length; i++) {
      var v = a.verts[i];
      if (i === 0) ctx.moveTo(v.x, v.y);
      else ctx.lineTo(v.x, v.y);
    }
    ctx.closePath();
    ctx.shadowColor = COLOR_GLOW;
    ctx.shadowBlur = 6;
    ctx.stroke();
    ctx.restore();
  }

  // ── Ship ──────────────────────────────────────────────────────────────
  function resetShip() {
    ship = {
      x: width / 2,
      y: height / 2,
      vx: 0, vy: 0,
      angle: -Math.PI / 2,
      thrusting: false,
    };
    invulnUntilMs = performance.now() + SHIP_RESPAWN_INVULN_MS;
    idleFiredForLife = false;
    lastInputMs = performance.now();
  }

  function isInvulnerable(now) { return now < invulnUntilMs; }

  function drawShip(now) {
    if (!ship) return;
    if (isInvulnerable(now)) {
      var blink = Math.floor((now - (invulnUntilMs - SHIP_RESPAWN_INVULN_MS)) / 120) % 2;
      if (blink === 0) return;
    }
    ctx.save();
    ctx.translate(ship.x, ship.y);
    ctx.rotate(ship.angle);
    ctx.strokeStyle = COLOR_GLOW;
    ctx.lineWidth = 1.6;
    ctx.shadowColor = COLOR_GLOW;
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.moveTo(SHIP_SIZE, 0);
    ctx.lineTo(-SHIP_SIZE * 0.8, SHIP_SIZE * 0.7);
    ctx.lineTo(-SHIP_SIZE * 0.5, 0);
    ctx.lineTo(-SHIP_SIZE * 0.8, -SHIP_SIZE * 0.7);
    ctx.closePath();
    ctx.stroke();

    if (ship.thrusting && Math.random() < 0.7) {
      ctx.strokeStyle = COLOR_WARN;
      ctx.shadowColor = COLOR_WARN;
      ctx.beginPath();
      ctx.moveTo(-SHIP_SIZE * 0.5, SHIP_SIZE * 0.35);
      ctx.lineTo(-SHIP_SIZE * 1.3 - Math.random() * 4, 0);
      ctx.lineTo(-SHIP_SIZE * 0.5, -SHIP_SIZE * 0.35);
      ctx.stroke();
    }
    ctx.restore();
  }

  // ── Bullets ───────────────────────────────────────────────────────────
  function tryFire(now) {
    if (!ship) return;
    var cooldown = shotPattern === "rapid"
      ? RAPID_FIRE_COOLDOWN_MS
      : shotPattern === "spread"
        ? SPREAD_FIRE_COOLDOWN_MS
        : SHIP_FIRE_COOLDOWN_MS;
    if (now - lastFireMs < cooldown) return;
    if (bullets.length >= BULLET_MAX_IN_FLIGHT) return;
    lastFireMs = now;

    // Spread pattern is a 5-bullet fan centered on the ship's heading;
    // single and rapid fire one bullet straight ahead.
    var offsets = shotPattern === "spread" ? [-0.34, -0.17, 0, 0.17, 0.34] : [0];
    for (var i = 0; i < offsets.length; i++) {
      if (bullets.length >= BULLET_MAX_IN_FLIGHT) break;
      var angle = ship.angle + offsets[i];
      var nx = Math.cos(angle), ny = Math.sin(angle);
      bullets.push({
        x: ship.x + nx * SHIP_SIZE,
        y: ship.y + ny * SHIP_SIZE,
        vx: ship.vx + nx * BULLET_SPEED,
        vy: ship.vy + ny * BULLET_SPEED,
        bornMs: now,
      });
    }
  }

  function drawBullets() {
    ctx.save();
    ctx.fillStyle = COLOR_GLOW;
    ctx.shadowColor = COLOR_GLOW;
    ctx.shadowBlur = 8;
    for (var i = 0; i < bullets.length; i++) {
      var b = bullets[i];
      ctx.fillRect(b.x - BULLET_SIZE / 2, b.y - BULLET_SIZE / 2, BULLET_SIZE, BULLET_SIZE);
    }
    ctx.restore();
  }

  // Atomic-orbit shield — ported from graph.js drawPersonOrbital so the
  // deflector reads as the same "agent energy" as the owner/person node.
  // Five tilted elliptical shells (36° increments), two motes per shell,
  // strongly elliptical (shellRatio 0.30), slow orbit (omega 1.7). Absorbs
  // pop motes off the array — the visible orbit thins until depleted.
  // Mote halo/core radii and alphas mirror graph.js personParams exactly
  // (glowR 0.48, glowA 0.28, coreR 0.22 against shellA 7.0) so the shield
  // reads as the same species of particle cloud as the owner node.
  var SHIELD_NUM_SHELLS = 5;
  var SHIELD_SHELL_RATIO = 0.30;        // minor/major axis — same as personParams
  var SHIELD_OMEGA = 1.7;               // radians/sec
  var SHIELD_NUCLEUS_ALPHA = 0.28;      // faint nucleus halo around the ship
  var SHIELD_MOTE_HALO_RATIO = 0.48 / 7.0;
  var SHIELD_MOTE_CORE_RATIO = 0.22 / 7.0;
  function drawShieldCloud(now) {
    if (!ship || shieldOrbitals.length === 0) return;
    var t = now / 1000;
    var shellA = SHIP_SIZE * 1.55;
    var shellB = shellA * SHIELD_SHELL_RATIO;

    var flashT = Math.max(0, (shieldHitFlashUntil - now) / 280);
    var flashBoost = 1 + flashT * 0.8;
    var flashAlpha = 0.10 + flashT * 0.45;

    ctx.save();

    // Faint nucleus halo — "agent energy" around the hull. Brightens on
    // absorb so the shield crackles with each hit.
    var nucGlow = ctx.createRadialGradient(
      ship.x, ship.y, 0, ship.x, ship.y, shellA * 1.4
    );
    nucGlow.addColorStop(
      0, "hsla(165,90%,85%," + (SHIELD_NUCLEUS_ALPHA + flashAlpha * 0.6).toFixed(3) + ")"
    );
    nucGlow.addColorStop(1, "hsla(165,85%,55%,0)");
    ctx.fillStyle = nucGlow;
    ctx.beginPath();
    ctx.arc(ship.x, ship.y, shellA * 1.4, 0, TAU);
    ctx.fill();

    // Motes — each orbital has a fixed (shell, paired) identity, so as
    // particles are consumed the empty shells read as collapse inward.
    var haloR = shellA * SHIELD_MOTE_HALO_RATIO * flashBoost;
    var coreR = shellA * SHIELD_MOTE_CORE_RATIO * flashBoost;
    for (var i = 0; i < shieldOrbitals.length; i++) {
      var p = shieldOrbitals[i];
      var tilt = (p.shell * Math.PI) / SHIELD_NUM_SHELLS;
      var cosT = Math.cos(tilt), sinT = Math.sin(tilt);
      var theta = SHIELD_OMEGA * t + p.phase;
      var lx = shellA * Math.cos(theta);
      var ly = shellB * Math.sin(theta);
      var mx = ship.x + lx * cosT - ly * sinT;
      var my = ship.y + lx * sinT + ly * cosT;

      var grd = ctx.createRadialGradient(mx, my, 0, mx, my, haloR);
      grd.addColorStop(0, "hsla(165,95%,92%,0.28)");
      grd.addColorStop(1, "hsla(165,85%,60%,0)");
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(mx, my, haloR, 0, TAU);
      ctx.fill();

      ctx.fillStyle = "hsla(165,95%,92%,0.95)";
      ctx.beginPath();
      ctx.arc(mx, my, coreR, 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  }

  // Force-field render — a larger pulsing envelope that matches the actual
  // magnetic repulsion radius.
  function drawForceField(now) {
    if (!ship || !isForceFieldActive(now)) return;
    var remaining = Math.max(0, forceFieldUntil - now);
    var t = (FORCE_FIELD_DURATION_MS - remaining) / FORCE_FIELD_DURATION_MS;
    var alpha = 0.50 * (1 - t * 0.4);
    var pulseR = FORCE_FIELD_RADIUS + Math.sin(now * 0.008) * 5.5;
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    var grd = ctx.createRadialGradient(ship.x, ship.y, 0, ship.x, ship.y, pulseR);
    grd.addColorStop(0, "hsla(165,70%,70%,0)");
    grd.addColorStop(0.72, "hsla(165,95%,80%," + (alpha * 0.18).toFixed(3) + ")");
    grd.addColorStop(0.88, "hsla(165,95%,86%," + (alpha * 0.30).toFixed(3) + ")");
    grd.addColorStop(1, "hsla(165,95%,85%,0)");
    ctx.fillStyle = grd;
    ctx.beginPath();
    ctx.arc(ship.x, ship.y, pulseR, 0, TAU);
    ctx.fill();

    ctx.strokeStyle = "hsla(165,95%,85%," + alpha.toFixed(3) + ")";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.arc(ship.x, ship.y, pulseR, 0, TAU);
    ctx.stroke();

    ctx.strokeStyle = "hsla(50,90%,78%," + (alpha * 0.45).toFixed(3) + ")";
    ctx.lineWidth = 0.8;
    ctx.beginPath();
    ctx.arc(ship.x, ship.y, pulseR * 0.64 + Math.sin(now * 0.011) * 4, 0, TAU);
    ctx.stroke();
    ctx.restore();
  }

  function makeLightningBolt(x0, y0, x1, y1) {
    var pts = [{ x: x0, y: y0 }];
    var dx = x1 - x0, dy = y1 - y0;
    var len = Math.max(1, Math.sqrt(dx * dx + dy * dy));
    var nx = -dy / len, ny = dx / len;
    var steps = 7;
    for (var i = 1; i < steps; i++) {
      var t = i / steps;
      var jitter = rand(-12, 12) * (1 - Math.abs(0.5 - t));
      pts.push({
        x: x0 + dx * t + nx * jitter,
        y: y0 + dy * t + ny * jitter,
      });
    }
    pts.push({ x: x1, y: y1 });
    return pts;
  }

  function spawnLightningEffect(points, now) {
    var bolts = [];
    for (var i = 0; i < points.length - 1; i++) {
      bolts.push(makeLightningBolt(points[i].x, points[i].y, points[i + 1].x, points[i + 1].y));
    }
    weaponEffects.push({
      type: "lightning",
      bornMs: now,
      ttlMs: 360,
      bolts: bolts,
    });
  }

  function spawnVoiceWaveEffect(now, radius) {
    var safeRadius = isFiniteNumber(radius) && radius > 0 ? radius : VOICE_WEAPON_BASE_RADIUS;
    var x = ship && isFiniteNumber(ship.x) ? ship.x : width / 2;
    var y = ship && isFiniteNumber(ship.y) ? ship.y : height / 2;
    weaponEffects.push({
      type: "voice",
      x: x,
      y: y,
      radius: safeRadius,
      bornMs: now,
      ttlMs: 640,
    });
  }

  function drawWeaponEffects(now) {
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    for (var i = weaponEffects.length - 1; i >= 0; i--) {
      var effect = weaponEffects[i];
      var age = (now - effect.bornMs) / effect.ttlMs;
      if (!isFiniteNumber(age) || age >= 1) {
        weaponEffects.splice(i, 1);
        continue;
      }
      if (age < 0) age = 0;
      var alpha = 1 - age;
      if (effect.type === "lightning") {
        ctx.strokeStyle = "hsla(195,95%,82%," + alpha.toFixed(3) + ")";
        ctx.lineWidth = 1.6;
        ctx.shadowColor = "hsl(195,95%,78%)";
        ctx.shadowBlur = 14;
        for (var b = 0; b < effect.bolts.length; b++) {
          var pts = effect.bolts[b];
          ctx.beginPath();
          for (var p = 0; p < pts.length; p++) {
            if (p === 0) ctx.moveTo(pts[p].x, pts[p].y);
            else ctx.lineTo(pts[p].x, pts[p].y);
          }
          ctx.stroke();
        }
      } else if (effect.type === "voice") {
        var r = effect.radius * age;
        if (!isFiniteNumber(r) || r < 0) {
          weaponEffects.splice(i, 1);
          continue;
        }
        ctx.strokeStyle = "hsla(50,90%,78%," + alpha.toFixed(3) + ")";
        ctx.lineWidth = 2.2;
        ctx.shadowColor = "hsl(50,90%,78%)";
        ctx.shadowBlur = 18;
        ctx.beginPath();
        ctx.arc(effect.x, effect.y, r, 0, TAU);
        ctx.stroke();
        ctx.strokeStyle = "hsla(165,95%,85%," + (alpha * 0.5).toFixed(3) + ")";
        ctx.lineWidth = 0.9;
        ctx.beginPath();
        ctx.arc(effect.x, effect.y, r * 0.72, 0, TAU);
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  // ── Particles ─────────────────────────────────────────────────────────
  function spawnExplosion(x, y, scale, color) {
    var count = Math.floor(10 + scale * 14);
    for (var i = 0; i < count; i++) {
      var a = Math.random() * TAU;
      var s = rand(30, 140) * scale;
      particles.push({
        x: x, y: y,
        vx: Math.cos(a) * s, vy: Math.sin(a) * s,
        life: rand(0.4, 0.9),
        maxLife: 0.9,
        color: color || COLOR_GLOW,
      });
    }
  }

  function updateParticles(dt) {
    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.vx *= 0.96;
      p.vy *= 0.96;
      p.life -= dt;
      if (p.life <= 0) particles.splice(i, 1);
    }
  }

  function drawParticles() {
    ctx.save();
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      var alpha = Math.max(0, p.life / p.maxLife);
      ctx.fillStyle = p.color;
      ctx.globalAlpha = alpha;
      ctx.fillRect(p.x - 1, p.y - 1, 2, 2);
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  // ── Collisions ────────────────────────────────────────────────────────
  function awardAsteroidHit(asteroid) {
    score += ASTEROID_SCORES[asteroid.size];
    checkHighScoreMilestone();
    killStreak += 1;
    if (
      killStreakNextThresholdIdx < killStreakThresholds.length &&
      killStreak >= killStreakThresholds[killStreakNextThresholdIdx]
    ) {
      var threshold = killStreakThresholds[killStreakNextThresholdIdx];
      killStreakNextThresholdIdx += 1;
      publishGame("kill_streak", { count: threshold });
    }
    updateHud();
  }

  function destroyAsteroid(index, impactX, impactY, color, split) {
    var asteroid = asteroids[index];
    if (!asteroid) return;
    awardAsteroidHit(asteroid);
    spawnExplosion(
      impactX,
      impactY,
      asteroid.size === "L" ? 1.2 : asteroid.size === "M" ? 0.9 : 0.6,
      color || COLOR_GLOW
    );
    playGameSfx("hit", asteroid.size === "L" ? 1.05 : asteroid.size === "M" ? 0.82 : 0.62, impactX, impactY);
    if (split) {
      var splits = ASTEROID_SPLITS[asteroid.size];
      for (var k = 0; k < splits.length; k++) {
        var baseAngle = Math.atan2(asteroid.vy, asteroid.vx);
        asteroids.push(makeAsteroid(splits[k], asteroid.x, asteroid.y, baseAngle));
      }
    }
    asteroids.splice(index, 1);
  }

  function handleCollisions(now) {
    // Bullet → asteroid
    for (var i = asteroids.length - 1; i >= 0; i--) {
      var a = asteroids[i];
      for (var j = bullets.length - 1; j >= 0; j--) {
        var b = bullets[j];
        var hitRadius = a.radius + BULLET_HIT_PADDING;
        if (dist2(a.x, a.y, b.x, b.y) <= hitRadius * hitRadius) {
          destroyAsteroid(i, b.x, b.y, COLOR_GLOW, true);
          bullets.splice(j, 1);
          if (asteroids.length === 0) onWaveCleared();
          break;
        }
      }
    }

    // Asteroid → ship. Damage order: force field (repulsion + immunity) →
    // shield cloud (absorbs hit) → hull (ship destroyed).
    if (ship && !isInvulnerable(now) && !isForceFieldActive(now)) {
      for (var m = 0; m < asteroids.length; m++) {
        var ast = asteroids[m];
        var rr = ast.radius + SHIP_SIZE * 0.7;
        if (dist2(ast.x, ast.y, ship.x, ship.y) <= rr * rr) {
          if (shieldParticlesLeft > 0) {
            absorbWithShield(ast, m);
            return;
          }
          onShipHit();
          return;
        }
      }
    }

    // Near-miss detection — only for large asteroids, 8s minimum gap.
    // Anything tighter makes the Harbormaster too chatty.
    if (ship && !isInvulnerable(now) && now - nearMissFiredAtMs > 8000) {
      for (var n = 0; n < asteroids.length; n++) {
        var b2 = asteroids[n];
        if (b2.size !== "L") continue;
        var gap = Math.sqrt(dist2(b2.x, b2.y, ship.x, ship.y)) - b2.radius - SHIP_SIZE;
        if (gap > 0 && gap < NEAR_MISS_RADIUS) {
          nearMissFiredAtMs = now;
          publishGame("near_miss", {});
          break;
        }
      }
    }
  }

  // Shield absorb: vaporize the rock, no split, no score. Consume particles
  // by rock size — fragments are just cosmetic dust. When the cloud runs
  // out the next hit falls through to onShipHit.
  function absorbWithShield(asteroid, index) {
    var cost = SHIELD_HITS_BY_SIZE[asteroid.size] || 1;
    var consumed = Math.min(cost, shieldParticlesLeft);
    var wasOnline = shieldParticlesLeft > 0;
    shieldParticlesLeft = Math.max(0, shieldParticlesLeft - consumed);
    for (var k = 0; k < consumed && shieldOrbitals.length > 0; k++) {
      shieldOrbitals.pop();
    }
    shieldHitFlashUntil = performance.now() + 280;
    playGameSfx("shield_hit", asteroid.size === "L" ? 1.1 : asteroid.size === "M" ? 0.85 : 0.65, asteroid.x, asteroid.y);
    spawnExplosion(
      asteroid.x, asteroid.y,
      asteroid.size === "L" ? 1.0 : asteroid.size === "M" ? 0.7 : 0.5,
      "hsl(165,90%,82%)"
    );
    asteroids.splice(index, 1);
    // Alarm the Copilot only when the shield actually ran out on THIS
    // absorb — not on every hit. This is the one shield event that
    // forces a reaction.
    if (wasOnline && shieldParticlesLeft === 0) {
      playGameSfx("shield_down", 1);
      publishGame("shield_depleted", {});
    }
    if (asteroids.length === 0) onWaveCleared();
  }

  function checkHighScoreMilestone() {
    if (highScoreFired) return;
    if (highScore > 0 && score > highScore) {
      highScoreFired = true;
      publishGame("high_score", { score: score });
    }
  }

  // Tick-loop gated watchdogs. Each nudge fires at most once per (wave|life)
  // so the Harbormaster does not interrupt himself. Only runs while a ship is
  // alive and flyable — invulnerability counts as presence.
  function checkNudgeWatchdogs(now) {
    if (!waveStalledFired &&
        asteroids.length > 0 &&
        waveStartMs > 0 &&
        now - waveStartMs > 45000) {
      waveStalledFired = true;
      publishGame("wave_stalled", { wave: wave });
    }
    if (!idleFiredForLife &&
        ship &&
        lastInputMs > 0 &&
        now - lastInputMs > 20000) {
      idleFiredForLife = true;
      publishGame("idle", {});
    }
  }

  // ── Update + draw loop ────────────────────────────────────────────────
  function updateShip(dt, now) {
    if (!ship) return;
    if (inputState.left) ship.angle -= SHIP_ROTATE_RATE * dt;
    if (inputState.right) ship.angle += SHIP_ROTATE_RATE * dt;
    ship.thrusting = inputState.thrust;
    if (ship.thrusting) {
      ship.vx += Math.cos(ship.angle) * SHIP_THRUST * dt;
      ship.vy += Math.sin(ship.angle) * SHIP_THRUST * dt;
      var speed = Math.sqrt(ship.vx * ship.vx + ship.vy * ship.vy);
      if (speed > SHIP_MAX_SPEED) {
        ship.vx = ship.vx / speed * SHIP_MAX_SPEED;
        ship.vy = ship.vy / speed * SHIP_MAX_SPEED;
      }
    } else {
      ship.vx *= Math.pow(SHIP_FRICTION, dt * 60);
      ship.vy *= Math.pow(SHIP_FRICTION, dt * 60);
    }
    ship.x = wrap(ship.x + ship.vx * dt, width);
    ship.y = wrap(ship.y + ship.vy * dt, height);

    if (inputState.fire) tryFire(now);

    if (inputState.hyper && now - lastHyperMs > SHIP_HYPER_COOLDOWN_MS) {
      lastHyperMs = now;
      ship.x = Math.random() * width;
      ship.y = Math.random() * height;
      ship.vx = 0; ship.vy = 0;
      spawnExplosion(ship.x, ship.y, 0.4, COLOR_GLOW_SOFT);
      playGameSfx("hyper", 0.85, ship.x, ship.y);
    }
  }

  function updateAsteroids(dt, now) {
    for (var i = 0; i < asteroids.length; i++) {
      var a = asteroids[i];
      a.x = wrap(a.x + a.vx * dt, width);
      a.y = wrap(a.y + a.vy * dt, height);
      a.angle += a.spin * dt;
    }
    applyForceFieldRepulsion(dt, now);
  }

  function applyForceFieldRepulsion(dt, now) {
    if (!ship || !isForceFieldActive(now)) return;
    for (var i = 0; i < asteroids.length; i++) {
      var a = asteroids[i];
      var vec = wrappedVector(ship.x, ship.y, a.x, a.y);
      var d2 = vec.dx * vec.dx + vec.dy * vec.dy;
      var influence = FORCE_FIELD_RADIUS + a.radius;
      if (d2 > influence * influence) continue;

      var dist = Math.sqrt(Math.max(1, d2));
      var nx = vec.dx / dist;
      var ny = vec.dy / dist;
      if (dist <= 1.1) {
        var angle = Math.random() * TAU;
        nx = Math.cos(angle);
        ny = Math.sin(angle);
        dist = 1.1;
      }

      var depth = influence - dist;
      var pressure = clamp(depth / influence, 0, 1);
      var inward = a.vx * nx + a.vy * ny;
      if (inward < 0) {
        a.vx -= nx * inward * FORCE_FIELD_BOUNCE;
        a.vy -= ny * inward * FORCE_FIELD_BOUNCE;
      }
      var accel = FORCE_FIELD_REPEL_ACCEL * (0.25 + pressure * pressure);
      a.vx += nx * accel * dt;
      a.vy += ny * accel * dt;
      a.x = wrap(a.x + nx * depth * FORCE_FIELD_POSITION_PUSH, width);
      a.y = wrap(a.y + ny * depth * FORCE_FIELD_POSITION_PUSH, height);
      a.spin += rand(-1, 1) * pressure * dt * 8;
      clampBodySpeed(a, FORCE_FIELD_ASTEROID_MAX_SPEED);
    }
  }

  function updateBullets(dt, now) {
    for (var i = bullets.length - 1; i >= 0; i--) {
      var b = bullets[i];
      b.x += b.vx * dt;
      b.y += b.vy * dt;
      if (
        now - b.bornMs > BULLET_TTL_MS ||
        b.x < -BULLET_DESPAWN_MARGIN ||
        b.x > width + BULLET_DESPAWN_MARGIN ||
        b.y < -BULLET_DESPAWN_MARGIN ||
        b.y > height + BULLET_DESPAWN_MARGIN
      ) {
        bullets.splice(i, 1);
      }
    }
  }

  function drawScene(now) {
    drawStars();
    drawParticles();
    for (var i = 0; i < asteroids.length; i++) drawAsteroid(asteroids[i]);
    drawBullets();
    drawWeaponEffects(now);
    drawShip(now);
    drawShieldCloud(now);
    drawForceField(now);
  }

  function drawFrame(now) {
    if (warp && (warp.phase === "in" || warp.phase === "out")) {
      drawWarpFrame(now);
      return;
    }
    ctx.fillStyle = COLOR_BG;
    ctx.fillRect(0, 0, width, height);
    drawScene(now);
    if (warp) drawWarp();
  }

  function drawWarpFrame(now) {
    if (!warp) return;
    var t = warp.t || 0;
    var progress = easeInOutCubic(t);
    var scale = warp.phase === "in"
      ? WARP_SCENE_MIN_SCALE + (1 - WARP_SCENE_MIN_SCALE) * progress
      : 1 - (1 - WARP_SCENE_MIN_SCALE) * progress;
    var alpha = warp.phase === "in"
      ? smoothstep(0.02, 0.18, t)
      : 1 - smoothstep(0.86, 1, t);

    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(warp.ox, warp.oy);
    ctx.scale(scale, scale);
    ctx.translate(-warp.ox, -warp.oy);
    ctx.fillStyle = COLOR_BG;
    ctx.fillRect(0, 0, width, height);
    drawScene(now);
    ctx.restore();
    drawWarp();
  }

  function tick(now) {
    var dt = Math.min(0.05, (now - lastFrameMs) / 1000);
    lastFrameMs = now;

    if (mode === "warp_in" || mode === "warp_out") {
      updateWarp(dt, now);
      drawFrame(now);
      finishCompletedWarp();
    } else if (mode === "playing") {
      updateStars(dt);
      updateShip(dt, now);
      updateAsteroids(dt, now);
      updateBullets(dt, now);
      updateParticles(dt);
      handleCollisions(now);
      checkNudgeWatchdogs(now);
      drawFrame(now);
    } else if (mode === "intermission") {
      updateStars(dt);
      updateAsteroids(dt, now);
      updateParticles(dt);
      drawFrame(now);
      if (now >= waveEndAtMs) startNextWave();
    } else if (mode === "paused" || mode === "game_over") {
      drawFrame(now);
    }

    tickNarrator(now);
    updateGameMusicMix();
    // Mic state can change due to bridge reconnects — cheap refresh.
    refreshMicButton();

    if (mode !== "idle") rafId = requestAnimationFrame(tick);
  }

  // ── Warp transitions ──────────────────────────────────────────────────
  function beginWarpIn(originX, originY) {
    var point = resolveWarpPoint(originX, originY);
    var ox = point.x, oy = point.y;
    var streaks = [];
    for (var i = 0; i < WARP_STAR_COUNT; i++) {
      var a = Math.random() * TAU;
      streaks.push({
        angle: a,
        r: rand(4, 40),
        vr: rand(280, 960),
        brightness: 0.35 + Math.random() * 0.65,
      });
    }
    warp = {
      phase: "in",
      t0: performance.now(),
      duration: WARP_IN_MS,
      ox: ox, oy: oy,
      streaks: streaks,
    };
    setOverlayWarpState("in");
    mode = "warp_in";
  }

  function beginWarpOut(onDone) {
    stopGameMusic(0.8);
    var point = resolveWarpPoint(null, null);
    var streaks = [];
    for (var i = 0; i < WARP_STAR_COUNT; i++) {
      var a = Math.random() * TAU;
      streaks.push({
        angle: a,
        r: rand(Math.max(width, height) * 0.6, Math.max(width, height) * 1.2),
        vr: -rand(360, 1080),
        brightness: 0.45 + Math.random() * 0.55,
      });
    }
    warp = {
      phase: "out",
      t0: performance.now(),
      duration: WARP_OUT_MS,
      ox: point.x, oy: point.y,
      streaks: streaks,
      onDone: onDone,
    };
    setOverlayWarpState("out");
    mode = "warp_out";
  }

  function updateWarp(dt, now) {
    if (!warp) return;
    var t = Math.min(1, (now - warp.t0) / warp.duration);
    warp.t = t;
    for (var i = 0; i < warp.streaks.length; i++) {
      var s = warp.streaks[i];
      s.r += s.vr * dt;
      if (warp.phase === "out") s.r = Math.max(0, s.r);
    }
    if (t >= 1) {
      if (warp.phase === "in") {
        warp = null;
        mode = "playing";
        setOverlayWarpState(null);
        lastFrameMs = now;
      } else {
        warp.complete = true;
      }
    }
  }

  function finishCompletedWarp() {
    if (!warp || !warp.complete) return;
    var cb = warp.onDone;
    warp = null;
    mode = "idle";
    setOverlayWarpState(null);
    if (typeof cb === "function") cb();
  }

  function drawWarp() {
    if (!warp) return;
    var ox = warp.ox, oy = warp.oy;
    var t = warp.t || 0;
    var progress = easeInOutCubic(t);
    var scale = warp.phase === "in"
      ? WARP_SCENE_MIN_SCALE + (1 - WARP_SCENE_MIN_SCALE) * progress
      : 1 - (1 - WARP_SCENE_MIN_SCALE) * progress;
    var diag = Math.sqrt(width * width + height * height);
    var ringR = Math.max(18, diag * 0.58 * scale);
    var ringAlpha = warp.phase === "in"
      ? (1 - smoothstep(0.52, 0.92, t)) * 0.9
      : smoothstep(0.34, 0.9, t) * (1 - smoothstep(0.94, 1, t)) * 0.9;
    var streakAlpha = warp.phase === "in"
      ? 1 - smoothstep(0.72, 1, t)
      : 1 - smoothstep(0.88, 1, t);

    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    ctx.strokeStyle = COLOR_GLOW;
    ctx.lineWidth = 1.2;
    ctx.shadowColor = COLOR_GLOW;
    ctx.shadowBlur = 6;
    for (var i = 0; i < warp.streaks.length; i++) {
      var s = warp.streaks[i];
      var tail = Math.min(80, Math.max(12, Math.abs(s.vr) * 0.04));
      var head = Math.max(0, s.r);
      var tailR = warp.phase === "out" ? head + tail : Math.max(0, head - tail);
      var x1 = ox + Math.cos(s.angle) * head;
      var y1 = oy + Math.sin(s.angle) * head;
      var x0 = ox + Math.cos(s.angle) * tailR;
      var y0 = oy + Math.sin(s.angle) * tailR;
      ctx.globalAlpha = s.brightness * streakAlpha;
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }

    if (ringAlpha > 0.01 && ringR < diag * 0.95) {
      ctx.globalAlpha = ringAlpha;
      ctx.lineWidth = 1.6;
      ctx.shadowBlur = 18;
      ctx.beginPath();
      ctx.arc(ox, oy, ringR, 0, TAU);
      ctx.stroke();
      ctx.globalAlpha = ringAlpha * 0.42;
      ctx.lineWidth = 0.8;
      ctx.beginPath();
      ctx.arc(ox, oy, ringR * 0.72, 0, TAU);
      ctx.stroke();
      ctx.globalAlpha = ringAlpha * 0.34;
      ctx.fillStyle = COLOR_GLOW_SOFT;
      ctx.beginPath();
      ctx.arc(ox, oy, Math.max(5, 14 * (1 - scale + WARP_SCENE_MIN_SCALE)), 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  }

  // ── State transitions ─────────────────────────────────────────────────
  function startGame(originX, originY) {
    if (mode !== "idle") return;
    resolveDom();
    if (!overlay || !canvas || !ctx) return;
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("game-mode");
    // Take keyboard focus away from anything behind the overlay (chat
    // input, search, etc.) so typing goes to the game.
    if (document.activeElement && typeof document.activeElement.blur === "function") {
      try { document.activeElement.blur(); } catch (_e) {}
    }
    bindMicButton();
    bindGameAudioControls();
    resetGameChrome();
    refreshGameAudioControls();
    refreshMicButton();
    if (narratorEl) narratorEl.setAttribute("data-visible", "0");
    narratorHideAt = 0;
    resizeCanvas();
    resizeGameHudCanvases();
    initStars();
    startGameMusic();
    score = 0; wave = 0; lives = 3;
    asteroids = []; bullets = []; particles = []; weaponEffects = [];
    inputState = { left: false, right: false, thrust: false, fire: false, hyper: false };
    highScoreFired = false;
    scoreLocked = false;
    justSubmittedScoreId = null;
    lightningReadyAt = 0;
    voiceWeaponReadyAt = 0;
    hideModal();
    updateHud();
    resetShip();
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("pointerdown", onGamePointer);
    window.addEventListener("resize", resizeCanvas);
    window.addEventListener("resize", resizeGameHudCanvases);
    beginWarpIn(originX, originY);
    lastFrameMs = performance.now();
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(tick);

    // Fetch leaderboard + connect to the game's own LiveKit room in parallel.
    // The Harbormaster is born in that room with the Hades voice — no persona
    // swap, no context to preserve. Mic defaults ON so the agent's event
    // nudges are actually spoken out loud.
    // The leaderboard promise is captured so the first `start` nudge can
    // include the top 3 ghosts — the Harbormaster greets by name.
    var leaderboardReady = fetchLeaderboard();
    (async function () {
      await suspendDashboardVoiceForGame();
      var ok = await gameConnect();
      if (ok) {
        await gameStartMic();
      }
      refreshMicButton();
      try { await leaderboardReady; } catch (_e) {}
      // Small delay so the agent pipeline is warm before the first nudge.
      setTimeout(function () {
        startWave(WAVE_FIRST, /*announce*/ true);
      }, 400);
    })();
  }

  function startWave(n, announce) {
    wave = n;
    var largeCount = WAVE_BASE_LARGE + (n - WAVE_FIRST) * WAVE_STEP;
    spawnWaveAsteroids(largeCount);
    waveStartMs = performance.now();
    waveStalledFired = false;
    updateHud();
    // Every fresh run publishes `system_boot` once so the Copilot gets a
    // single quiet boot ack. Subsequent wave transitions are invisible.
    if (announce) publishGame("system_boot", { wave: wave });
  }

  function startNextWave() {
    publishGame("wave_cleared", { wave: wave, next_wave: wave + 1 });
    mode = "playing";
    // Shield + force-field cooldowns tick down each wave transition.
    // Shield cloud itself carries across waves until particles are spent.
    if (shieldCooldownWaves > 0) shieldCooldownWaves -= 1;
    if (forceFieldCooldownWaves > 0) forceFieldCooldownWaves -= 1;
    startWave(wave + 1, false);
  }

  function onWaveCleared() {
    mode = "intermission";
    waveEndAtMs = performance.now() + WAVE_INTERMISSION_MS;
    playGameSfx("wave", 1);
  }

  function onShipHit() {
    spawnExplosion(ship.x, ship.y, 1.6, COLOR_WARN);
    lives -= 1;
    playGameSfx("ship_hit", lives <= 0 ? 1.25 : 0.95, ship.x, ship.y);
    killStreak = 0;
    killStreakNextThresholdIdx = 0;
    idleFiredForLife = false;
    lastInputMs = performance.now();
    updateHud();
    // Ship-alarm names (hull_lost / ship_destroyed) match the Copilot's
    // mental model: there's the ship, it took hull damage, or it's
    // destroyed. Gameplay-flow names like "death" are out.
    if (lives <= 0) {
      ship = null;
      publishGame("ship_destroyed", { wave: wave, score: score });
      gameOver();
      return;
    }
    publishGame("hull_lost", { lives_remaining: lives });
    ship = null;
    setTimeout(function () {
      if (mode === "playing") resetShip();
    }, 1100);
  }

  function gameOver() {
    mode = "game_over";
    gameOverAtMs = performance.now();
    playGameSfx("game_over", 1);
    // Show the modal immediately with no submitted result. The user picks
    // their initials, then submits on Enter — that's when we publish the
    // game_over event with the final rank. If they bail early with Esc or
    // "Warp out", we publish a no-submission game_over at that point.
    showGameOverModal(null);
    if (score === 0) {
      // Nothing worth logging; send a game_over so the agent can sign off.
      publishGame("game_over", {
        score: 0, wave: wave, rank: null, prev_best: null, beat_names: [],
      });
    }
  }

  function exitGame() {
    // If the user bails from game-over without locking initials, still
    // publish a terminal game_over so the Harbormaster gets a sign-off
    // cue (rank null because the score was never submitted).
    if (mode === "game_over" && !scoreLocked && score > 0) {
      publishGame("game_over", {
        score: score, wave: wave, rank: null, prev_best: null,
        beat_names: computeBeatNames(leaderboard, score),
      });
    }
    hideModal();
    shieldParticlesLeft = 0;
    shieldOrbitals = [];
    shieldCooldownWaves = 0;
    forceFieldUntil = 0;
    forceFieldCooldownWaves = 0;
    shiftJumpReadyAt = 0;
    lightningReadyAt = 0;
    voiceWeaponReadyAt = 0;
    shotPattern = "single";
    if (narratorEl) narratorEl.setAttribute("data-visible", "0");
    beginWarpOut(function () {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("pointerdown", onGamePointer);
      window.removeEventListener("resize", resizeCanvas);
      window.removeEventListener("resize", resizeGameHudCanvases);
      if (overlay) {
        overlay.hidden = true;
        overlay.setAttribute("aria-hidden", "true");
      }
      document.body.classList.remove("game-mode");
      asteroids = []; bullets = []; particles = []; weaponEffects = []; ship = null;
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
      // Disconnect from the game room last — after the warp animation
      // completes — so the Harbormaster's final sign-off nudge has a
      // chance to be spoken before the room tears down.
      gameDisconnect();
      resumeDashboardVoiceFromGame();
    });
  }

  function restartGame() {
    if (mode === "game_over") {
      // Do a clean restart without warping out
      hideModal();
      score = 0; wave = 0; lives = 3;
      asteroids = []; bullets = []; particles = []; weaponEffects = [];
      highScoreFired = false;
      scoreLocked = false;
      justSubmittedScoreId = null;
      killStreak = 0;
      killStreakNextThresholdIdx = 0;
      waveStalledFired = false;
      idleFiredForLife = false;
      shieldParticlesLeft = 0;
      shieldOrbitals = [];
      shieldCooldownWaves = 0;
      forceFieldUntil = 0;
      forceFieldCooldownWaves = 0;
      shiftJumpReadyAt = 0;
      lightningReadyAt = 0;
      voiceWeaponReadyAt = 0;
      shotPattern = "single";
      resetShip();
      updateHud();
      mode = "playing";
      publishGame("system_boot", { wave: WAVE_FIRST });
      startWave(WAVE_FIRST, false);
    }
  }

  // ── HUD ───────────────────────────────────────────────────────────────
  function updateHud() {
    if (hudScoreEl) hudScoreEl.textContent = String(score);
    if (hudWaveEl) hudWaveEl.textContent = String(wave || 1);
    if (hudLivesEl) {
      var tri = "";
      for (var i = 0; i < Math.max(0, lives); i++) tri += "\u25B2";
      hudLivesEl.textContent = tri || "—";
    }
  }

  function refreshMicButton() {
    if (!gameHudMicBtn) return;
    gameHudMicBtn.setAttribute("data-active", gameMicActive ? "1" : "0");
    gameHudMicBtn.textContent = gameMicActive ? "LIVE" : "MIC";
  }

  function bindMicButton() {
    if (micListenerBound || !gameHudMicBtn) return;
    micListenerBound = true;
    gameHudMicBtn.addEventListener("click", async function () {
      if (gameHudMicBtn.disabled) return;
      var turningOn = !gameMicActive;
      if (window.MysticSoundFx && typeof window.MysticSoundFx.playMicToggle === "function") {
        window.MysticSoundFx.playMicToggle(turningOn);
      }
      if (turningOn) {
        await gameStartMic();
      } else {
        await gameStopMic();
      }
      refreshMicButton();
    });
  }

  function bindGameAudioControls() {
    if (audioControlsBound) return;
    audioControlsBound = true;
    if (gameMusicVolumeInput) {
      gameMusicVolumeInput.addEventListener("input", function () {
        resumeGameMusicContext();
        setGameMusicVolume(Number(gameMusicVolumeInput.value || 0) / 100);
      });
    }
    if (gameSfxVolumeInput) {
      gameSfxVolumeInput.addEventListener("input", function () {
        resumeGameMusicContext();
        setGameSfxVolume(Number(gameSfxVolumeInput.value || 0) / 100);
      });
    }
  }

  function showNarrator(text, streaming) {
    if (!narratorEl || !narratorTextEl) return;
    narratorTextEl.textContent = text;
    narratorEl.setAttribute("data-visible", "1");
    // Keep visible a while after final; fast refresh while streaming.
    narratorHideAt = performance.now() + (streaming ? 8000 : 5000);
  }

  function tickNarrator(now) {
    if (!narratorEl) return;
    if (narratorHideAt && now >= narratorHideAt) {
      narratorEl.setAttribute("data-visible", "0");
      narratorHideAt = 0;
    }
  }

  // ── Input ─────────────────────────────────────────────────────────────
  function onKeyDown(e) {
    if (mode === "idle" || mode === "warp_in" || mode === "warp_out") return;
    resumeGameMusicContext();
    if (e.code === "Escape" || e.key === "Escape") {
      e.preventDefault();
      if (mode === "playing" || mode === "intermission" || mode === "paused") {
        togglePause();
      } else if (mode === "game_over") {
        exitGame();
      }
      return;
    }
    var action = KEY_MAP[e.code];
    if (!action) return;
    e.preventDefault();
    inputState[action] = true;
    lastInputMs = performance.now();
  }

  function onKeyUp(e) {
    var action = KEY_MAP[e.code];
    if (!action) return;
    e.preventDefault();
    inputState[action] = false;
    lastInputMs = performance.now();
  }

  function onGamePointer() {
    if (mode !== "idle") resumeGameMusicContext();
  }

  function togglePause() {
    if (mode === "playing" || mode === "intermission") {
      mode = "paused";
      setGameMusicPaused(true);
      showPauseModal();
    } else if (mode === "paused") {
      hideModal();
      mode = asteroids.length === 0 ? "intermission" : "playing";
      if (mode === "intermission") waveEndAtMs = performance.now() + WAVE_INTERMISSION_MS;
      setGameMusicPaused(false);
      lastFrameMs = performance.now();
    }
  }

  // ── Leaderboard I/O ───────────────────────────────────────────────────
  async function fetchLeaderboard() {
    try {
      var resp = await fetch("/dashboard/api/game/scores", { credentials: "same-origin" });
      if (!resp.ok) return;
      var data = await resp.json();
      leaderboard = Array.isArray(data.scores) ? data.scores : [];
      highScore = leaderboard.length > 0 ? Number(leaderboard[0].score || 0) : 0;
      if (mode === "game_over" && !scoreLocked) {
        renderGameOverModal(null, "");
      }
    } catch (_e) {}
  }

  async function submitScore() {
    if (score <= 0) return null;
    var name = initials.join("");
    try {
      var resp = await fetch("/dashboard/api/game/scores", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ name: name, score: score, wave: wave }),
      });
      if (!resp.ok) return null;
      var data = await resp.json();
      justSubmittedScoreId = data.id;
      // Refresh board
      await fetchLeaderboard();
      return data;
    } catch (_e) { return null; }
  }

  // ── Modal helpers ─────────────────────────────────────────────────────
  function showModal(contentHtml, keyHandler) {
    if (!modal || !modalBox) return;
    modalBox.innerHTML = contentHtml;
    modal.hidden = false;
    if (overlay) overlay.setAttribute("data-menu", "1");
    refreshGameAudioControls();
    if (modalKeyHandler) {
      window.removeEventListener("keydown", modalKeyHandler, true);
      modalKeyHandler = null;
    }
    if (typeof keyHandler === "function") {
      modalKeyHandler = keyHandler;
      window.addEventListener("keydown", modalKeyHandler, true);
    }
  }

  function hideModal() {
    if (modal) modal.hidden = true;
    if (modalBox) modalBox.innerHTML = "";
    if (overlay) overlay.removeAttribute("data-menu");
    if (modalKeyHandler) {
      window.removeEventListener("keydown", modalKeyHandler, true);
      modalKeyHandler = null;
    }
  }

  function showPauseModal() {
    showModal(
      '<div class="game-modal-title">Paused</div>' +
      '<div class="game-modal-sub">You are suspended between realities.</div>' +
      '<div class="game-actions">' +
        '<button type="button" class="game-btn" data-game-action="resume">Resume</button>' +
        '<button type="button" class="game-btn" data-game-action="exit">Warp out</button>' +
      '</div>' +
      '<div class="game-hint">Esc to resume. Rotate &larr;&rarr;. Thrust &uarr;. Fire space. Shift = hyperspace.</div>'
    );
    bindModalButtons();
  }

  function showGameOverModal(result) {
    var rank = result && result.rank ? result.rank : null;
    var rankBlurb = rank ? 'Rank ' + rank : '';
    if (!result) {
      // Fresh game-over — reset initials for entry.
      initials = ["A", "A", "A"];
      initialIdx = 0;
      justSubmittedScoreId = null;
    }
    renderGameOverModal(result, rankBlurb);
  }

  function renderGameOverModal(result, rankBlurb) {
    var canSaveScore = !scoreLocked && score > 0;
    var statusBlurb = scoreLocked ? "Score logged." : (score > 0 ? "Choose your initials, then save." : "No score to log.");
    var slotsHtml = '<div class="game-initials-row">' +
      [0, 1, 2].map(function (i) {
        return '<div class="game-initial-slot" data-active="' + (i === initialIdx ? "1" : "0") + '">' + initials[i] + '</div>';
      }).join("") + '</div>';

    var boardHtml = '<div class="game-leaderboard">' +
      leaderboard.slice(0, 8).map(function (row, i) {
        var highlight = justSubmittedScoreId && row.name === initials.join("") && row.score === score;
        return '<div class="game-leaderboard-row" data-highlight="' + (highlight ? "1" : "0") + '">' +
          '<span class="game-leaderboard-row-num">' + (i + 1) + '</span>' +
          '<span>' + escapeHtml(row.name) + '</span>' +
          '<span class="game-leaderboard-row-score">' + Number(row.score || 0) + '</span>' +
          '<span class="game-leaderboard-row-wave">w' + Number(row.wave || 1) + '</span>' +
        '</div>';
      }).join("") +
    '</div>';

    showModal(
      '<div class="game-modal-title">End of Run</div>' +
      '<div class="game-modal-sub">Pilot burned out. ' + statusBlurb + '</div>' +
      '<div class="game-score-big">' + score + '</div>' +
      (rankBlurb ? '<div class="game-rank">' + rankBlurb + '</div>' : '') +
      '<div class="game-modal-sub">Stamp your mark</div>' +
      slotsHtml +
      boardHtml +
      '<div class="game-actions">' +
        (canSaveScore ? '<button type="button" class="game-btn" data-game-action="save-score">Save score</button>' : '') +
        '<button type="button" class="game-btn" data-game-action="restart">Again</button>' +
        '<button type="button" class="game-btn" data-game-action="exit">Warp out</button>' +
      '</div>' +
      '<div class="game-hint">&uarr;/&darr; to cycle. &larr;/&rarr; to move. Enter to lock.</div>',
      initialsKeyHandler
    );
    bindModalButtons();
  }

  function initialsKeyHandler(e) {
    if (mode !== "game_over") return;
    if (e.target && e.target.type === "range") return;
    if (e.code === "Escape") {
      e.preventDefault();
      exitGame();
      return;
    }
    if (scoreLocked) return;
    var handled = false;
    if (e.code === "ArrowUp" || e.code === "KeyW") {
      initials[initialIdx] = nextLetter(initials[initialIdx], +1); handled = true;
    } else if (e.code === "ArrowDown" || e.code === "KeyS") {
      initials[initialIdx] = nextLetter(initials[initialIdx], -1); handled = true;
    } else if (e.code === "ArrowLeft" || e.code === "KeyA") {
      initialIdx = Math.max(0, initialIdx - 1); handled = true;
    } else if (e.code === "ArrowRight" || e.code === "KeyD") {
      initialIdx = Math.min(2, initialIdx + 1); handled = true;
    } else if (e.code === "Enter") {
      handled = true;
      if (initialIdx < 2) { initialIdx += 1; }
      else {
        e.preventDefault();
        lockInitials();
        return;
      }
    } else if (/^Key[A-Z]$/.test(e.code)) {
      initials[initialIdx] = e.code.replace("Key", "");
      if (initialIdx < 2) initialIdx += 1;
      handled = true;
    } else if (e.code === "Backspace") {
      initialIdx = Math.max(0, initialIdx - 1); handled = true;
    }
    if (handled) {
      e.preventDefault();
      renderGameOverModal(null, "");
    }
  }

  function nextLetter(ch, dir) {
    var code = ch.charCodeAt(0);
    if (dir > 0) return code >= 90 ? "A" : String.fromCharCode(code + 1);
    return code <= 65 ? "Z" : String.fromCharCode(code - 1);
  }

  async function lockInitials() {
    if (scoreLocked) return;
    scoreLocked = true;
    // Snapshot the board BEFORE submitScore runs — the post refreshes the
    // leaderboard to include this run's own row, which would poison
    // beat_names if we read it after.
    var preSubmitBoard = leaderboard.slice();
    var result = await submitScore();
    if (!result) {
      scoreLocked = false;
      renderGameOverModal(null, "");
      return;
    }
    publishGame("game_over", {
      score: score, wave: wave,
      rank: result ? result.rank : null,
      prev_best: result ? result.prev_best : null,
      beat_names: computeBeatNames(preSubmitBoard, score),
    });
    var rankBlurb = result && result.rank ? 'Rank ' + result.rank : '';
    renderGameOverModal(result, rankBlurb);
  }

  function bindModalButtons() {
    if (!modalBox) return;
    modalBox.querySelectorAll("[data-game-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        resumeGameMusicContext();
        var act = btn.getAttribute("data-game-action");
        if (act === "resume") togglePause();
        else if (act === "exit") exitGame();
        else if (act === "restart") restartGame();
        else if (act === "save-score") lockInitials();
      });
    });
  }

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  // ── Bootstrap ─────────────────────────────────────────────────────────
  window.addEventListener("mh:game-start", function (e) {
    var origin = (e && e.detail) || {};
    startGame(origin.originX, origin.originY);
  });

  // Expose for debugging / potential future triggers (not used in UI).
  window.MysticGame = {
    start: function (opts) {
      opts = opts || {};
      startGame(opts.originX, opts.originY);
    },
    musicState: function () {
      return {
        context: gameMusicCtx ? gameMusicCtx.state : "none",
        playing: gameMusicPlaying,
        targetGain: gameMusicTargetGain,
        musicVolume: gameMusicVolume,
        sfxVolume: gameSfxVolume,
        nextTime: gameMusicNextTime,
        timer: !!gameMusicTimer,
      };
    },
    exit: exitGame,
  };
})();
