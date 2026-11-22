// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Atlas Omnia
//
// Sidecar for LibreOffice window: the page's own code (contract: docs/web-protocol.md).
//
// This file is the first classic script in <head>. At top level, before any other script runs, it
// takes the one-time launch code out of the URL fragment and cleans the URL. Everything else waits
// for DOMContentLoaded (after the deferred marked and DOMPurify builds have run).
//
// Rules this file keeps:
// - It talks only to the plugin's /api/* routes, same origin, with credentials "omit".
// - The session token lives only in this closure (no storage, cookie, URL or window.name).
// - Replies are Markdown rendered by marked, then sanitized by DOMPurify with a strict allow-list
//   (formatting only). Links become controls that show and copy the address; nothing navigates.
//   The DOMPurify Trusted Types policy is the only HTML sink; every other text uses textContent.
// - The rendering helpers (addMessage, setRunning, setStatus, scrollMessagesToBottom) follow
//   AtlasOmnia/sidecar excel/src/taskpane/taskpane.ts so the DOM, and the CSS on it, are the same.
(function () {
  "use strict";

  // ------------------------------------------------------------------ launch code (runs first)
  var LAUNCH_FRAGMENT = /^#launch=([A-Za-z0-9_-]{43})$/;
  var launchCode = null;
  (function takeLaunchCode() {
    var match = LAUNCH_FRAGMENT.exec(window.location.hash || "");
    launchCode = match ? match[1] : null;
    try {
      history.replaceState(null, "", "/ui/index.html");
    } catch (e) {
      // Nothing to clean outside the plugin's origin.
    }
  })();

  // ------------------------------------------------------------------ constants
  var HOSTS = {
    calc: { app: "Calc", noun: "spreadsheet" },
    writer: { app: "Writer", noun: "document" },
    impress: { app: "Impress", noun: "presentation" },
  };
  var STATE_POLL_MS = 3000;
  var RETRY_MS = 1000;
  var SEND_TRIES = 3;          // one Send: the request and at most two retries with the same send_id
  var HOST_GONE_CLOSE_MS = 2000;

  // Section 9.2 of the contract: what the page says when its session ended, by reason.
  var ENDED_TEXTS = {
    none: "Click Open Sidecar in LibreOffice to reconnect.",
    replaced: "This document is open in a newer Sidecar window. This one is disconnected.",
    host_gone: "LibreOffice closed this document.",
    turned_off: "The LibreOffice plugin was turned off in Hermes.",
    idle: "This window was idle too long. Click Open Sidecar in LibreOffice again.",
    double_use: "This launch was used by another program; that connection was closed. Click Open Sidecar again.",
    evicted: "Too many Sidecar windows are open, so this one was disconnected. Click Open Sidecar in LibreOffice again.",
  };
  // Chat readiness reasons from the plugin (chat.reason): the window opens, but chat is off.
  var CHAT_OFF_TEXTS = {
    plugin_inactive: "Chat is off: the LibreOffice plugin is not active in Hermes. Enable it, restart the Hermes gateway, then click Open Sidecar in LibreOffice again.",
    no_api_key: "Chat is off: Hermes's API server key is not available. Enable the API server, set API_SERVER_KEY, then click Open Sidecar in LibreOffice again.",
    bad_api_key: "Chat is off: Hermes's API server key cannot be used. Set API_SERVER_KEY again, then click Open Sidecar in LibreOffice again.",
    other: "Chat is off in Hermes. The document tools still work. See README ▸ Sidebar chat.",
  };
  var TEXTS = {
    connecting: "Connecting…",
    ready: "Ready",
    working: "Hermes is working…",
    stopping: "Stopping…",
    approval: "Hermes asked for an approval that Sidecar cannot give. Press Stop and ask in Hermes itself.",
    stopped: "Stopped",
    uncertain: "Outcome not known",
    error: "Error",
    chatOff: "Chat is off",
    notConnected: "Not connected",
    unreachableStatus: "Hermes not reachable",
    unreachable: "Hermes is not reachable. Check that the Hermes gateway is running.",
    newChat: "New chat",
    replyFailed: "I couldn't complete that request.",
    checkingSend: "Checking that Hermes got your message…",
    sendUnknown: "It is not known yet whether Hermes received this message. If it did, its reply appears here. Try again resends this message with the same ID. Hermes remembers the last 64 published message IDs in this session to avoid repeats.",
    sendRetry: "Hermes has not confirmed your last message. Try again can send it now if it did not arrive earlier. Hermes remembers the last 64 published message IDs in this session to avoid repeats.",
    alreadySent: "Hermes already received this message, so it was not sent again. Check the document.",
    emptyReply: "Hermes finished without a reply.",
    copyLink: "Copy link",
    copied: "Copied",
    copyByHand: "Selected: press Ctrl+C or ⌘C",
  };

  // R1 + R19: the only sanitizer profile. Formatting only: no controls, forms, dialogs, images,
  // styles, ids, classes, names, data-* or aria-* from a reply; links only http(s) and mailto.
  // DOMPurify tests every allowed attribute that is not "URI-safe" against ALLOWED_URI_REGEXP, so
  // align ("right") and start ("3") are declared URI-safe; otherwise table alignment and list
  // numbering would be stripped.
  var REPLY_POLICY = Object.freeze({
    ALLOWED_TAGS: Object.freeze(["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li",
      "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr", "a"]),
    ALLOWED_ATTR: Object.freeze(["href", "align", "start"]),
    ADD_URI_SAFE_ATTR: Object.freeze(["align", "start"]),
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(https?|mailto):/i,
    RETURN_TRUSTED_TYPE: true,
  });
  var LINE_BREAK_TAG = /^<br\s*\/?>$/i;

  // ------------------------------------------------------------------ state (closure only)
  var session = null;          // the session token: never stored anywhere else
  var ended = false;
  var info = { document: null, endpoint: "", chatLabel: "", chatReady: false, chatReason: null, maxMessage: 8000 };
  var turn = null;             // { id, seq, have, text, state, stopSent }
  var seenTurns = {};          // ids of the turns this window has followed, so each reply is shown once
  var pendingSend = null;      // { id, message, tries, unknown } until this Send's own turn or refusal arrives
  var sending = false;
  var newChatPending = false;  // a New chat request is on its way: no Send, no state adoption
  var epoch = 0;               // grows at each Send and New chat: a state answer from before is stale
  var focusStop = false;       // Send came from the composer: move focus to Stop once it is usable
  var unreachable = false;
  var statePending = false;
  var stateTimer = null;
  var theme = null;
  var followCapable = false;
  var followOn = false;
  var undocked = false;
  var otherDisplay = false;
  var windowSeq = -1;
  var windowPoll = null;
  var windowTimer = null;
  var placed = null;
  var placementPending = false;
  var placementTimer = null;
  var placementGeneration = 0;

  // ------------------------------------------------------------------ transport
  function api(method, path, body, keepalive, signal) {
    var headers = { "Accept": "application/json" };
    if (session) headers["X-Sidecar-Session"] = session;
    var init = {
      method: method,
      headers: headers,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
    };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    if (keepalive) init.keepalive = true;
    if (signal) init.signal = signal;
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        return { status: res.status, ok: res.ok, data: data && typeof data === "object" ? data : {} };
      });
    });
  }

  // A refusal that ends the window's session (section 6, 9.2). True when handled.
  function handleSessionEnd(result) {
    var code = result.data.code;
    if (result.status === 401 && code === "no_session") {
      endSession(result.data.ended || null);
      return true;
    }
    if (code === "turned_off") {
      endSession("turned_off");
      return true;
    }
    return false;
  }

  // The plugin's error strings are sentences without a capital or a full stop ("this chat is still
  // waiting for a reply"); show them as sentences.
  function sentence(text, fallback) {
    var s = typeof text === "string" && text.trim() ? text.trim() : fallback;
    s = s.charAt(0).toUpperCase() + s.slice(1);
    return /[.!?…]$/.test(s) ? s : s + ".";
  }

  // The plugin counts reply characters as Unicode code points (Python str length).
  function codePoints(text) { return Array.from(text).length; }
  function firstCodePoints(text, n) { return Array.from(text).slice(0, n).join(""); }

  // ------------------------------------------------------------------ session
  function exchange() {
    if (!launchCode) {
      endSession(null);
      return;
    }
    var code = launchCode;
    launchCode = null;
    setStatus(TEXTS.connecting);
    api("POST", "/api/session", { launch: code }).then(function (result) {
      if (result.ok && typeof result.data.session === "string") {
        session = result.data.session;
        var data = result.data;
        if (theme) theme.setHostScheme(data.host_scheme);
        if (data.limits && typeof data.limits.max_message === "number" && data.limits.max_message > 0) {
          info.maxMessage = data.limits.max_message;
          byId("prompt-input").maxLength = data.limits.max_message;
        }
        info.endpoint = typeof data.endpoint === "string" ? data.endpoint : window.location.origin;
        if (data.mode === "app" && validBounds(data.bounds)) alignWindow(data.bounds);
        followCapable = data.mode === "app" && data.follow === true;
        followOn = readFollowPreference();
        byId("follow-setting").hidden = !followCapable;
        byId("follow-toggle").checked = followOn;
        updateFollowStatus();
        resumeFollowing();
        applyState(data);
        startStatePolling();
        return;
      }
      ended = true;
      showConnect(sentence(result.data.error, ENDED_TEXTS.none), false);
      setStatus(TEXTS.notConnected);
      setBridge(false, "LibreOffice disconnected");
    }, function () {
      // Nothing came back; the code may or may not have been used, so it is not presented again.
      ended = true;
      showConnect(TEXTS.unreachable + " Then click Open Sidecar in LibreOffice again.", false);
      setStatus(TEXTS.unreachableStatus);
      setBridge(false, "LibreOffice disconnected");
    });
  }

  function endSession(reason) {
    var key = reason && Object.prototype.hasOwnProperty.call(ENDED_TEXTS, reason) ? reason : "none";
    session = null;
    ended = true;
    turn = null;
    pendingSend = null;
    sending = false;
    newChatPending = false;
    epoch += 1;
    stopStatePolling();
    stopFollowing();
    cancelPlacement();
    updateFollowStatus();
    setRunning(false);
    setComposerEnabled(false);
    byId("new-chat-button").disabled = true;
    showConnect(ENDED_TEXTS[key], false);
    setStatus(TEXTS.notConnected);
    setBridge(false, key === "host_gone" ? "LibreOffice document closed" : "LibreOffice disconnected");
    if (key === "host_gone") {
      setTimeout(function () { window.close(); }, HOST_GONE_CLOSE_MS);
    }
  }

  function closeSessionOnExit() {
    stopFollowing();
    cancelPlacement();
    if (!session) return;
    var token = session;
    session = null;
    try {
      fetch("/api/session/close", {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/json", "X-Sidecar-Session": token },
        body: "{}",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        keepalive: true,
      }).catch(function () {});
    } catch (e) {
      // The page is going away either way.
    }
  }

  // ------------------------------------------------------------------ window placement (app mode)
  function validBounds(b) {
    if (!b || typeof b !== "object") return false;
    return ["x", "y", "width", "height"].every(function (k) { return Number.isInteger(b[k]); }) &&
      b.x >= -32768 && b.x <= 32767 && b.y >= -32768 && b.y <= 32767 &&
      b.width >= 240 && b.height >= 240 && b.width <= 8192 && b.height <= 8192;
  }

  // bounds is the client rectangle the window should have. Measure this window's own title bar,
  // clamp to the screen's available area, then resize BEFORE moving (moving first is clamped
  // against the old, taller size).
  function alignWindow(bounds) {
    try {
      var deco = Math.max(0, window.outerHeight - window.innerHeight);
      var scr = window.screen;
      var ax = typeof scr.availLeft === "number" ? scr.availLeft : 0;
      var ay = typeof scr.availTop === "number" ? scr.availTop : 0;
      var aw = scr.availWidth;
      var ah = scr.availHeight;
      var w = Math.min(bounds.width, aw);
      var h = Math.min(bounds.height + deco, ah);
      var x = Math.min(Math.max(bounds.x, ax), ax + aw - w);
      var y = Math.min(Math.max(bounds.y - deco, ay), ay + ah - h);
      var sizeChanged = window.outerWidth !== w || window.outerHeight !== h;
      var positionChanged = window.screenX !== x || window.screenY !== y;
      if (!sizeChanged && !positionChanged) return;
      beginPlacement(); // mark pending BEFORE resize events can run
      if (sizeChanged) window.resizeTo(w, h);
      if (positionChanged) window.moveTo(x, y);
    } catch (e) {
      // A browser that refuses to move the window leaves it where the launcher put it.
    }
  }

  // ------------------------------------------------------------------ optional following
  // This is the only page preference; session and geometry never enter storage.
  function readFollowPreference() {
    try { return window.localStorage.getItem("sidecar.follow") === "on"; }
    catch (e) { return false; }
  }

  function followEligible() {
    return !!session && !ended && followCapable && followOn && !undocked &&
      !otherDisplay && !document.hidden;
  }

  function screenMeasurements() {
    var s = window.screen;
    return [s.availLeft, s.availTop, s.availWidth, s.availHeight,
      s.width, s.height, window.devicePixelRatio].join(",");
  }

  function readPlacement() {
    return { x: window.screenX, y: window.screenY, width: window.outerWidth,
      height: window.outerHeight, screen: screenMeasurements() };
  }

  function cancelPlacement() {
    placementGeneration += 1;
    if (placementTimer !== null) clearTimeout(placementTimer);
    placementTimer = null;
    placementPending = false;
  }

  function beginPlacement() {
    cancelPlacement();
    placementPending = true;
    var generation = placementGeneration;
    placementTimer = setTimeout(function () {
      if (generation !== placementGeneration) return;
      placed = readPlacement();
      placementTimer = null;
      placementPending = false; // only this completed read-back releases comparisons
    }, 400);
  }

  function checkUndock() {
    if (!followEligible() || placementPending) return;
    var now = readPlacement();
    if (!placed || now.screen !== placed.screen) {
      placed = now; // screen/scale/work-area change is not a hand move
      return;
    }
    if (["x", "y", "width", "height"].some(function (k) { return Math.abs(now[k] - placed[k]) > 8; })) {
      undocked = true;
      stopFollowing();
      updateFollowStatus();
      setText(byId("follow-announcement"), "Sidecar is not following LibreOffice.");
    }
  }

  function updateFollowStatus(text) {
    var active = followCapable && followOn && !ended;
    byId("follow-strip").hidden = !active;
    byId("reattach-button").hidden = !(active && (undocked || otherDisplay));
    setText(byId("follow-status"), text || (undocked ? "Sidecar is not following LibreOffice." :
      otherDisplay ? "LibreOffice is on another display. Move this window there, then choose Reattach." :
      "Following LibreOffice"));
  }

  function stopFollowing() {
    if (windowTimer !== null) clearTimeout(windowTimer);
    windowTimer = null;
    if (windowPoll) windowPoll.abort();
    windowPoll = null; // callbacks from this chain are fenced by controller identity
  }

  function resumeFollowing() {
    if (followEligible() && !windowPoll && windowTimer === null) pollWindow();
  }

  function pollWindow() {
    if (!followEligible() || windowPoll) return;
    checkUndock();
    if (!followEligible()) return;
    var controller = new AbortController();
    windowPoll = controller;
    var next = function (delay) {
      if (!followEligible()) return;
      if (delay === 0) pollWindow();
      else windowTimer = setTimeout(function () { windowTimer = null; pollWindow(); }, delay);
    };
    api("GET", "/api/window?seq=" + Math.max(0, windowSeq), undefined, false, controller.signal).then(function (result) {
      if (windowPoll !== controller) return;
      windowPoll = null;
      if (!followEligible() || handleSessionEnd(result)) return;
      if (!result.ok) {
        if (result.data.code === "no_follow") {
          followCapable = false;
          byId("follow-setting").hidden = true;
          updateFollowStatus();
          return;
        }
        next(RETRY_MS);
        return;
      }
      if (result.data.superseded === true) return;
      applyWindowSnapshot(result.data.window);
      var retry = result.data.retry_ms;
      next(retry === 0 ? 0 : Number.isFinite(retry) ? Math.min(5000, Math.max(100, retry)) : RETRY_MS);
    }, function () {
      if (windowPoll !== controller) return;
      windowPoll = null;
      next(RETRY_MS);
    });
  }

  function applyWindowSnapshot(snapshot) {
    if (!followEligible() || !snapshot || !Number.isInteger(snapshot.seq) ||
        snapshot.seq < 0 || snapshot.seq > 2147483647 || snapshot.seq <= windowSeq) return;
    var state = snapshot.state;
    if (state !== "normal" && ["minimized", "fullscreen", "hidden", "window_closed"].indexOf(state) < 0) return;
    if (state === "normal" ? !validBounds(snapshot.bounds) : snapshot.bounds !== null) return;
    checkUndock(); // compare against actual placed geometry before any new own placement
    if (!followEligible()) return;
    windowSeq = snapshot.seq;
    if (state !== "normal") {
      var paused = { minimized: "Following is paused while LibreOffice is minimised.",
        fullscreen: "Following is paused while LibreOffice is in full screen.",
        hidden: "Following is paused while LibreOffice is hidden.",
        window_closed: "The LibreOffice window this Sidecar followed was closed." };
      updateFollowStatus(paused[state]);
      return; // null geometry never invokes a placement fallback
    }
    var b = snapshot.bounds, s = window.screen;
    var left = typeof s.availLeft === "number" ? s.availLeft : 0;
    var top = typeof s.availTop === "number" ? s.availTop : 0;
    var cx = b.x + b.width / 2, cy = b.y + b.height / 2;
    if (s.isExtended === true && (cx < left || cx >= left + s.availWidth || cy < top || cy >= top + s.availHeight)) {
      otherDisplay = true;
      stopFollowing();
      updateFollowStatus();
      return;
    }
    updateFollowStatus();
    alignWindow(b);
  }

  function reattach() {
    if (!followCapable || !followOn || !session || ended) return;
    stopFollowing();
    cancelPlacement();
    undocked = false;
    otherDisplay = false;
    placed = readPlacement();
    windowSeq = -1; // query seq=0, accept even the latest previously seen snapshot
    setText(byId("follow-announcement"), "");
    updateFollowStatus();
    resumeFollowing();
  }

  function toggleFollowing() {
    followOn = byId("follow-toggle").checked;
    try { window.localStorage.setItem("sidecar.follow", followOn ? "on" : "off"); }
    catch (e) { /* unavailable storage: this session only; the next launch defaults off */ }
    stopFollowing();
    cancelPlacement();
    if (followOn && !undocked && !otherDisplay) {
      placed = readPlacement();
      windowSeq = -1;
    }
    updateFollowStatus();
    resumeFollowing();
  }

  function followVisibilityChanged() {
    if (document.hidden) stopFollowing();
    else { checkUndock(); resumeFollowing(); }
  }

  // ------------------------------------------------------------------ host state
  function applyState(data) {
    if (data.document && typeof data.document === "object") info.document = data.document;
    if (typeof data.chat_label === "string") info.chatLabel = data.chat_label;
    var chat = data.chat && typeof data.chat === "object" ? data.chat : { ready: false, reason: null };
    info.chatReady = chat.ready === true;
    info.chatReason = typeof chat.reason === "string" ? chat.reason : null;
    if (unreachable) {
      unreachable = false;
      byId("desk-connect-button").hidden = true;
    }

    var host = info.document ? HOSTS[info.document.type] : null;
    var title = host ? "Sidecar for " + host.app : "Sidecar for LibreOffice";
    setText(byId("app-title"), title);
    var docTitle = info.document && typeof info.document.title === "string" ? info.document.title : "";
    document.title = docTitle ? title + " — " + docTitle : title;
    var prompt = "Ask Hermes to work in this " + (host ? host.noun : "document");
    var input = byId("prompt-input");
    if (input.placeholder !== prompt) {
      input.placeholder = prompt;
      input.setAttribute("aria-label", prompt);
    }
    setBridge(true, "LibreOffice connected");
    var line = byId("bridge-session-id");
    setText(line, [info.chatLabel, docTitle].filter(Boolean).join(" · "));
    line.title = docTitle;

    if (!info.chatReady) {
      showConnect(CHAT_OFF_TEXTS[info.chatReason] || CHAT_OFF_TEXTS.other, false);
      if (!turn) setStatus(TEXTS.chatOff);
    } else if (pendingSend && pendingSend.unknown && !sending) {
      showConnect(TEXTS.sendRetry, true);   // the Try again of this Send stays offered
      setStatus(TEXTS.uncertain);           // not "not reachable": this poll was answered
    } else {
      byId("desk-connect").hidden = true;
      var current = byId("connection-status").textContent;
      if ([TEXTS.chatOff, TEXTS.connecting, TEXTS.notConnected, TEXTS.unreachableStatus].indexOf(current) >= 0) {
        setStatus(TEXTS.ready);
      }
    }

    // A turn this window has not followed yet, also one that has already ended (the answer to Send
    // was lost and the reply finished before this poll): follow it, so its reply is shown once.
    // While a Send is undecided only that Send's own turn (its send_id) decides it: a state without
    // it proves nothing (the Send may still be admitted, or this answer left before it arrived).
    var view = data.turn;
    if (!turn && !sending && view && typeof view === "object" && typeof view.id === "string" &&
        !Object.prototype.hasOwnProperty.call(seenTurns, view.id) &&
        (!pendingSend || view.send_id === pendingSend.id)) {
      settlePending();
      startTurn(view);
      return;
    }
    setRunning(!!turn || sending);
  }

  // An answer to a state poll is applied only when no Send or New chat began after the poll was
  // sent (the plugin may have answered it before either arrived).
  function pollState() {
    if (!session || turn || sending || statePending || newChatPending) return;
    statePending = true;
    var asked = epoch;
    api("GET", "/api/state").then(function (result) {
      statePending = false;
      if (!session || asked !== epoch) return;
      if (result.ok) applyState(result.data);
      else if (!handleSessionEnd(result)) showUnreachable();
    }, function () {
      statePending = false;
      if (session && asked === epoch) showUnreachable();
    });
  }
  function startStatePolling() {
    stopStatePolling();
    stateTimer = setInterval(function () { checkUndock(); pollState(); }, STATE_POLL_MS);
  }
  function stopStatePolling() {
    if (stateTimer !== null) clearInterval(stateTimer);
    stateTimer = null;
  }

  function showUnreachable() {
    unreachable = true;
    showConnect(pendingSend && pendingSend.unknown ? TEXTS.unreachable + " " + TEXTS.sendRetry : TEXTS.unreachable, true);
    setStatus(TEXTS.unreachableStatus);
    setComposerEnabled(false);
  }

  function showConnect(text, retry) {
    byId("desk-connect").hidden = false;
    setText(byId("desk-connect-text"), text);
    byId("desk-connect-button").hidden = !retry;
    setComposerEnabled(false);
  }

  function setBridge(connected, text) {
    var status = byId("bridge-status");
    setText(status, text);
    status.className = "bridge-status " + (connected ? "bridge-connected" : "bridge-disconnected");
  }

  // ------------------------------------------------------------------ chat turn
  function sendPrompt() {
    var input = byId("prompt-input");
    var prompt = input.value.trim();
    if (!prompt || !session || turn || sending || pendingSend || newChatPending || !info.chatReady || unreachable) return;
    if (codePoints(prompt) > info.maxMessage) {
      setStatus("A message can have at most " + info.maxMessage + " characters.");
      return;
    }
    focusStop = document.activeElement === input || document.activeElement === byId("send-button");
    input.value = "";
    addMessage("user", prompt);
    epoch += 1;
    pendingSend = { id: newSendId(), message: prompt, tries: 0, unknown: false };
    setStatus(TEXTS.working);
    deliver(pendingSend);
  }

  // One Send, made idempotent by its send_id (contract 6.3): when the answer is lost, the same
  // request is sent again, and the plugin answers with the turn the first one started instead of
  // starting another (it waits for that first request if it is still being admitted). After
  // SEND_TRIES unanswered requests nothing is sent on its own: the Send stays undecided (no fresh
  // send_id, no New chat) until the state poll shows its turn or the user presses Try again,
  // which sends the same request once more.
  function deliver(p) {
    if (pendingSend !== p || !session) return;
    p.tries += 1;
    sending = true;
    setRunning(true);
    api("POST", "/api/chat", { message: p.message, send_id: p.id }).then(function (result) {
      if (pendingSend !== p) return;
      unreachable = false;   // the plugin answered: an earlier "not reachable" banner no longer holds
      if (result.ok && result.data.turn && typeof result.data.turn.id === "string") {
        sending = false;
        settlePending();
        startTurn(result.data.turn);
        return;
      }
      if (handleSessionEnd(result)) return;
      var code = result.data.code;
      // endpoint_busy: every handler slot was taken, nothing was read. busy on a retry: the
      // first request of this Send is still being admitted. Both: ask again with the same send_id.
      if (code === "endpoint_busy" || (code === "busy" && p.tries > 1)) {
        retryDelivery(p);
        return;
      }
      sending = false;
      settlePending();
      focusStop = false;
      if (code === "already_sent") {
        // An earlier request of this Send started its turn, which this window no longer follows.
        addMessage("assistant", null, TEXTS.alreadySent);
        setStatus(TEXTS.uncertain);
      } else {
        // A refusal for this send_id: the plugin did not start it.
        var input = byId("prompt-input");
        if (!input.value) input.value = p.message;
        showTurnError(sentence(result.data.error, TEXTS.replyFailed));
      }
      finishRunning();
    }, function () {
      if (pendingSend === p) retryDelivery(p);
    });
  }

  function retryDelivery(p) {
    if (p.tries < SEND_TRIES) {
      setStatus(TEXTS.checkingSend);
      setTimeout(function () { deliver(p); }, RETRY_MS);
      return;
    }
    // Not known whether it arrived. No blind resend and no fresh send_id: the state poll shows
    // its turn if it did; Try again asks once more with the same send_id.
    sending = false;
    focusStop = false;
    if (!p.unknown) addMessage("assistant", null, TEXTS.sendUnknown);
    p.unknown = true;
    setStatus(TEXTS.uncertain);
    showConnect(unreachable ? TEXTS.unreachable + " " + TEXTS.sendRetry : TEXTS.sendRetry, true);
    finishRunning();
  }

  // Try again for an undecided Send: one more request with the same send_id.
  function retryPending() {
    var p = pendingSend;
    if (!p || !p.unknown || sending || !session) return;
    epoch += 1;  // invalidate state polls started before this explicit retry, including failures
    p.tries = SEND_TRIES - 1;
    setStatus(TEXTS.checkingSend);
    deliver(p);
  }

  // The undecided Send is decided (its turn, or a refusal): drop it and its Try again.
  function settlePending() {
    var p = pendingSend;
    pendingSend = null;
    if (p && p.unknown && !unreachable) byId("desk-connect").hidden = true;
  }

  function onConnectButton() {
    if (pendingSend && pendingSend.unknown) retryPending();
    else pollState();
  }

  function newSendId() {
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    var raw = "";
    for (var i = 0; i < bytes.length; i += 1) raw += String.fromCharCode(bytes[i]);
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function startTurn(view) {
    seenTurns[view.id] = true;
    turn = {
      id: view.id,
      seq: typeof view.seq === "number" ? view.seq : 0,
      have: 0,
      text: "",
      state: view.state,
      stopSent: view.state === "stopping",
    };
    setRunning(true);
    showWorking(view);
    if (focusStop && !byId("stop-button").disabled) byId("stop-button").focus();
    focusStop = false;
    pollTurn();
  }

  // One long-poll per active turn, re-issued from the network callback (never from a timer);
  // one second's pause after a network error.
  function pollTurn() {
    var t = turn;
    if (!t || !session) return;
    var path = "/api/turn?id=" + encodeURIComponent(t.id) + "&seq=" + t.seq + "&have=" + t.have;
    api("GET", path).then(function (result) {
      if (turn !== t) return;
      if (result.ok && result.data.turn && typeof result.data.turn === "object") {
        if (result.data.superseded === true) return;
        applyTurn(t, result.data.turn);
        if (turn === t) pollTurn();
        return;
      }
      if (handleSessionEnd(result)) return;
      if (result.status === 404) {
        turn = null;
        showTurnError(sentence(result.data.error, TEXTS.replyFailed));
        finishRunning();
        return;
      }
      setTimeout(pollTurn, RETRY_MS);
    }, function () {
      if (turn === t) setTimeout(pollTurn, RETRY_MS);
    });
  }

  function applyTurn(t, view) {
    if (typeof view.text === "string" && typeof view.text_from === "number" && view.text_from <= codePoints(t.text)) {
      t.text = firstCodePoints(t.text, view.text_from) + view.text;
      t.have = codePoints(t.text);
    }
    if (typeof view.seq === "number") t.seq = view.seq;
    t.state = view.state;
    if (view.state === "done") {
      turn = null;
      finishTurn(t, view.outcome && typeof view.outcome === "object" ? view.outcome : { kind: "error", message: null });
    } else {
      showWorking(view);
    }
  }

  function showWorking(view) {
    if (view.state === "stopping") {
      setStatus(TEXTS.stopping);
      byId("stop-button").disabled = true;
    } else if (view.hermes_state === "waiting_for_approval") {
      setStatus(TEXTS.approval);
    } else {
      setStatus(TEXTS.working);
    }
  }

  function finishTurn(t, outcome) {
    var kind = outcome.kind;
    if (kind === "completed") {
      addMessage("assistant", t.text.trim() ? t.text : null, TEXTS.emptyReply);
      setStatus(TEXTS.ready);
    } else if (kind === "user") {
      addMessage("assistant", t.text.trim() ? t.text : null, null, sentence(outcome.message, "Stopped."));
      setStatus(TEXTS.stopped);
    } else if (kind === "uncertain") {
      addMessage("assistant", null, sentence(outcome.message, TEXTS.replyFailed));
      setStatus(TEXTS.uncertain);
    } else if (kind === "turned_off") {
      addMessage("assistant", null, sentence(outcome.message, ENDED_TEXTS.turned_off));
      setStatus(TEXTS.notConnected);
    } else {
      showTurnError(sentence(outcome.message, TEXTS.replyFailed));
    }
    finishRunning();
  }

  // Excel's error shape: the assistant says it could not complete the request, and the reason is
  // an error row in the message's activity list.
  function showTurnError(message) {
    addMessage("assistant", null, TEXTS.replyFailed, null, message);
    setStatus(TEXTS.error);
  }

  function finishRunning() {
    setRunning(false);
    var active = document.activeElement;
    var composer = byId("prompt-input").closest(".composer");
    if (!active || active === document.body || composer.contains(active)) {
      if (!byId("prompt-input").disabled) byId("prompt-input").focus();
    }
  }

  function stopTurn() {
    var t = turn;
    if (!t || t.state === "done" || t.stopSent || !session) return;
    t.stopSent = true;
    byId("stop-button").disabled = true;
    setStatus(TEXTS.stopping);
    api("POST", "/api/turn/stop", { turn: t.id }).then(function (result) {
      if (turn !== t) return;
      if (result.ok) return;  // the long-poll reports stopping, then the outcome
      if (handleSessionEnd(result)) return;
      t.stopSent = false;
      byId("stop-button").disabled = false;
      setStatus(sentence(result.data.error, "Stop did not reach Hermes. Try again."));
    }, function () {
      if (turn !== t) return;
      t.stopSent = false;
      byId("stop-button").disabled = false;
      setStatus(TEXTS.unreachableStatus);
    });
  }

  // New chat is one request at a time and never overlaps a Send: while it is on its way no Send
  // starts and no state answer is applied, and a state answer from before it is dropped.
  function newChat() {
    if (!session || turn || sending || pendingSend || newChatPending) return;
    newChatPending = true;
    epoch += 1;
    setRunning(false);
    var done = function () {
      newChatPending = false;
      epoch += 1;
    };
    api("POST", "/api/new-chat", {}).then(function (result) {
      done();
      if (result.ok) {
        seenTurns = {};
        byId("messages").replaceChildren();
        if (typeof result.data.chat_label === "string") info.chatLabel = result.data.chat_label;
        var docTitle = info.document && typeof info.document.title === "string" ? info.document.title : "";
        setText(byId("bridge-session-id"), [info.chatLabel, docTitle].filter(Boolean).join(" · "));
        setStatus(TEXTS.newChat);
        byId("prompt-input").focus();
      } else if (!handleSessionEnd(result)) {
        setStatus(sentence(result.data.error, "New chat is not available right now."));
      }
      setRunning(!!turn || sending);
    }, function () {
      done();
      setRunning(!!turn || sending);
      showUnreachable();
    });
  }

  // ------------------------------------------------------------------ rendering (from taskpane.ts)
  // markdown: reply text rendered through the strict profile; plain: app text shown as-is;
  // note: an app sentence after the reply (the Stop outcome); error: the reason row of a failure.
  function addMessage(role, markdown, plain, note, error) {
    var messages = byId("messages");
    var root = document.createElement("article");
    root.className = "message " + role;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    root.appendChild(bubble);
    var uiMessage = { root: root, bubble: bubble };
    if (role === "assistant" && typeof markdown === "string") renderReply(bubble, markdown);
    else if (role === "assistant" && plain) appendParagraph(bubble, plain);
    else if (role !== "assistant") bubble.textContent = markdown;
    if (note) appendParagraph(bubble, note).className = "reply-note";
    if (error) renderActivity(uiMessage, { status: "error", label: error });
    // Inserted complete, once, so a screen reader reads the reply once.
    messages.appendChild(root);
    scrollMessagesToBottom();
    return uiMessage;
  }

  function appendParagraph(bubble, text) {
    var p = document.createElement("p");
    p.textContent = text;
    bubble.appendChild(p);
    return p;
  }

  // HTML the model writes into a reply is shown as text (a line break tag stays a line break).
  // Replies are Markdown; this keeps hostile markup away from even the sanitizer's inert parse,
  // which the page's CSP would otherwise report.
  function escapeHtml(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function installMarkdownRules() {
    if (!window.marked || typeof window.marked.use !== "function") return;
    window.marked.use({
      renderer: {
        html: function (token) {
          var raw = typeof token === "object" && token ? String(token.text || "") : String(token || "");
          if (LINE_BREAK_TAG.test(raw.trim())) return "<br>";
          var shown = escapeHtml(raw.replace(/\n+$/, "")).replace(/\n/g, "<br>");
          return token && token.block ? "<p>" + shown + "</p>\n" : shown;
        },
      },
    });
  }

  function renderReply(target, markdown) {
    var purify = window.DOMPurify;
    if (!window.marked || !purify || !purify.isSupported) {
      target.replaceChildren();
      appendParagraph(target, markdown);
      return;
    }
    var html;
    try {
      html = window.marked.parse(markdown, { async: false, breaks: true });
    } catch (e) {
      target.replaceChildren();
      appendParagraph(target, markdown);
      return;
    }
    target.innerHTML = purify.sanitize(html, REPLY_POLICY);
    var anchors = target.querySelectorAll("a");
    for (var i = 0; i < anchors.length; i += 1) replaceLink(anchors[i]);
  }

  // A link in a reply never navigates: it becomes a button that shows the full address with a
  // Copy link button next to it.
  function replaceLink(anchor) {
    var href = anchor.getAttribute("href") || "";
    var label = anchor.textContent || href;
    if (!href) {
      anchor.replaceWith(document.createTextNode(label));
      return;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "reply-link";
    button.textContent = label;
    button.title = href;
    button.setAttribute("aria-expanded", "false");
    var panel = null;
    button.addEventListener("click", function () {
      if (panel) {
        panel.remove();
        panel = null;
        button.setAttribute("aria-expanded", "false");
        return;
      }
      panel = linkPanel(href);
      button.after(panel);
      button.setAttribute("aria-expanded", "true");
    });
    anchor.replaceWith(button);
  }

  function linkPanel(href) {
    var panel = document.createElement("span");
    panel.className = "reply-link-panel";
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", "Link address");
    var address = document.createElement("code");
    address.className = "reply-link-url";
    address.textContent = href;
    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "reply-link-copy";
    copy.textContent = TEXTS.copyLink;
    copy.addEventListener("click", function () {
      var done = function (text) {
        copy.textContent = text;
        setTimeout(function () { copy.textContent = TEXTS.copyLink; }, 2000);
      };
      var byHand = function () {
        var selection = window.getSelection();
        if (selection) {
          var range = document.createRange();
          range.selectNodeContents(address);
          selection.removeAllRanges();
          selection.addRange(range);
        }
        done(TEXTS.copyByHand);
      };
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        navigator.clipboard.writeText(href).then(function () { done(TEXTS.copied); }, byHand);
      } else {
        byHand();
      }
    });
    panel.appendChild(address);
    panel.appendChild(copy);
    return panel;
  }

  function renderActivity(ui, item) {
    if (!ui.activityList) {
      var activity = document.createElement("details");
      activity.className = "activity";
      activity.open = true;
      var summary = document.createElement("summary");
      summary.textContent = "Activity";
      activity.appendChild(summary);
      var list = document.createElement("div");
      list.className = "activity-list";
      activity.appendChild(list);
      ui.root.appendChild(activity);
      ui.activityList = list;
    }
    var row = document.createElement("div");
    row.className = "activity-item" + (item.status === "error" ? " error" : "");
    row.dataset.status = item.status;
    var mark = document.createElement("span");
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = item.status === "running" ? "\u2026" : item.status === "done" ? "\u2713" : "!";
    var label = document.createElement("span");
    label.textContent = item.label;
    row.appendChild(mark);
    row.appendChild(label);
    ui.activityList.appendChild(row);
  }

  function setComposerEnabled(on) {
    byId("prompt-input").disabled = !on;
    byId("send-button").disabled = !on;
  }

  function setRunning(isRunning) {
    var usable = !!session && !ended && info.chatReady && !unreachable && !pendingSend && !newChatPending;
    byId("send-button").disabled = isRunning || !usable;
    byId("prompt-input").disabled = isRunning || !usable;
    byId("stop-button").disabled = !isRunning || !turn || turn.stopSent;
    byId("new-chat-button").disabled = isRunning || !usable;
  }

  function setStatus(status) { setText(byId("connection-status"), status); }

  // Only touch text that changed, so live regions announce real changes only.
  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }

  function scrollMessagesToBottom() {
    var messages = byId("messages");
    messages.scrollTop = messages.scrollHeight;
  }

  function byId(id) {
    var el = document.getElementById(id);
    if (!el) throw new Error("Missing element #" + id);
    return el;
  }

  // ------------------------------------------------------------------ controls
  function bindControls() {
    byId("send-button").addEventListener("click", sendPrompt);
    byId("stop-button").addEventListener("click", stopTurn);
    byId("new-chat-button").addEventListener("click", newChat);
    byId("desk-connect-button").addEventListener("click", onConnectButton);
    byId("prompt-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        sendPrompt();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && turn && !byId("settings-dialog").open) {
        event.preventDefault();
        stopTurn();
      }
    });
    byId("settings-button").addEventListener("click", function () {
      setText(byId("bridge-url"), info.endpoint || "not connected");
      var doc = info.document;
      setText(byId("document-line"), doc && typeof doc.title === "string"
        ? doc.title + (HOSTS[doc.type] ? " (" + HOSTS[doc.type].app + ")" : "") : "none");
      byId("settings-dialog").showModal();
    });
    // Keep Tab inside the open dialog: native showModal() would otherwise leave one stop on the
    // browser's own controls between the last and the first button (G5).
    byId("settings-dialog").addEventListener("keydown", function (event) {
      if (event.key !== "Tab") return;
      var first = byId("close-settings-button"), last = byId("save-settings-button");
      if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      }
    });
    byId("reattach-button").addEventListener("click", reattach);
    byId("follow-toggle").addEventListener("change", toggleFollowing);
    window.addEventListener("resize", checkUndock);
    document.addEventListener("visibilitychange", followVisibilityChanged);
    window.addEventListener("focus", pollState);
    document.addEventListener("visibilitychange", pollState);
    window.addEventListener("pagehide", closeSessionOnExit);
    // A page restored from the back/forward cache closed its session when it was hidden.
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && !session) endSession(null);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    theme = window.OmniaTheme ? window.OmniaTheme.installThemeControls(document) : null;
    installMarkdownRules();
    bindControls();
    exchange();
  });
})();
