(function () {
  "use strict";

  function createVoiceBridge(options) {
    var CHAT_ACK_TIMEOUT_MS = 5000;
    var HEARTBEAT_INTERVAL_MS = 60000;
    var callbacks = options || {};
    var onState = callbacks.onState || function () {};
    var onError = callbacks.onError || function () {};
    var onEvent = callbacks.onEvent || function () {};
    var onHistory = callbacks.onHistory || function () {};
    var onHudHistory = callbacks.onHudHistory || function () {};
    var onTranscript = callbacks.onTranscript || function () {};
    var onAgentAudio = callbacks.onAgentAudio || function () {};
    var onUserAudio = callbacks.onUserAudio || function () {};
    var onProgress = callbacks.onProgress || function () {};

    var room = null;
    var connected = false;
    var voiceActive = false;
    var callId = null;
    var roomName = null;
    var participantName = null;
    var connectPromise = null;
    var heartbeatInterval = null;
    var pendingChatAcks = [];
    var disconnectNotified = false;

    var audioCtx = null;
    var agentAnalyser = null;
    var agentAnalyserTrack = null;
    var userAnalyser = null;
    var userAnalyserTrack = null;
    var analyserNodes = [];
    var remoteAudioElements = new WeakMap();
    var remoteAudioElementList = [];

    function createClientMessageId() {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
      return "msg-" + Date.now() + "-" + Math.random().toString(36).slice(2);
    }

    function postVoiceDisconnect(id, participant) {
      if (!id) return;
      if (disconnectNotified) return;
      disconnectNotified = true;
      fetch("/dashboard/api/voice/disconnect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ callId: id, participantName: participant || "" }),
        keepalive: true,
      }).catch(function () {});
    }

    function createAnalyser(mediaStreamTrack) {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === "suspended") {
        audioCtx.resume().catch(function () {});
      }
      var stream = new MediaStream([mediaStreamTrack]);
      var source = audioCtx.createMediaStreamSource(stream);
      var analyser = audioCtx.createAnalyser();
      analyser.fftSize = 64;
      analyser.smoothingTimeConstant = 0.6;
      source.connect(analyser);
      analyserNodes.push({ analyser: analyser, source: source, stream: stream });
      return analyser;
    }

    function resumeAudioContext() {
      if (audioCtx && audioCtx.state === "suspended") {
        audioCtx.resume().catch(function () {});
      }
    }

    function releaseAnalyser(analyser) {
      if (!analyser) return;
      var index = analyserNodes.findIndex(function (entry) {
        return entry.analyser === analyser;
      });
      if (index === -1) return;
      var entry = analyserNodes[index];
      try { entry.source.disconnect(); } catch (_e) {}
      analyserNodes.splice(index, 1);
    }

    function isAudioTrack(track) {
      if (!track) return false;
      var audioKind = LivekitClient.Track && LivekitClient.Track.Kind
        ? LivekitClient.Track.Kind.Audio
        : "audio";
      return track.kind === "audio" || track.kind === audioKind;
    }

    function attachRemoteAudio(track) {
      if (!track || remoteAudioElements.has(track)) return;
      var attached;
      try { attached = track.attach(); } catch (_e) { return; }
      var elements = Array.isArray(attached) ? attached : [attached];
      elements.forEach(function (el) {
        if (!el || el.nodeType !== 1) return;
        if (String(el.tagName || "").toLowerCase() !== "audio") return;
        el.autoplay = true;
        el.playsInline = true;
        el.style.display = "none";
        if (!el.parentNode) document.body.appendChild(el);
        remoteAudioElements.set(track, el);
        remoteAudioElementList.push(el);
      });
    }

    function detachRemoteAudio(track) {
      if (!track) return;
      try {
        track.detach().forEach(function (el) {
          if (el && el.parentNode) el.parentNode.removeChild(el);
        });
      } catch (_e) {
        var el = remoteAudioElements.get(track);
        if (el && el.parentNode) el.parentNode.removeChild(el);
      }
      remoteAudioElements.delete(track);
      remoteAudioElementList = remoteAudioElementList.filter(function (el) {
        return el && el.parentNode;
      });
    }

    function useAgentAudioTrack(track) {
      if (!isAudioTrack(track) || !track.mediaStreamTrack) return false;
      if (agentAnalyserTrack === track.mediaStreamTrack && agentAnalyser) {
        attachRemoteAudio(track);
        resumeAudioContext();
        return true;
      }
      releaseAnalyser(agentAnalyser);
      attachRemoteAudio(track);
      agentAnalyser = createAnalyser(track.mediaStreamTrack);
      agentAnalyserTrack = track.mediaStreamTrack;
      onAgentAudio(agentAnalyser);
      return true;
    }

    function clearAgentAudioTrack(track) {
      if (!track || track.mediaStreamTrack === agentAnalyserTrack) {
        releaseAnalyser(agentAnalyser);
        agentAnalyser = null;
        agentAnalyserTrack = null;
        onAgentAudio(null);
      }
    }

    function useUserAudioTrack(track) {
      if (!isAudioTrack(track) || !track.mediaStreamTrack) return false;
      if (userAnalyserTrack === track.mediaStreamTrack && userAnalyser) {
        resumeAudioContext();
        return true;
      }
      releaseAnalyser(userAnalyser);
      userAnalyser = createAnalyser(track.mediaStreamTrack);
      userAnalyserTrack = track.mediaStreamTrack;
      onUserAudio(userAnalyser);
      return true;
    }

    function collectionValues(collection) {
      if (!collection) return [];
      if (Array.isArray(collection)) return collection;
      if (typeof collection.values === "function") return Array.from(collection.values());
      return Object.keys(collection).map(function (key) { return collection[key]; });
    }

    function publicationTrack(publication) {
      if (!publication) return null;
      return publication.track || publication.audioTrack || null;
    }

    function participantAudioPublications(participant) {
      var seen = new Set();
      var values = [];
      ["audioTrackPublications", "trackPublications", "tracks"].forEach(function (name) {
        collectionValues(participant && participant[name]).forEach(function (publication) {
          if (!publication || seen.has(publication)) return;
          seen.add(publication);
          values.push(publication);
        });
      });
      return values;
    }

    function reconcileAgentAudio() {
      if (!room) return false;
      var participants = collectionValues(room.remoteParticipants).filter(function (participant) {
        return participant && !isLocalParticipant(participant);
      });
      participants.sort(function (a, b) {
        return (b.isAgent === true ? 1 : 0) - (a.isAgent === true ? 1 : 0);
      });

      for (var p = 0; p < participants.length; p++) {
        var publications = participantAudioPublications(participants[p]);
        for (var i = 0; i < publications.length; i++) {
          var track = publicationTrack(publications[i]);
          if (useAgentAudioTrack(track)) return true;
        }
      }
      return false;
    }

    function scheduleAgentAudioReconcile() {
      reconcileAgentAudio();
      window.setTimeout(reconcileAgentAudio, 250);
      window.setTimeout(reconcileAgentAudio, 1000);
    }

    function cleanupAudio() {
      releaseAnalyser(agentAnalyser);
      releaseAnalyser(userAnalyser);
      agentAnalyser = null;
      agentAnalyserTrack = null;
      userAnalyser = null;
      userAnalyserTrack = null;
      analyserNodes = [];
      remoteAudioElementList.forEach(function (el) {
        if (el && el.parentNode) el.parentNode.removeChild(el);
      });
      remoteAudioElementList = [];
      remoteAudioElements = new WeakMap();
      onAgentAudio(null);
      onUserAudio(null);
      if (audioCtx) {
        audioCtx.close().catch(function () {});
        audioCtx = null;
      }
    }

    function createPendingChatAck(clientMessageId) {
      var pending = {
        clientMessageId: clientMessageId,
        settled: false,
        timeoutId: null,
        resolve: function () {},
        reject: function () {},
      };

      pending.promise = new Promise(function (resolve, reject) {
        pending.resolve = resolve;
        pending.reject = reject;
      });

      pending.finish = function (error) {
        if (pending.settled) return;
        pending.settled = true;
        if (pending.timeoutId !== null) {
          window.clearTimeout(pending.timeoutId);
        }
        var index = pendingChatAcks.indexOf(pending);
        if (index !== -1) {
          pendingChatAcks.splice(index, 1);
        }
        if (error) {
          pending.reject(error);
          return;
        }
        pending.resolve();
      };

      pending.timeoutId = window.setTimeout(function () {
        pending.finish(new Error("Timed out waiting for LiveKit chat delivery."));
      }, CHAT_ACK_TIMEOUT_MS);

      pendingChatAcks.push(pending);
      return pending;
    }

    function resolvePendingChatAck(clientMessageId) {
      if (!clientMessageId) return false;
      var index = pendingChatAcks.findIndex(function (pending) {
        return pending.clientMessageId === clientMessageId;
      });
      if (index === -1) return false;
      pendingChatAcks[index].finish();
      return true;
    }

    function rejectPendingChatAcks(error) {
      var pending = pendingChatAcks.slice();
      pending.forEach(function (entry) {
        entry.finish(error);
      });
    }

    function dispatchSignal(speaker, modality, channel) {
      // Voice turns are already visualized by the graph soundwave strand,
      // so only emit signal particles for non-voice modalities.
      if (modality === "voice") return;
      document.dispatchEvent(new CustomEvent("mh:signal", {
        detail: { speaker: speaker, channel: channel || "dashboard", modality: modality || "text" },
      }));
    }

    function startHeartbeat() {
      if (heartbeatInterval !== null) return;
      heartbeatInterval = window.setInterval(function () {
        if (!room || !connected) return;
        room.localParticipant.publishData(
          new Uint8Array(0),
          { reliable: true, topic: "mh.ping" }
        ).catch(function () {});
      }, HEARTBEAT_INTERVAL_MS);
    }

    function stopHeartbeat() {
      if (heartbeatInterval === null) return;
      window.clearInterval(heartbeatInterval);
      heartbeatInterval = null;
    }

    function handleAgentEvent(text) {
      var payload;
      try { payload = JSON.parse(text); } catch (_e) { return; }
      var type = payload.type;
      var msg = null;

      if (type === "user_input_transcribed" && payload.is_final) {
        var t2 = (payload.transcript || "").trim();
        if (t2) msg = { speaker: "user", text: t2, modality: "voice" };
      } else if (type === "user_chat_received") {
        var t3 = (payload.text || "").trim();
        if (t3) {
          var resolved = resolvePendingChatAck(payload.clientMessageId);
          if (!resolved) msg = { speaker: "user", text: t3, modality: "text" };
        }
      } else if (type === "agent_chat_response") {
        var t4 = (payload.text || "").trim();
        if (t4) msg = { speaker: "agent", text: t4, modality: "text" };
      } else if (type === "agent_voice_transcribed") {
        var t5 = (payload.transcript || payload.text || "").trim();
        if (t5) {
          msg = {
            speaker: "agent",
            text: t5,
            modality: "voice",
            streaming: payload.is_final === false,
            segmentId: payload.segmentId || null,
          };
        }
      } else if (type === "tool_started" || type === "tool_completed") {
        payload.name = (payload.name || "").trim() || "tool";
        onEvent(payload);
        return;
      } else if (type === "provider_latency") {
        onEvent(payload);
        return;
      } else if (type === "agent_error") {
        rejectPendingChatAcks(new Error(payload.message || "Agent encountered an error."));
        onError(payload.message || "Agent encountered an error.");
        return;
      }

      if (msg) {
        onTranscript({
          type: "transcript",
          speaker: msg.speaker,
          text: msg.text,
          modality: msg.modality,
          streaming: msg.streaming === true,
          segmentId: msg.segmentId || null,
          source: "event",
        });
        if (!msg.streaming) {
          dispatchSignal(msg.speaker, msg.modality, "dashboard");
        }
      }
    }

    function isLocalParticipant(participant) {
      if (!participant || !room || !room.localParticipant) return false;
      if (participant.isLocal) return true;
      return participant.identity === room.localParticipant.identity;
    }

    function emitAgentTranscript(text, streaming, segmentId) {
      var body = (text || "").trim();
      if (!body) return;
      onTranscript({
        type: "transcript",
        speaker: "agent",
        text: body,
        modality: voiceActive ? "voice" : "text",
        streaming: streaming === true,
        segmentId: segmentId || null,
        source: "stream",
      });
    }

    function normalizeStreamText(text) {
      return String(text || "").replace(/\s+/g, " ").trim();
    }

    function mergeTextStreamChunk(current, chunk) {
      var existing = String(current || "");
      var next = String(chunk || "");
      var existingNorm = normalizeStreamText(existing);
      var nextNorm = normalizeStreamText(next);
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

    async function handleTranscriptionStream(reader, participant) {
      if (isLocalParticipant(participant)) return;
      var info = reader && reader.info ? reader.info : {};
      var attributes = info.attributes || {};
      var segmentId = (
        attributes["lk.transcription.segment_id"] ||
        attributes["lk.segment_id"] ||
        info.id ||
        null
      );
      var text = "";
      for await (var chunk of reader) {
        text = mergeTextStreamChunk(text, chunk);
        emitAgentTranscript(text, true, segmentId);
      }
      emitAgentTranscript(text, false, segmentId);
    }

    function handleDisplayStream(text) {
      var payload;
      try { payload = JSON.parse(text); } catch (_e) { return; }
      onEvent({ type: "display", payload: payload });
    }

    function handleNotify(text) {
      var payload;
      try { payload = JSON.parse(text); } catch (_e) { return; }
      var title = payload.title || "Mystic Horizon";
      var body = payload.body || "";
      if ("Notification" in window) {
        if (Notification.permission === "granted") {
          try { new Notification(title, { body: body }); } catch (_e) {}
        } else if (Notification.permission === "default") {
          Notification.requestPermission().then(function (perm) {
            if (perm === "granted") {
              try { new Notification(title, { body: body }); } catch (_e) {}
            }
          });
        }
      }
    }

    async function connect() {
      if (connected && room) return;
      if (connectPromise) return connectPromise;

      onState("connecting");
      connectPromise = (async function () {
        onProgress("requesting_token");
        var resp = await fetch("/dashboard/api/voice/token", { method: "POST" });
        if (!resp.ok) {
          var errText = await resp.text();
          throw new Error(errText || "Failed to get voice token");
        }
        var data = await resp.json();
        callId = data.callId;
        roomName = data.roomName;
        participantName = data.participantName || null;
        disconnectNotified = false;
        onProgress("token_received");

        var hasHudHistory = data.hudHistory && data.hudHistory.length > 0;
        if (hasHudHistory && typeof onHudHistory === "function") {
          onHudHistory(data.hudHistory);
        }
        if (data.history && data.history.length > 0) {
          onHistory(data.history, { skipHud: hasHudHistory });
        }

        onProgress("connecting_room");
        room = new LivekitClient.Room();

        room.registerTextStreamHandler("lk.transcription", handleTranscriptionStream);

        room.on(LivekitClient.RoomEvent.DataReceived, function (data, participant, _kind, topic) {
          if (!topic) return;
          var text;
          try { text = new TextDecoder().decode(data); } catch (_e) { return; }
          if (topic === "lk.agent.events") {
            handleAgentEvent(text);
          } else if (topic === "mh.display") {
            handleDisplayStream(text);
          } else if (topic === "mh.notify") {
            handleNotify(text);
          }
        });

        room.on(LivekitClient.RoomEvent.Disconnected, function () {
          var id = callId;
          var participant = participantName;
          rejectPendingChatAcks(new Error("Disconnected from LiveKit."));
          connected = false;
          voiceActive = false;
          callId = null;
          roomName = null;
          participantName = null;
          stopHeartbeat();
          cleanupAudio();
          onState("disconnected");
          postVoiceDisconnect(id, participant);
        });
        room.on(LivekitClient.RoomEvent.ParticipantDisconnected, function (p) {
          if (p.isAgent) {
            rejectPendingChatAcks(new Error("Agent disconnected from LiveKit."));
            onError("Agent disconnected from LiveKit.");
          }
        });
        room.on(LivekitClient.RoomEvent.ParticipantConnected, function () {
          scheduleAgentAudioReconcile();
        });
        if (LivekitClient.RoomEvent.TrackPublished) {
          room.on(LivekitClient.RoomEvent.TrackPublished, function (_publication, participant) {
            if (participant && !isLocalParticipant(participant)) {
              scheduleAgentAudioReconcile();
            }
          });
        }
        room.on(LivekitClient.RoomEvent.TrackSubscribed, function (track, publication, participant) {
          void publication;
          if (isAudioTrack(track) && participant && !isLocalParticipant(participant)) {
            useAgentAudioTrack(track);
          }
        });
        room.on(LivekitClient.RoomEvent.TrackUnsubscribed, function (track) {
          detachRemoteAudio(track);
          clearAgentAudioTrack(track);
          scheduleAgentAudioReconcile();
        });
        room.on(LivekitClient.RoomEvent.LocalTrackPublished, function (publication) {
          useUserAudioTrack(publication.track);
        });

        await room.connect(data.url, data.token);
        connected = true;
        scheduleAgentAudioReconcile();
        startHeartbeat();
        onProgress("room_joined");
        onState("connected", roomName);
      })();

      try {
        await connectPromise;
      } catch (err) {
        var id = callId;
        var participant = participantName;
        connected = false;
        room = null;
        callId = null;
        roomName = null;
        participantName = null;
        stopHeartbeat();
        cleanupAudio();
        onState("disconnected");
        postVoiceDisconnect(id, participant);
        throw err;
      } finally {
        connectPromise = null;
      }
    }

    async function startVoice() {
      if (voiceActive) return;
      await connect();
      onState("requesting");
      try {
        await room.localParticipant.setMicrophoneEnabled(true);
        await room.startAudio();
        resumeAudioContext();
        scheduleAgentAudioReconcile();
        await room.localParticipant.publishData(
          new TextEncoder().encode(JSON.stringify({ action: "start" })),
          { reliable: true, topic: "mh.voice_control" }
        );
        voiceActive = true;
        scheduleAgentAudioReconcile();
        onState("listening", roomName);
      } catch (err) {
        onState(connected ? "connected" : "disconnected", roomName);
        throw err;
      }
    }

    async function stopVoice() {
      if (!room || !voiceActive) return;
      await room.localParticipant.setMicrophoneEnabled(false);
      try {
        await room.localParticipant.publishData(
          new TextEncoder().encode(JSON.stringify({ action: "stop" })),
          { reliable: true, topic: "mh.voice_control" }
        );
      } catch (_err) {}
      voiceActive = false;
      onState("connected", roomName);
    }

    async function sendChat(text, metadata) {
      var message = (text || "").trim();
      if (!message) return;
      await connect();
      var clientMessageId = createClientMessageId();
      var payload = { text: message, clientMessageId: clientMessageId };
      if (metadata && typeof metadata === "object") {
        Object.assign(payload, metadata);
      }
      var pendingAck = createPendingChatAck(clientMessageId);
      try {
        var encoder = new TextEncoder();
        await room.localParticipant.publishData(
          encoder.encode(JSON.stringify(payload)),
          { reliable: true, topic: "mh.chat" }
        );
        dispatchSignal("user", "text", "dashboard");
        await pendingAck.promise;
      } catch (error) {
        pendingAck.finish(error instanceof Error ? error : new Error(String(error)));
        throw error;
      }
    }

    async function sendVoiceControl(action) {
      if (!room || !connected) return;
      var body = JSON.stringify({ action: action });
      try {
        await room.localParticipant.publishData(
          new TextEncoder().encode(body),
          { reliable: true, topic: "mh.voice_control" }
        );
      } catch (_err) {}
    }

    async function disconnect() {
      var id = callId;
      var participant = participantName;
      rejectPendingChatAcks(new Error("Voice connection ended."));
      connected = false;
      voiceActive = false;
      callId = null;
      roomName = null;
      participantName = null;
      stopHeartbeat();
      cleanupAudio();
      if (room) {
        room.disconnect();
        room = null;
      }
      onState("disconnected");
      postVoiceDisconnect(id, participant);
    }

    return {
      connect: connect,
      startVoice: startVoice,
      stopVoice: stopVoice,
      disconnect: disconnect,
      sendChat: sendChat,
      sendVoiceControl: sendVoiceControl,
      isConnected: function () { return connected; },
      isVoiceActive: function () { return voiceActive; },
      isActive: function () { return connected; },
      getAgentAnalyser: function () { return agentAnalyser; },
      getUserAnalyser: function () { return userAnalyser; },
    };
  }

  window.MysticVoiceBridge = { create: createVoiceBridge };
})();
